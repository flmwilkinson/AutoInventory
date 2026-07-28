"""Embeddable scan core (SPEC-10 §1).

``scan(ScanRequest) -> ScanResult`` runs the whole detection pipeline over an
already-materialised source tree and RETURNS the live ``Record``/``Graph``/fact
lines it builds — it persists nothing and reads no configuration from the
environment. The caller (the CLI adapter today, a service worker later) decides
where the source comes from, how the result is stored, and how it is rendered.

This is a pure move of the former ``cli._run_pipeline``: same stages, same
order, same objects — the only change is that the terminal ``write_artifacts``
became a returned ``ScanResult``. Byte-identity with a from-disk scan is
therefore preserved, and the pipeline is now testable in memory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import aiscan
from aiscan.context import OrgPack, ScanContext, ScanHealth, Settings
from aiscan.derive import derive_record
from aiscan.facts.models import AgentDefF, FindingRecord
from aiscan.frontends.bespoke.agent_shape import analyze_shapes
from aiscan.frontends.bespoke.call_sites import emit_call_sites
from aiscan.frontends.bespoke.wrappers import WrapperAnalyzer, load_registry, save_registry
from aiscan.frontends.declared import declared_agent_findings, find_declared_agents
from aiscan.frontends.framework.engine import FrameworkEngine, load_packs
from aiscan.graph.build import build_graph
from aiscan.graph.model import Graph
from aiscan.graph.queries import bundle_bom
from aiscan.ingest.env_defaults import collect_env_defaults
from aiscan.ingest.triage import count_source_files, run_triage
from aiscan.inventory.bom import build_ai_bom
from aiscan.inventory.emit import build_record, facts_jsonl
from aiscan.inventory.owner import ai_provenance, owner_candidate
from aiscan.inventory.schema import Record
from aiscan.modules.graph import ModuleGraph, read_package_versions
from aiscan.modules.symbols import SymbolTable
from aiscan.modules.ts_graph import read_ts_config
from aiscan.parse.base import ParseErrorInfo
from aiscan.parse.registry import PIPELINE_EXTS, make_parser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import SinkEngine, load_host_registry

if TYPE_CHECKING:
    from aiscan.store import StateStore

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv"}


def discover_python_files(repo_root: Path) -> list[str]:
    """Repo-relative posix paths of all scannable .py files, sorted."""
    return _discover(repo_root, (".py",))


def discover_source_files(repo_root: Path) -> list[str]:
    """Repo-relative posix paths of every file the pipeline analyses
    (Python + TS/JS), sorted."""
    return _discover(repo_root, tuple(sorted(PIPELINE_EXTS)))


def _discover(repo_root: Path, exts: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    stack = [repo_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                stack.append(entry)
            elif entry.is_file() and name.endswith(exts):
                # Skip TS declaration files: type-only, never contain agents.
                if name.endswith(".d.ts"):
                    continue
                found.append(entry.relative_to(repo_root).as_posix())
    return sorted(found)


@dataclass(slots=True)
class ScanRequest:
    """Everything the core needs to scan a materialised source tree.

    The source is already fetched (``repo_root`` is a real directory); the core
    never clones or reads credentials from the environment. ``org_pack`` and
    ``settings`` are supplied pre-loaded by the caller. The two LLM-tier cache
    paths are caller-chosen (the CLI points them at the scan output dir; a
    service can point them elsewhere or leave enrichment's at ``None`` for no
    disk cache)."""

    repo_root: Path
    repo_url: str | None
    commit: str
    bundle_name: str
    settings: Settings
    org_pack: OrgPack
    logger: logging.Logger
    dirty: bool = False
    # LLM tiers (opt-in; call_fn injection lets a caller supply its own client).
    # llm_api_key is the caller-supplied credential — the core never reads it
    # from the environment (SPEC-10 §4); the CLI adapter resolves it and injects.
    adjudicate: bool = False
    adjudicate_call_fn: Callable[[str, str], str] | None = None
    adjudication_cache_path: Path | None = None
    enrich: bool = False
    enrich_call_fn: Callable[[str, str], str] | None = None
    enrichment_cache_path: Path | None = None
    enrich_tests: bool = False
    llm_api_key: str | None = None
    # Incremental analysis (SPEC-8): the prior manifest + the base commit to diff,
    # and the store the prior scan's cached facts are fetched from by (bundle,
    # commit). Without a store, incremental cannot load prior facts -> full scan.
    manifest: object | None = None
    base: str | None = None
    store: StateStore | None = None


@dataclass(slots=True)
class ScanResult:
    """The live products of a scan. The caller persists/renders these; the core
    itself writes nothing."""

    record: Record
    graph: Graph
    fact_lines: list[str]
    analysis_finding_lines: list[str] = field(default_factory=list)
    entrypoint_marks: list[tuple[str, str, str]] = field(default_factory=list)


class _IncrementalPlan:
    """The affected-module set and the cached facts/findings to carry forward
    for one incremental scan (SPEC-8). Present only when the gate cleared the
    scan for incremental analysis."""

    __slots__ = ("affected", "carried_facts", "carried_findings", "carried_marks")

    def __init__(
        self,
        affected: frozenset[str],
        carried_facts: list[object],
        carried_findings: list[object],
        carried_marks: list[tuple[str, str, str]],
    ) -> None:
        self.affected = affected
        self.carried_facts = carried_facts
        self.carried_findings = carried_findings
        self.carried_marks = carried_marks


def _plan_incremental(
    ctx: ScanContext,
    manifest: object | None,
    base: str | None,
    store: StateStore | None,
    module_graph: ModuleGraph,
    tables: dict[str, SymbolTable],
) -> _IncrementalPlan | None:
    """Decide full vs incremental and, if incremental, compute the affected
    modules and the cached facts/findings to reuse. Returns None (=> full scan)
    whenever anything is uncertain — failing safe toward a correct BOM."""
    from aiscan.incremental import (
        Manifest,
        affected_modules,
        build_dependency_edges,
        can_incremental,
        keep_unaffected_facts,
        keep_unaffected_findings,
        keep_unaffected_marks,
    )
    from aiscan.incremental.gate import deps_hash, org_pack_hash, tsconfig_hash
    from aiscan.ingest.git import changed_files
    from aiscan.modules.graph import detect_source_roots

    if not isinstance(manifest, Manifest) or store is None:
        return None
    base_commit = base or manifest.last_scanned_commit
    # SPEC-10 §I: the carried facts AND the manifest's global-input hashes are
    # both from last_scanned_commit, so incremental is only sound when the diff
    # base IS that commit (the common CI case: --base is the previously-scanned
    # push). An explicit --base pointing elsewhere — a PR base we hold no aligned
    # (facts, manifest) pair for — fails safe to a full scan; sound arbitrary-base
    # PR-incremental needs per-commit manifest storage (deferred to the DB backend).
    if base_commit != manifest.last_scanned_commit:
        ctx.logger.info(
            "full scan (base %s has no aligned fact cache; last-scanned is %s)",
            base_commit[:8],
            manifest.last_scanned_commit[:8],
        )
        return None
    diff = changed_files(ctx.repo_root, base_commit, ctx.commit, ctx.logger)
    rulepacks = {p.framework: p.version for p in load_packs()}
    decision = can_incremental(
        manifest,
        scanner_version=f"aiscan {aiscan.__version__}",
        rulepack_versions=rulepacks,
        current_deps_hash=deps_hash(ctx.repo_root),
        current_tsconfig_hash=tsconfig_hash(ctx.repo_root),
        current_org_hash=org_pack_hash(ctx.settings.org_pack_path),
        current_source_roots=detect_source_roots(sorted(module_graph.modules_by_path)),
        changed_files=diff or [],
        base_available=diff is not None,
    )
    if decision.mode != "incremental" or diff is None:
        ctx.logger.info("full scan (%s)", decision.reason)
        return None
    # A deleted source file cannot be mapped to a current module; escalate to a
    # full scan rather than risk a dangling cross-module reference.
    for path in diff:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in PIPELINE_EXTS and not (ctx.repo_root / path).is_file():
            ctx.logger.info("full scan (source file deleted: %s)", path)
            return None
    # The prior scan's facts are addressed by (bundle, commit) in the store, not
    # by a path baked into the manifest — the carried set is the one produced at
    # the diff base (guaranteed == last_scanned_commit above) (SPEC-10 §3/§I).
    cached = store.get_prior_facts(manifest.bundle, base_commit)
    if cached is None:
        ctx.logger.info("full scan (fact cache missing/unreadable)")
        return None
    changed_modules = {
        name
        for path in diff
        if (name := module_graph.name_by_path.get(path)) is not None
    }
    edges = build_dependency_edges(tables)
    affected = affected_modules(changed_modules, edges, tables)
    changed_set = set(diff)
    carried_facts = keep_unaffected_facts(
        cached.facts, affected, module_graph.name_by_path, changed_set
    )
    carried_findings = keep_unaffected_findings(
        cached.analysis_findings, affected, module_graph.name_by_path, changed_set
    )
    carried_marks = keep_unaffected_marks(cached.entrypoint_marks, set(affected))
    ctx.logger.info(
        "incremental: %d changed, %d affected module(s), %d/%d facts carried",
        len(diff),
        len(affected),
        len(carried_facts),
        len(cached.facts),
    )
    return _IncrementalPlan(
        frozenset(affected), carried_facts, list(carried_findings), carried_marks
    )


def scan(request: ScanRequest) -> ScanResult:
    """Run the detection pipeline over ``request.repo_root`` and return the
    record/graph/facts. Never executes repo code; never writes to disk (bar the
    caller-chosen LLM caches). Deterministic per commit."""
    ctx = ScanContext(
        settings=request.settings,
        org_pack=request.org_pack,
        repo_root=request.repo_root,
        repo_url=request.repo_url,
        commit=request.commit,
        bundle_name=request.bundle_name,
        logger=request.logger,
        health=ScanHealth(),
    )
    ctx.logger.info("scanning %s @ %s", ctx.repo_root, ctx.commit)

    t_triage = time.monotonic()
    triage_signals = run_triage(ctx.repo_root, ctx.org_pack)
    ctx.health.triage = {k: triage_signals[k] for k in sorted(triage_signals)}
    ctx.health.language_files = count_source_files(ctx.repo_root)
    ctx.health.stage_ms["triage"] = int((time.monotonic() - t_triage) * 1000)
    owner_hint = owner_candidate(ctx.repo_root, ctx.logger)
    if not triage_signals:
        ctx.logger.info("no AI signals found; emitting no_ai_detected stub")
        # SPEC-3 §2.2 LLM guard: a no_ai verdict makes zero network calls;
        # the flags leave a visible note instead of a silent no-op.
        if request.enrich:
            ctx.health.enrichment = {"status": "skipped", "reason": "no_ai verdict"}
            ctx.logger.info("enrichment skipped: no_ai verdict")
        if request.adjudicate:
            ctx.health.adjudication = {"status": "skipped", "reason": "no_ai verdict"}
            ctx.logger.info("adjudication skipped: no_ai verdict")
        packs = load_packs()
        versions = {p.framework: p.version for p in packs}
        record = build_record(
            ctx, versions, no_ai_detected=True, owner_hint=owner_hint
        )
        record = derive_record(record, ctx.org_pack, load_host_registry(ctx.org_pack))
        return ScanResult(record=record, graph=Graph(), fact_lines=[])

    t0 = time.monotonic()
    modules_by_path = {}
    for rel_path in discover_source_files(ctx.repo_root):
        ext = "." + rel_path.rsplit(".", 1)[-1].lower()
        parser = make_parser(ext, on_secret=ctx.secret_findings.append)
        if parser is None:
            continue
        try:
            source = (ctx.repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            ctx.health.parse_errors.append(
                ParseErrorInfo(path=rel_path, message=f"unreadable: {exc}")
            )
            continue
        result = parser.parse(rel_path, source)
        if isinstance(result, ParseErrorInfo):
            ctx.health.parse_errors.append(result)
            ctx.logger.info("parse error in %s: %s", rel_path, result.message)
        else:
            modules_by_path[rel_path] = result
            ctx.health.loc += result.loc
        ctx.health.files += 1
    ctx.health.stage_ms["parse"] = int((time.monotonic() - t0) * 1000)

    t1 = time.monotonic()
    versions = read_package_versions(ctx.repo_root)
    ts_config = read_ts_config(ctx.repo_root)
    module_graph = ModuleGraph(modules_by_path, versions, ts_config=ts_config)
    tables = module_graph.build_symbol_tables()
    imported_roots = frozenset(
        imp.module.split(".")[0]
        for mod in modules_by_path.values()
        for imp in mod.imports
        if imp.level == 0 and imp.module
    )
    ai_bom = build_ai_bom(
        ctx.health.triage,
        versions,
        imported_roots,
        ctx.org_pack,
        npm_versions=ts_config.npm_versions,
    )
    ctx.health.stage_ms["modules"] = int((time.monotonic() - t1) * 1000)

    plan = _plan_incremental(
        ctx, request.manifest, request.base, request.store, module_graph, tables
    )
    analyze_only = plan.affected if plan is not None else None

    t2 = time.monotonic()
    resolver = Resolver(module_graph, tables, ctx.settings.resolver, ctx.health.resolver)
    sink_engine = SinkEngine(resolver, ctx.org_pack, repo_root=ctx.repo_root)
    sink_result = sink_engine.scan_all(analyze_only)
    for sink in sink_result.sinks:
        ctx.health.sinks[sink.kind] = ctx.health.sinks.get(sink.kind, 0) + 1
    for site, score in sink_result.suspected:
        ctx.analysis_findings.append(
            FindingRecord(
                kind="suspected_llm_call",
                evidence=(str(site.call.span),),
                detail=f"shape_score={score}",
            )
        )
    ctx.health.stage_ms["sinks"] = int((time.monotonic() - t2) * 1000)

    t3 = time.monotonic()
    registry = load_registry(ctx.settings.org_registry_path)
    analyzer = WrapperAnalyzer(
        resolver, sink_engine, ctx.org_pack, registry, analyze_only=analyze_only
    )
    wrapper_result = analyzer.fixed_point(sink_result.sinks)
    all_sinks = [*sink_result.sinks, *wrapper_result.wrapper_sinks]
    if wrapper_result.wrapper_sinks:
        ctx.health.sinks["wrapper"] = len(wrapper_result.wrapper_sinks)
    ctx.health.wrapper_classifications = len(wrapper_result.used_wrappers)
    # Full scans own the wrapper registry; an incremental scan sees only the
    # affected wrappers, so it must not overwrite the full classification set
    # (which the affected modules' attribution reads from).
    if ctx.settings.org_registry_path is not None and plan is None:
        persisted = {
            fq: info
            for fq, info in wrapper_result.classified.items()
            if fq in wrapper_result.used_wrappers and info.source in ("derived", "registry")
        }
        if persisted:
            save_registry(ctx.settings.org_registry_path, persisted)
    all_sink_spans = frozenset(s.span for s in all_sinks)

    packs = load_packs()
    f1 = FrameworkEngine(
        resolver,
        sink_engine,
        packs,
        llm_sink_spans=all_sink_spans,
        sinks=tuple(all_sinks),
    )
    f1_result = f1.run(
        analyze_only=analyze_only,
        carried_facts=plan.carried_facts if plan is not None else None,  # type: ignore[arg-type]
        carried_entrypoint_marks=plan.carried_marks if plan is not None else None,
    )
    ctx.health.unpromoted_candidates = f1_result.unpromoted_candidates

    ctx.analysis_findings.extend(wrapper_result.findings)

    shape_result = analyze_shapes(
        all_sinks,
        resolver,
        sink_engine,
        ctx.org_pack,
        all_sink_spans,
        excluded_spans=frozenset(
            f1_result.consumed_sink_spans | wrapper_result.wrapper_def_spans
        ),
    )
    ctx.analysis_findings.extend(shape_result.findings)

    consumed = frozenset(
        f1_result.consumed_sink_spans
        | wrapper_result.wrapper_def_spans
        | shape_result.consumed_spans
    )
    usage_result = emit_call_sites(
        all_sinks, consumed, member_spans=frozenset(shape_result.member_spans)
    )
    ctx.analysis_findings.extend(usage_result.findings)
    if plan is not None:
        # Carry forward the unaffected modules' analysis findings (the affected
        # ones were just regenerated); derive re-grades the merged set globally.
        ctx.analysis_findings.extend(plan.carried_findings)  # type: ignore[arg-type]

    # SPEC-4 §8: declared-agent prompt rosters — surfaced, never invented.
    detected_names = {f.name for f in f1_result.facts if isinstance(f, AgentDefF)} | {
        f.name for f in shape_result.facts if isinstance(f, AgentDefF)
    }
    has_detection = bool(detected_names or all_sinks)
    roster = find_declared_agents(ctx.repo_root)
    if roster:
        ctx.health.declared_agents = sorted(roster)
        ctx.analysis_findings.extend(
            declared_agent_findings(roster, detected_names, ai_signals_only=not has_detection)
        )
    ctx.health.stage_ms["frontends"] = int((time.monotonic() - t3) * 1000)

    adjudicated_facts: list[object] = []
    rulepack_versions = dict(f1_result.rulepack_versions)
    if request.adjudicate:
        from aiscan.adjudicate.engine import Adjudicator

        if request.adjudication_cache_path is None:
            raise ValueError("adjudicate=True requires adjudication_cache_path")
        t_adj = time.monotonic()
        adjudicator = Adjudicator(
            repo_root=ctx.repo_root,
            cache_path=request.adjudication_cache_path,
            logger=ctx.logger,
            call_fn=request.adjudicate_call_fn,
            base_url=ctx.settings.adjudicate_base_url,
            model=ctx.settings.adjudicate_model,
            budget=ctx.settings.adjudicate_budget,
            api_key=request.llm_api_key,
        )
        adj = adjudicator.run(ctx.analysis_findings)
        adjudicated_facts = list(adj.facts)
        ctx.analysis_findings.extend(adj.findings)
        rulepack_versions["adjudication"] = adjudicator.model
        ctx.health.adjudication = {
            "status": "ok",
            "calls": adj.calls_made,
            "cache_hits": adj.cache_hits,
            "facts_added": len(adj.facts),
        }
        ctx.logger.info(
            "adjudication: %d calls, %d cache hits, %d facts added",
            adj.calls_made,
            adj.cache_hits,
            len(adj.facts),
        )
        ctx.health.stage_ms["adjudicate"] = int((time.monotonic() - t_adj) * 1000)

    t4 = time.monotonic()
    all_facts = [*f1_result.facts, *shape_result.facts, *usage_result.facts]
    all_facts.extend(adjudicated_facts)  # type: ignore[arg-type]
    build = build_graph(all_facts)
    bom = bundle_bom(build.graph)
    fact_lines = facts_jsonl(all_facts)
    # SPEC-5 §6: config-at-rest env candidates, kept only for env keys some
    # detected fact actually references (a candidate is not a detection).
    fact_blob = "\n".join(fact_lines)
    env_defaults = {
        key: entries
        for key, entries in collect_env_defaults(ctx.repo_root).items()
        if f"env:{key}" in fact_blob
    }
    # SPEC-6 §3.4: git-derived contributor/date candidates over AI-touching files.
    ai_files = sorted({f for fact in all_facts for f in fact.source_files})
    contributors, ai_commit_range = ai_provenance(ctx.repo_root, ai_files, ctx.logger)
    record = build_record(
        ctx,
        rulepack_versions,
        build.graph,
        bom,
        owner_hint=owner_hint,
        ai_dependencies=ai_bom,
        env_defaults=env_defaults,
        contributor_candidates=contributors,
        ai_commit_range=ai_commit_range,
    )
    record = derive_record(record, ctx.org_pack, load_host_registry(ctx.org_pack))
    ctx.health.stage_ms["emit"] = int((time.monotonic() - t4) * 1000)

    if request.enrich:
        from aiscan.enrich.engine import enrich_record

        t_enrich = time.monotonic()
        enriched = enrich_record(
            record,
            ctx.repo_root,
            ctx.logger,
            base_url=ctx.settings.adjudicate_base_url,
            model=ctx.settings.adjudicate_model,
            cache_path=request.enrichment_cache_path,
            call_fn=request.enrich_call_fn,
            include_tests=request.enrich_tests,
            api_key=request.llm_api_key,
        )
        record = enriched.record
        ctx.health.stage_ms["enrich"] = int((time.monotonic() - t_enrich) * 1000)
        ctx.logger.info(
            "enrichment: %d nodes, %d drafted, %d grounded_false, %d failed",
            enriched.nodes,
            enriched.drafted,
            enriched.grounded_false,
            enriched.failed,
        )

    from aiscan.incremental.factcache import analysis_findings_jsonl

    ctx.logger.info(
        "scan complete: %d files, %d loc, %d agents, %d sinks, %d parse errors",
        ctx.health.files,
        ctx.health.loc,
        len(record.agents),
        len(sink_result.sinks),
        len(ctx.health.parse_errors),
    )
    return ScanResult(
        record=record,
        graph=build.graph,
        fact_lines=fact_lines,
        analysis_finding_lines=analysis_findings_jsonl(ctx.analysis_findings),
        entrypoint_marks=f1_result.entrypoint_marks,
    )
