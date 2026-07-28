"""Derived-indicator engine (SPEC-3 §3).

Pure function of ``(record, org pack, host registry)`` — deterministic,
no LLM, runs at record-emission time before enrichment. Every derived field
carries ``{value, source: "derived", evidence}`` where evidence is what it
aggregates. Nothing is ever guessed: a flag whose inputs are absent (e.g.
``moves_money`` without ``org.payment_hosts``) is computed false, and
endpoint classification only reads *literal* URLs — symbolic/template
endpoints classify as ``unknown``.
"""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from aiscan.context import OrgPack
from aiscan.derive.inventory import (
    EnvDefaults,
    build_models_used,
    data_signals_of,
    group_usages,
    system_type_of,
)
from aiscan.facts.models import FindingRecord
from aiscan.inventory.schema import (
    AgentRecord,
    DerivedBlock,
    DerivedValue,
    McpRecord,
    ModelUsage,
    Record,
    ToolRecord,
)
from aiscan.ir.values import JsonRepr
from aiscan.parse.registry import PIPELINE_EXTS
from aiscan.sinks.attribution import HostRegistry
from aiscan.sinks.engine import is_test_path, path_location

Severity = Literal["high", "medium", "low", "info"]

# SPEC-3 §3.3 fixed kind → severity map (report sorts by it).
# Kind -> severity, the single source graded by ``_grade``. Kinds whose severity
# is *conditional* (unanalysed_language_code, declared_agent_artefact) are set
# inline at their one creation site and so are intentionally absent here.
SEVERITY: dict[str, Severity] = {
    "secret_literal_redacted": "high",
    "unapproved_gateway": "high",
    "high_privilege_agent": "high",
    "unresolved_model": "medium",
    "unresolved_tools": "medium",
    "dormant_ai_wrapper": "info",
    "dynamic_prompt": "medium",
    "suspected_llm_call": "low",
    "ambiguous_agent_shape": "low",
    "adjudication_unavailable": "low",
    "llm_call_in_test_or_main": "info",
    "orphan_model_usage": "info",
}

_PRIVILEGE_FLAGS = ("moves_money", "executes_code", "mutates_identities")


def _literal_host(endpoint: JsonRepr) -> str | None:
    """Hostname of a *literal* endpoint URL; anything symbolic → None."""
    if not isinstance(endpoint, str):
        return None
    return urlparse(endpoint).hostname


