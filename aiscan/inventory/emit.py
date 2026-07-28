"""Record emission (SPEC §6.9): bundle_bom → record.json, plus artifact writing.

Every [D] field carries provenance; [E]/[G] slots stay null with source
markers. Canonical JSON everywhere; timestamps only in record provenance and
scan_health.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiscan
from aiscan.context import ScanContext
from aiscan.facts.models import (
    AgentDefF,
    AnyFact,
    FindingRecord,
    LLMCallSiteF,
    MCPDefF,
    ModelRefF,
    PolicyDefF,
    PromptDefF,
    StateDefF,
    ToolDefF,
)
from aiscan.graph.canonical import canonical_json, graph_canonical_json
from aiscan.graph.model import Graph
from aiscan.graph.queries import BundleBom
from aiscan.ingest.env_defaults import EnvDefault
from aiscan.inventory.schema import (
    AgentRecord,
    AiDependency,
    AuthorisationSlot,
    DetectedValue,
    DetectionInfo,
    InventoryProvenance,
    McpRecord,
    ModelBinding,
    ModelUsage,
    OwnerSlot,
    PolicyEntry,
    PromptBinding,
    Record,
    SignatureInfo,
    StateEntry,
    ToolRecord,
    Verdict,
)
from aiscan.sinks.engine import path_location


def secret_findings_as_records(ctx: ScanContext) -> list[FindingRecord]:
    """Detected secret findings as record findings (SPEC-1 §5.4)."""
    findings = sorted(
        ctx.secret_findings,
        key=lambda f: (f.path, f.evidence, f.secret_kind),
    )
    return [
        FindingRecord(
            kind="secret_literal_redacted",
            evidence=(f.evidence,),
            detail=f.secret_kind,
        )
        for f in findings
    ]


def _agent_record_id(fact: AgentDefF) -> str:
    for prefix in ("agent:framework:", "agent:bespoke:"):
        if fact.id.startswith(prefix):
            return fact.id[len(prefix) :]
    return fact.id


def _tool_record_ids(graph: Graph) -> dict[str, str]:
    """tool fact id → record tool_id (short name; fq on collision)."""
    tools = [
        (nid, graph.nodes[nid])
        for nid in sorted(graph.nodes)
        if isinstance(graph.nodes[nid], ToolDefF)
    ]
    name_counts: dict[str, int] = {}
    for _, t in tools:
        assert isinstance(t, ToolDefF)
        name_counts[t.name] = name_counts.get(t.name, 0) + 1
    out: dict[str, str] = {}
    for nid, t in tools:
        assert isinstance(t, ToolDefF)
        out[nid] = (
            t.name
            if name_counts[t.name] == 1
            else nid.removeprefix("tool:")
        )
    return out


def _mcp_record_id(fact_id: str) -> str:
    return fact_id.removeprefix("mcp:")


def build_record(
    ctx: ScanContext,
    rulepacks: dict[str, str],
    graph: Graph | None = None,
    bom: BundleBom | None = None,
    *,
    no_ai_detected: bool = False,
    owner_hint: str | None = None,
    scanned_at: str | None = None,
    ai_dependencies: tuple[AiDependency, ...] = (),
    env_defaults: dict[str, tuple[EnvDefault, ...]] | None = None,
    contributor_candidates: tuple[str, ...] = (),
    ai_commit_range: dict[str, str] | None = None,
) -> Record:
    """Assemble the bundle record from the context, graph, and BOM."""
    # SPEC-10 §5a: store the org-pack basename, not an absolute path — the path
    # is non-deterministic across machines and a minor leak in a multi-tenant
    # context; the basename is all that identifies which pack was used.
    org_pack_ref = (
        ctx.settings.org_pack_path.name
        if ctx.settings.org_pack_path
        else None
    )

    agents: list[AgentRecord] = []
    tools: list[ToolRecord] = []
    mcp_servers: list[McpRecord] = []
    usages: list[ModelUsage] = []

    if graph is not None and bom is not None:
        tool_ids = _tool_record_ids(graph)
        agent_ids: dict[str, str] = {}

        for agent_bom in bom.agents:
            fact = graph.nodes[agent_bom.agent_id]
            if isinstance(fact, AgentDefF):
                agent_ids[agent_bom.agent_id] = _agent_record_id(fact)

        for agent_bom in bom.agents:
            fact = graph.nodes[agent_bom.agent_id]

            if not isinstance(fact, AgentDefF):
                continue

            model_binding: ModelBinding | None = None

            if agent_bom.model_ids:
                m = graph.nodes[agent_bom.model_ids[0]]

                if isinstance(m, ModelRefF):
                    model_binding = ModelBinding(
                        value=m.model,
                        method=m.method,
                        endpoint=m.endpoint,
                        api_style=m.api_style,
                        deployment=m.deployment,
                    )

            prompt_binding: PromptBinding | None = None

            if agent_bom.prompt_ids:
                p = graph.nodes[agent_bom.prompt_ids[0]]

                if isinstance(p, PromptDefF):
                    prompt_binding = PromptBinding(
                        value=p.content,
                        dynamic=p.dynamic,
                        origin=p.origin,
                        file_ref=p.file_ref,
                        evidence=p.evidence,
                    )

            states = tuple(
                StateEntry(
                    kind=s.kind,
                    evidence=s.evidence,
                )
                for sid in agent_bom.state_ids
                if isinstance(
                    s := graph.nodes[sid],
                    StateDefF,
                )
            )

            policies = tuple(
                PolicyEntry(
                    kind=pol.kind,
                    params=pol.params,
                    evidence=pol.evidence,
                )
                for pid in agent_bom.policy_ids
                if isinstance(
                    pol := graph.nodes[pid],
                    PolicyDefF,
                )
            )

            handoffs = tuple(
                sorted(
                    agent_ids.get(e.dst, e.dst)
                    for e in agent_bom.transfers_out
                    if e.type == "transfer:handoff"
                )
            )
            routes = tuple(
                sorted(
                    agent_ids.get(e.dst, e.dst)
                    for e in agent_bom.transfers_out
                    if e.type == "transfer:route"
                )
            )

            agents.append(
                AgentRecord(
                    agent_id=agent_ids[agent_bom.agent_id],
                    detection=DetectionInfo(
                        method=fact.method,
                        confidence=fact.confidence,
                        evidence=fact.evidence,
                    ),
                    location=fact.location,
                    language=fact.language,
                    model=model_binding,
                    system_prompt=prompt_binding,
                    tools=tuple(
                        sorted(
                            tool_ids[t]
                            for t in agent_bom.tool_ids
                        )
                    ),
                    mcp_servers=tuple(
                        sorted(
                            _mcp_record_id(m)
                            for m in agent_bom.mcp_ids
                        )
                    ),
                    state=states,
                    control_policies=policies,
                    handoffs=handoffs,
                    routes=routes,
                )
            )

        agents.sort(key=lambda a: a.agent_id)

        for nid in sorted(graph.nodes):
            f = graph.nodes[nid]

            if isinstance(f, ToolDefF):
                auth = None

                if f.auth_verb and isinstance(
                    f.external_target,
                    str,
                ):
                    auth = f"{f.auth_verb} {f.external_target}"

                tools.append(
                    ToolRecord(
                        tool_id=tool_ids[nid],
                        kind=f.kind,
                        location=path_location(f.evidence[0].rsplit(":", 1)[0]),
                        signature=(
                            SignatureInfo(
                                params=f.signature.params,
                                returns=f.signature.returns,
                            )
                            if f.signature
                            else None
                        ),
                        side_effects=f.side_effects,
                        external_target=f.external_target,
                        credential_ref=f.credential_ref,
                        declared_authorisation=AuthorisationSlot(
                            value=auth,
                        ),
                        evidence=f.evidence,
                    )
                )

            elif isinstance(f, MCPDefF):
                mcp_servers.append(
                    McpRecord(
                        server_id=_mcp_record_id(nid),
                        server=f.server,
                        transport=f.transport,
                        declared_tools=f.declared_tools,
                        approval_policy=f.approval_policy,
                        evidence=f.evidence,
                    )
                )

            elif isinstance(f, LLMCallSiteF):
                mf = graph.nodes.get(f.model_id)

                usages.append(
                    ModelUsage(
                        model=DetectedValue(
                            value=(
                                mf.model
                                if isinstance(mf, ModelRefF)
                                else None
                            ),
                            method=(
                                mf.method
                                if isinstance(mf, ModelRefF)
                                else None
                            ),
                            confidence=f.confidence,
                            evidence=f.evidence,
                        ),
                        task=f.task,
                        endpoint=(mf.endpoint if isinstance(mf, ModelRefF) else None),
                        evidence=f.evidence,
                        in_agent=f.in_agent,
                    )
                )

        tools.sort(key=lambda t: t.tool_id)
        mcp_servers.sort(key=lambda m: m.server_id)
        usages.sort(key=lambda u: u.evidence)

    # Findings: secret redactions + process findings (SPEC-1 §5.4).
    findings = secret_findings_as_records(ctx) + sorted(
        ctx.analysis_findings, key=lambda f: (f.kind, f.evidence)
    )
    ctx.health.findings = len(findings)

    # SPEC-3 §2.1: three-way verdict. Detected = any agent, sink, model usage,
    # or MCP server; triage fired without any of those = signals only.
    verdict: Verdict
    if no_ai_detected:
        verdict = "no_ai"
    elif agents or usages or mcp_servers or sum(ctx.health.sinks.values()):
        verdict = "ai_detected"
    else:
        verdict = "ai_signals_only"

    return Record(
        bundle_id=f"repo:{ctx.bundle_name}",
        name=ctx.bundle_name,
        repo_url=ctx.repo_url,
        scanned_commit=ctx.commit,
        ai_verdict=verdict,
        no_ai_detected=verdict == "no_ai",
        owner=OwnerSlot(candidate=owner_hint),
        ai_dependencies=ai_dependencies,
        env_defaults=env_defaults or {},
        contributor_candidates=contributor_candidates,
        ai_commit_range=ai_commit_range,
        agents=tuple(agents),
        tools=tuple(tools),
        mcp_servers=tuple(mcp_servers),
        model_usages=tuple(usages),
        findings=tuple(findings),
        scan_health=ctx.health,
        inventory_provenance=InventoryProvenance(
            scanner=f"aiscan {aiscan.__version__}",
            rulepacks=dict(sorted(rulepacks.items())),
            org_pack=org_pack_ref,
            scanned_at=(
                scanned_at
                or datetime.now(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        ),
    )


def facts_jsonl(facts: list[AnyFact]) -> list[str]:
    """One canonical JSON line per fact, sorted for byte-stable output."""
    lines = []

    for fact in facts:
        payload = {
            "fact_type": type(fact).__name__,
            **fact.model_dump(mode="json"),
        }

        lines.append(
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
            )
        )

    return sorted(set(lines))


def write_record_artifacts(out_dir: Path, record: Record) -> None:
    """Write record.json, scan_health.json, inventory.html, report.html
    (detection artifacts untouched — this is also the enrich-in-place writer,
    SPEC-3 §7.3). SPEC-6: inventory.html is the record; report.html is the
    same record body + technical annex."""
    from aiscan.report import (  # local import: report depends on schema
        render_inventory,
        render_report,
    )
    from aiscan.report.graph_svg import render_graph_svg

    out_dir.mkdir(parents=True, exist_ok=True)

    graph_svg = render_graph_svg(record) if record.ai_verdict == "ai_detected" else None
    (out_dir / "inventory.html").write_text(
        render_inventory(record, graph_svg=graph_svg or None),
        encoding="utf-8",
    )
    (out_dir / "report.html").write_text(
        render_report(record, graph_svg=graph_svg or None),
        encoding="utf-8",
    )

    (out_dir / "record.json").write_text(
        canonical_json(
            record.model_dump(mode="json")
        ),
        encoding="utf-8",
    )

    (out_dir / "scan_health.json").write_text(
        canonical_json(
            record.scan_health.model_dump(mode="json")
        ),
        encoding="utf-8",
    )


def write_artifacts(
    out_dir: Path,
    record: Record,
    fact_lines: list[str],
    graph: Graph | None = None,
    analysis_finding_lines: list[str] | None = None,
    entrypoint_marks: list[tuple[str, str, str]] | None = None,
) -> None:
    """Write record.json, graph.json, facts.jsonl, scan_health.json, report.html.

    ``analysis_finding_lines`` and ``entrypoint_marks`` (SPEC-8) persist the raw
    analysis-stage findings and the (framework, agent, module) entrypoint marks
    separately from the graded record, so an incremental rescan can carry forward
    those belonging to unaffected modules."""
    write_record_artifacts(out_dir, record)

    facts = "".join(
        line + "\n"
        for line in fact_lines
    )

    (out_dir / "facts.jsonl").write_text(
        facts,
        encoding="utf-8",
    )

    (out_dir / "analysis_findings.jsonl").write_text(
        "".join(line + "\n" for line in (analysis_finding_lines or [])),
        encoding="utf-8",
    )

    (out_dir / "entrypoint_marks.jsonl").write_text(
        "".join(
            json.dumps(list(m), ensure_ascii=False) + "\n"
            for m in sorted(entrypoint_marks or [])
        ),
        encoding="utf-8",
    )

    if graph is not None:
        (out_dir / "graph.json").write_text(
            graph_canonical_json(graph),
            encoding="utf-8",
        )