def _is_private_host(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _provider_class(endpoint: JsonRepr, registry: HostRegistry) -> str:
    """SPEC-3 §3.1: vendor_external | internal_gateway | self_hosted | unknown.

    No endpoint at all means the SDK's default vendor host — vendor_external.
    """
    if endpoint is None:
        return "vendor_external"
    host = _literal_host(endpoint)
    if host is None:
        return "unknown"
    if host in registry.gateway_hosts:
        return "internal_gateway"
    provider = registry.provider_for(host)
    if provider is not None and provider != "org-gateway":
        return "vendor_external"
    if _is_private_host(host):
        return "self_hosted"
    return "unknown"


def _endpoint_unapproved(endpoint: JsonRepr, registry: HostRegistry) -> bool:
    """True when a literal endpoint's host is outside hosts.yaml + gateway_hosts."""
    host = _literal_host(endpoint)
    return host is not None and not registry.is_known(host)


def _flag_dict() -> dict[str, object]:
    return {
        "moves_money": False,
        "executes_code": False,
        "mutates_identities": False,
        "sends_external": False,
        "reads_sensitive": False,
    }


def _is_unresolved(value: JsonRepr) -> bool:
    return isinstance(value, dict) and "unresolved" in value


def _agent_flags(
    agent: AgentRecord, tools_by_id: dict[str, ToolRecord], org: OrgPack
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Capability flags over one agent's tools + the evidence spans behind them."""
    flags = _flag_dict()
    evidence: list[str] = []
    for tool_id in agent.tools:
        tool = tools_by_id.get(tool_id)
        if tool is None:
            continue
        target = tool.external_target if isinstance(tool.external_target, str) else None
        hit = False
        if "code_exec" in tool.side_effects:
            flags["executes_code"] = hit = True
        if "admin_mutation" in tool.side_effects:
            flags["mutates_identities"] = hit = True
        if "external_send" in tool.side_effects:
            flags["sends_external"] = hit = True
            if target is not None and target in org.payment_hosts:
                flags["moves_money"] = True
        if "read" in tool.side_effects and target is not None and target in org.sensitive_hosts:
            flags["reads_sensitive"] = hit = True
        if hit:
            evidence.extend(tool.evidence)
    return flags, tuple(sorted(set(evidence)))


def _reachable_tools(agent: AgentRecord, agents_by_id: dict[str, AgentRecord]) -> tuple[str, ...]:
    """Transitive tool set via handoff/route edges (cycle-safe blast radius)."""
    seen_agents: set[str] = set()
    tools: set[str] = set()
    stack = [agent.agent_id]
    while stack:
        current = stack.pop()
        if current in seen_agents:
            continue
        seen_agents.add(current)
        a = agents_by_id.get(current)
        if a is None:
            continue
        tools.update(a.tools)
        stack.extend(a.handoffs)
        stack.extend(a.routes)
    return tuple(sorted(tools))


def _derive_agent(
    agent: AgentRecord,
    agents_by_id: dict[str, AgentRecord],
    in_handoffs: set[str],
    tools_by_id: dict[str, ToolRecord],
    org: OrgPack,
) -> AgentRecord:
    if agent.routes:
        role = "router"
    elif agent.handoffs:
        role = "supervisor"
    elif agent.agent_id in in_handoffs:
        role = "worker"
    else:
        role = "solo"
    # Supervisor wins ties (SPEC-3 §3.2).
    if agent.handoffs:
        role = "supervisor"

    approvals = tuple(p for p in agent.control_policies if p.kind == "approval")
    autonomy = "approval_gated" if approvals else "autonomous"
    autonomy_evidence = tuple(e for p in approvals for e in p.evidence) or agent.detection.evidence

    flags, flag_evidence = _agent_flags(agent, tools_by_id, org)

    return agent.model_copy(
        update={
            "role_class": DerivedValue(value=role, evidence=agent.detection.evidence),
            "autonomy_level": DerivedValue(value=autonomy, evidence=autonomy_evidence),
            "capability_flags": DerivedValue(value=flags, evidence=flag_evidence),
            "reachable_tools": DerivedValue(
                value=list(_reachable_tools(agent, agents_by_id)),
                evidence=agent.detection.evidence,
            ),
        }
    )


def _derive_tool(tool: ToolRecord, org: OrgPack) -> ToolRecord:
    target = tool.external_target if isinstance(tool.external_target, str) else None
    sensitive = (target is not None and target in org.sensitive_hosts) or bool(
        {"admin_mutation", "code_exec"} & set(tool.side_effects)
    )
    return tool.model_copy(
        update={"is_sensitive": DerivedValue(value=sensitive, evidence=tool.evidence)}
    )


def _derive_mcp(mcp: McpRecord) -> McpRecord:
    if mcp.transport == "stdio":
        risk = "low"
    else:
        server_host = _literal_host(mcp.server) if isinstance(mcp.server, str) else None
        risk = "medium" if server_host is not None and _is_private_host(server_host) else "high"
    return mcp.model_copy(
        update={
            "approval_required": DerivedValue(
                value=mcp.approval_policy is not None, evidence=mcp.evidence
            ),
            "transport_risk": DerivedValue(value=risk, evidence=mcp.evidence),
        }
    )


def _derive_usage(usage: ModelUsage, registry: HostRegistry) -> ModelUsage:
    return usage.model_copy(
        update={
            "provider_class": DerivedValue(
                value=_provider_class(usage.endpoint, registry), evidence=usage.evidence
            )
        }
    )


def _grade(finding: FindingRecord) -> FindingRecord:
    severity = finding.severity or SEVERITY.get(finding.kind)
    # SPEC-7 sweep fix: a secret-shaped literal whose every evidence span lives
    # in a test/mock path is a dummy fixture — grade low, not high (OpenHands
    # produced 21 HIGH findings that were all test fixtures). Production-path
    # secrets keep their severity.
    if (
        finding.kind == "secret_literal_redacted"
        and severity == "high"
        and finding.evidence
        and all(is_test_path(e.rsplit(":", 1)[0]) for e in finding.evidence)
    ):
        severity = "low"
    if severity == finding.severity:
        return finding
    return finding.model_copy(update={"severity": severity}) if severity else finding


def _derived_findings(
    agents: list[AgentRecord], usages: list[ModelUsage], registry: HostRegistry
) -> list[FindingRecord]:
    found: list[FindingRecord] = []
    for agent in agents:
        ref = f"agent:{agent.agent_id}"
        endpoint = agent.model.endpoint if agent.model else None
        if _endpoint_unapproved(endpoint, registry):
            found.append(
                FindingRecord(
                    kind="unapproved_gateway",
                    evidence=agent.detection.evidence,
                    detail=(
                        "LLM endpoint host not in approved host list: "
                        f"{_literal_host(endpoint)}"
                    ),
                    subject_ref=ref,
                )
            )
        flags = agent.capability_flags.value if agent.capability_flags else None
        if isinstance(flags, dict):
            privileged = sorted(k for k in _PRIVILEGE_FLAGS if flags.get(k))
            if privileged and agent.location in ("production", "example"):
                found.append(
                    FindingRecord(
                        kind="high_privilege_agent",
                        evidence=agent.detection.evidence,
                        detail=f"agent capability flags: {', '.join(privileged)}",
                        subject_ref=ref,
                    )
                )
        if agent.model is not None and _is_unresolved(agent.model.value):
            found.append(
                FindingRecord(
                    kind="unresolved_model",
                    evidence=agent.detection.evidence,
                    detail="agent model could not be resolved statically",
                    subject_ref=ref,
                )
            )
        if (
            agent.system_prompt is not None
            and agent.system_prompt.dynamic
            and isinstance(flags, dict)
            and (flags.get("sends_external") or flags.get("moves_money"))
        ):
            found.append(
                FindingRecord(
                    kind="dynamic_prompt",
                    evidence=agent.system_prompt.evidence or agent.detection.evidence,
                    detail="dynamic system prompt on an agent with external-send tools",
                    subject_ref=ref,
                )
            )
    for usage in usages:
        if _endpoint_unapproved(usage.endpoint, registry):
            found.append(
                FindingRecord(
                    kind="unapproved_gateway",
                    evidence=usage.evidence,
                    detail=(
                        "LLM endpoint host not in approved host list: "
                        f"{_literal_host(usage.endpoint)}"
                    ),
                    subject_ref="model_usage",
                )
            )
        if not usage.in_agent and usage.task != "embedding":
            found.append(
                FindingRecord(
                    kind="orphan_model_usage",
                    evidence=usage.evidence,
                    detail="LLM call site outside any detected agent",
                    subject_ref="model_usage",
                )
            )
        if _is_unresolved(usage.model.value):
            found.append(
                FindingRecord(
                    kind="unresolved_model",
                    evidence=usage.evidence,
                    detail="call-site model could not be resolved statically",
                    subject_ref="model_usage",
                )
            )
    return found


def _system_block(
    agents: list[AgentRecord],
    tools: list[ToolRecord],
    mcp: list[McpRecord],
    usages: list[ModelUsage],
    registry: HostRegistry,
    env_defaults: EnvDefaults,
) -> DerivedBlock:
    counts_by_location = {"production": 0, "example": 0, "test": 0}
    for a in agents:
        counts_by_location[a.location] += 1
    by_location: dict[str, object] = dict(counts_by_location)

    tool_counts_by_location = {"production": 0, "example": 0, "test": 0}
    for t in tools:
        tool_counts_by_location[t.location] += 1
    tools_by_location: dict[str, object] = dict(tool_counts_by_location)

    counts_by_language: dict[str, int] = {}
    for a in agents:
        counts_by_language[a.language] = counts_by_language.get(a.language, 0) + 1
    by_language: dict[str, object] = dict(sorted(counts_by_language.items()))

    production = [a for a in agents if a.location == "production"]
    estate = [a for a in agents if a.location in ("production", "example")]

    autonomy: str | None = None
    if production:
        levels = {
            a.autonomy_level.value for a in production if a.autonomy_level is not None
        }
        autonomy = "approval_gated" if "approval_gated" in levels else "autonomous"

    flags = _flag_dict()
    flag_evidence: list[str] = []
    for a in estate:
        if a.capability_flags is None or not isinstance(a.capability_flags.value, dict):
            continue
        for key, val in a.capability_flags.value.items():
            if val and key in flags and not flags[key]:
                flags[key] = True
                flag_evidence.extend(a.capability_flags.evidence)

    endpoints: list[tuple[JsonRepr, tuple[str, ...]]] = [
        (a.model.endpoint, a.detection.evidence) for a in agents if a.model is not None
    ] + [(u.endpoint, u.evidence) for u in usages]
    unapproved_evidence = tuple(
        sorted({e for ep, ev in endpoints if _endpoint_unapproved(ep, registry) for e in ev})
    )

    dynamic_evidence = tuple(
        sorted(
            {
                e
                for a in agents
                if a.system_prompt is not None and a.system_prompt.dynamic
                for e in (a.system_prompt.evidence or a.detection.evidence)
            }
        )
    )
    unresolved_evidence = tuple(
        sorted(
            {
                e
                for a in agents
                if a.model is not None and _is_unresolved(a.model.value)
                for e in a.detection.evidence
            }
            | {e for u in usages if _is_unresolved(u.model.value) for e in u.evidence}
        )
    )

    # SPEC-6 §3.2: canonical, deduped models table — the same model reached
    # through many call sites is one inventory row.
    model_sources: list[tuple[JsonRepr, JsonRepr, str, str, str, str]] = []
    for a in agents:
        if a.model is not None:
            model_sources.append(
                (
                    a.model.value,
                    a.model.endpoint,
                    a.model.api_style,
                    _provider_class(a.model.endpoint, registry),
                    "chat",
                    a.location,
                )
            )
    for u in usages:
        u_path = u.evidence[0].rsplit(":", 1)[0] if u.evidence else ""
        # provider_class is already derived onto the usage; reuse it, falling
        # back for a not-yet-derived usage.
        u_provider = (
            str(u.provider_class.value)
            if u.provider_class
            else _provider_class(u.endpoint, registry)
        )
        model_sources.append(
            (
                u.model.value,
                u.endpoint,
                "unknown",
                u_provider,
                u.task,
                path_location(u_path),
            )
        )
    models_used: list[object] = list(build_models_used(model_sources, env_defaults))

    external = tuple(
        sorted(
            {
                t.external_target
                for t in tools
                if isinstance(t.external_target, str) and "external_send" in t.side_effects
            }
            | {
                t.external_target
                for t in tools
                if isinstance(t.external_target, str) and "read" in t.side_effects
            }
        )
    )

    def count(value: int) -> DerivedValue:
        return DerivedValue(value=value)

    return DerivedBlock(
        agent_count=count(len(agents)),
        tool_count=count(len(tools)),
        model_count=count(len(models_used)),
        mcp_count=count(len(mcp)),
        bespoke_agent_count=count(sum(1 for a in agents if "bespoke" in a.detection.method)),
        framework_agent_count=count(sum(1 for a in agents if "bespoke" not in a.detection.method)),
        agents_by_location=DerivedValue(value=by_location),
        tools_by_location=DerivedValue(value=tools_by_location),
        agents_by_language=DerivedValue(value=by_language),
        autonomy_profile=DerivedValue(value=autonomy),
        capability_flags=DerivedValue(value=flags, evidence=tuple(sorted(set(flag_evidence)))),
        has_unapproved_endpoint=DerivedValue(
            value=bool(unapproved_evidence), evidence=unapproved_evidence
        ),
        has_dynamic_prompts=DerivedValue(value=bool(dynamic_evidence), evidence=dynamic_evidence),
        has_unresolved_models=DerivedValue(
            value=bool(unresolved_evidence), evidence=unresolved_evidence
        ),
        models_used=DerivedValue(value=models_used),
        external_systems=DerivedValue(value=list(external)),
        system_type=DerivedValue(value=system_type_of(agents, usages)),
        usage_groups=DerivedValue(
            value=list[object](group_usages(usages, env_defaults))
        ),
        data_signals=DerivedValue(
            value=list[object](data_signals_of(tools, usages, agents))
        ),
    )


def derive_record(record: Record, org: OrgPack, registry: HostRegistry) -> Record:
    """Fill every [D derived] field and append/grade findings (SPEC-3 §3)."""
    tools_by_id = {t.tool_id: t for t in record.tools}
    agents_by_id = {a.agent_id: a for a in record.agents}
    in_handoffs = {dst for a in record.agents for dst in (*a.handoffs, *a.routes)}

    agents = [
        _derive_agent(a, agents_by_id, in_handoffs, tools_by_id, org) for a in record.agents
    ]
    tools = [_derive_tool(t, org) for t in record.tools]
    mcp = [_derive_mcp(m) for m in record.mcp_servers]
    usages = [_derive_usage(u, registry) for u in record.model_usages]

    findings = [_grade(f) for f in record.findings]
    # Grade the derived findings too — their severity now comes from the SEVERITY
    # map (the single source) rather than an inline literal at each creation site.
    findings.extend(_grade(f) for f in _derived_findings(agents, usages, registry))

    # SPEC-3/4 coverage honesty: source files in languages the pipeline cannot
    # analyse were skipped. With AI signals present that is a real coverage gap
    # (medium); without, it is context for the negative attestation (info).
    unanalysed = {
        ext: n
        for ext, n in record.scan_health.language_files.items()
        if ext not in PIPELINE_EXTS and n > 0
    }
    if unanalysed:
        detail = ", ".join(f"{n} {ext}" for ext, n in sorted(unanalysed.items()))
        findings.append(
            FindingRecord(
                kind="unanalysed_language_code",
                detail=(
                    f"{sum(unanalysed.values())} source files in unsupported "
                    f"languages not analysed: {detail}"
                ),
                severity="medium" if record.scan_health.triage else "info",
                subject_ref="system",
            )
        )

    findings.sort(key=lambda f: (f.kind, f.evidence, f.subject_ref or ""))

    return record.model_copy(
        update={
            "agents": tuple(agents),
            "tools": tuple(tools),
            "mcp_servers": tuple(mcp),
            "model_usages": tuple(usages),
            "findings": tuple(findings),
            "derived": _system_block(
                agents, tools, mcp, usages, registry, record.env_defaults
            ),
            "scan_health": record.scan_health.model_copy(
                update={"findings": len(findings)}
            ),
        }
    )
