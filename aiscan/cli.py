"""``aiscan`` CLI (SPEC §1): thin orchestration over the pipeline stages."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

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
from aiscan.incremental.gate import org_pack_hash, should_skip
from aiscan.incremental.manifest import manifest_path, read_manifest
from aiscan.ingest.env_defaults import collect_env_defaults
from aiscan.ingest.git import IngestError, ingest
from aiscan.ingest.triage import count_source_files, run_triage
from aiscan.inventory.bom import build_ai_bom
from aiscan.inventory.emit import build_record, facts_jsonl, write_artifacts
from aiscan.inventory.owner import ai_provenance, owner_candidate
from aiscan.modules.graph import ModuleGraph, read_package_versions
from aiscan.modules.symbols import SymbolTable
from aiscan.modules.ts_graph import read_ts_config
from aiscan.parse.base import ParseErrorInfo
from aiscan.parse.registry import PIPELINE_EXTS, make_parser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import SinkEngine, load_host_registry

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            },
            sort_keys=True,
        )


def _make_logger(json_logs: bool) -> logging.Logger:
    logger = logging.getLogger("aiscan")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    stream = logging.StreamHandler(sys.stderr)
    if json_logs:
        stream.setFormatter(_JsonFormatter())
    else:
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(stream)
    return logger


def _attach_log_file(logger: logging.Logger, path: Path, json_logs: bool) -> logging.FileHandler:
    handler = logging.FileHandler(path, encoding="utf-8")
    if json_logs:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return handler


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


def run_scan(
    target: str,
    commit: str | None = None,
    out: Path | None = None,
    org_pack: Path | None = None,
    json_logs: bool = False,
    settings: Settings | None = None,
    adjudicate: bool = False,
    adjudicate_call_fn: Callable[[str, str], str] | None = None,
    enrich: bool = False,
    enrich_call_fn: Callable[[str, str], str] | None = None,
    enrich_tests: bool = False,
    repo: Path | None = None,
    base: str | None = None,
    full: bool = False,
) -> Path:
    """Run a scan; returns the artifact directory. Never executes repo code.

    ``base`` names the commit to diff against for incremental analysis (defaults
    to the bundle's last-scanned commit); ``full`` forces a from-scratch scan and
    ignores any cache (SPEC-8)."""
    target_path = Path(target)
    if (target_path / "record.json").is_file():
        # SPEC-3 §7.3 enrich-in-place: the target is a prior scan output.
        return _enrich_in_place(
            target_path,
            repo=repo,
            enrich=enrich,
            enrich_call_fn=enrich_call_fn,
            enrich_tests=enrich_tests,
            json_logs=json_logs,
            settings=settings,
        )
    needs_env = (adjudicate and adjudicate_call_fn is None) or (
        enrich and enrich_call_fn is None
    )
    if needs_env and settings is None:
        # Populate LLM credentials from ./.env before reading Settings.
        from aiscan.env_file import load_dotenv

        load_dotenv(Path(".env"))
    settings = settings or Settings.load()
    if org_pack is not None:
        settings = settings.model_copy(update={"org_pack_path": org_pack})
    logger = _make_logger(json_logs or settings.json_logs)
    out_root = (out or settings.out_dir).resolve()
    if settings.org_registry_path is None:
        settings = settings.model_copy(
            update={"org_registry_path": out_root / "org_registry.json"}
        )

    ingest_result = ingest(target, commit, out_root / "_clones", logger)
    commit_tag = ingest_result.commit[:8] if ingest_result.commit else "unknown"
    scan_out = out_root / f"{ingest_result.bundle_name}-{commit_tag}"

    # SPEC-8: cache gate. The manifest records the last-scanned commit + every
    # input whose change would invalidate the cache.
    cache_dir = out_root / "_cache"
    mpath = manifest_path(cache_dir, ingest_result.bundle_name)
    # A dirty local tree is not addressable by HEAD: never skip, never cache.
    cacheable = not full and not ingest_result.dirty
    manifest = read_manifest(mpath) if cacheable else None
    scanner_version = f"aiscan {aiscan.__version__}"
    rulepack_versions = {p.framework: p.version for p in load_packs()}
    org_digest = org_pack_hash(settings.org_pack_path)
    if manifest is not None and should_skip(
        manifest,
        head_commit=ingest_result.commit,
        scanner_version=scanner_version,
        rulepack_versions=rulepack_versions,
        org_pack_digest=org_digest,
        scan_out_dir=Path(manifest.scan_out_dir),
    ):
        logger.info(
            "commit %s already scanned with identical inputs; skipping (%s)",
            commit_tag,
            manifest.scan_out_dir,
        )
        return Path(manifest.scan_out_dir)

    scan_out.mkdir(parents=True, exist_ok=True)
    log_handler = _attach_log_file(logger, scan_out / "scan.log", json_logs or settings.json_logs)
    try:
        ctx = ScanContext(
            settings=settings,
            org_pack=OrgPack.load(settings.org_pack_path),
            repo_root=ingest_result.repo_root,
            repo_url=ingest_result.repo_url,
            commit=ingest_result.commit,
            bundle_name=ingest_result.bundle_name,
            logger=logger,
            health=ScanHealth(),
        )
        logger.info(
            "scanning %s @ %s -> %s", ingest_result.repo_root, ingest_result.commit, scan_out
        )
        _run_pipeline(
            ctx,
            scan_out,
            adjudicate=adjudicate,
            adjudicate_call_fn=adjudicate_call_fn,
            enrich=enrich,
            enrich_call_fn=enrich_call_fn,
            enrich_tests=enrich_tests,
            manifest=manifest,
            base=base,
        )
        if not ingest_result.dirty:
            _write_scan_manifest(
                mpath,
                ctx,
                scan_out=scan_out,
                scanner_version=scanner_version,
                rulepack_versions=rulepack_versions,
                org_digest=org_digest,
            )
        return scan_out
    finally:
        logger.removeHandler(log_handler)
        log_handler.close()


def _write_scan_manifest(
    mpath: Path,
    ctx: ScanContext,
    *,
    scan_out: Path,
    scanner_version: str,
    rulepack_versions: dict[str, str],
    org_digest: str,
) -> None:
    """Record this scan's inputs so the next run can skip / go incremental."""
    from aiscan.incremental import ANALYSIS_VERSION, Manifest, write_manifest
    from aiscan.incremental.gate import deps_hash, tsconfig_hash
    from aiscan.modules.graph import detect_source_roots

    if ctx.commit in ("", "unknown", "unversioned"):
        return  # only commit-addressable scans are cache-safe
    roots = detect_source_roots(discover_source_files(ctx.repo_root))
    write_manifest(
        mpath,
        Manifest(
            bundle=ctx.bundle_name,
            last_scanned_commit=ctx.commit,
            scan_out_dir=str(scan_out),
            scanner_version=scanner_version,
            analysis_version=ANALYSIS_VERSION,
            rulepack_versions=rulepack_versions,
            source_roots=roots,
            deps_hash=deps_hash(ctx.repo_root),
            tsconfig_hash=tsconfig_hash(ctx.repo_root),
            org_pack_hash=org_digest,
        ),
    )


def _enrich_in_place(
    out_dir: Path,
    *,
    repo: Path | None,
    enrich: bool,
    enrich_call_fn: Callable[[str, str], str] | None,
    enrich_tests: bool,
    json_logs: bool,
    settings: Settings | None,
) -> Path:
    """Re-enrich an existing scan output (SPEC-3 §7.3): rewrites record.json +
    report.html only; graph.json/facts.jsonl are never touched."""
    from aiscan.enrich.engine import enrich_record
    from aiscan.inventory.emit import write_record_artifacts
    from aiscan.inventory.schema import Record

    if not enrich:
        raise IngestError(
            f"{out_dir} is a prior scan output; pass --enrich to re-enrich it"
        )
    if settings is None and enrich_call_fn is None:
        from aiscan.env_file import load_dotenv

        load_dotenv(Path(".env"))
    settings = settings or Settings.load()
    logger = _make_logger(json_logs or settings.json_logs)
    log_handler = _attach_log_file(logger, out_dir / "scan.log", json_logs or settings.json_logs)
    try:
        record = Record.model_validate_json(
            (out_dir / "record.json").read_text(encoding="utf-8")
        )
        if record.ai_verdict == "no_ai":
            logger.info("enrich-in-place skipped: no_ai verdict")
            record = record.model_copy(
                update={
                    "scan_health": record.scan_health.model_copy(
                        update={"enrichment": {"status": "skipped", "reason": "no_ai verdict"}}
                    )
                }
            )
            write_record_artifacts(out_dir, record)
            return out_dir
        # Slices need the source tree; without --repo we degrade to facts-only
        # grounding, recorded honestly in scan_health.enrichment.grounding.
        repo_root = repo if repo is not None else out_dir / "__no_source_tree__"
        enriched = enrich_record(
            record,
            repo_root,
            logger,
            base_url=settings.adjudicate_base_url,
            model=settings.adjudicate_model,
            cache_path=out_dir / "enrichment_cache.json",
            call_fn=enrich_call_fn,
            include_tests=enrich_tests,
        )
        write_record_artifacts(out_dir, enriched.record)
        logger.info(
            "enrich-in-place: %d nodes, %d drafted, %d grounded_false, %d failed (%s)",
            enriched.nodes,
            enriched.drafted,
            enriched.grounded_false,
            enriched.failed,
            enriched.grounding,
        )
        return out_dir
    finally:
        logger.removeHandler(log_handler)
        log_handler.close()


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
        load_cached_analysis,
    )
    from aiscan.incremental.gate import deps_hash, org_pack_hash, tsconfig_hash
    from aiscan.ingest.git import changed_files
    from aiscan.modules.graph import detect_source_roots

    if not isinstance(manifest, Manifest):
        return None
    base_commit = base or manifest.last_scanned_commit
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
    cached = load_cached_analysis(Path(manifest.scan_out_dir))
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


def _run_pipeline(
    ctx: ScanContext,
    scan_out: Path,
    adjudicate: bool = False,
    adjudicate_call_fn: Callable[[str, str], str] | None = None,
    enrich: bool = False,
    enrich_call_fn: Callable[[str, str], str] | None = None,
    enrich_tests: bool = False,
    manifest: object | None = None,
    base: str | None = None,
) -> None:
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
        if enrich:
            ctx.health.enrichment = {"status": "skipped", "reason": "no_ai verdict"}
            ctx.logger.info("enrichment skipped: no_ai verdict")
        if adjudicate:
            ctx.health.adjudication = {"status": "skipped", "reason": "no_ai verdict"}
            ctx.logger.info("adjudication skipped: no_ai verdict")
        packs = load_packs()
        versions = {p.framework: p.version for p in packs}
        record = build_record(
            ctx, versions, no_ai_detected=True, owner_hint=owner_hint
        )
        record = derive_record(record, ctx.org_pack, load_host_registry(ctx.org_pack))
        write_artifacts(scan_out, record, fact_lines=[], graph=Graph())
        return

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

    plan = _plan_incremental(ctx, manifest, base, module_graph, tables)
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
    if adjudicate:
        from aiscan.adjudicate.engine import Adjudicator

        t_adj = time.monotonic()
        adjudicator = Adjudicator(
            repo_root=ctx.repo_root,
            cache_path=scan_out / "adjudication_cache.json",
            logger=ctx.logger,
            call_fn=adjudicate_call_fn,
            base_url=ctx.settings.adjudicate_base_url,
            model=ctx.settings.adjudicate_model,
            budget=ctx.settings.adjudicate_budget,
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

    if enrich:
        from aiscan.enrich.engine import enrich_record

        t_enrich = time.monotonic()
        enriched = enrich_record(
            record,
            ctx.repo_root,
            ctx.logger,
            base_url=ctx.settings.adjudicate_base_url,
            model=ctx.settings.adjudicate_model,
            cache_path=scan_out / "enrichment_cache.json",
            call_fn=enrich_call_fn,
            include_tests=enrich_tests,
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

    write_artifacts(
        scan_out,
        record,
        fact_lines,
        build.graph,
        analysis_finding_lines=analysis_findings_jsonl(ctx.analysis_findings),
        entrypoint_marks=f1_result.entrypoint_marks,
    )
    ctx.logger.info(
        "scan complete: %d files, %d loc, %d agents, %d sinks, %d parse errors",
        ctx.health.files,
        ctx.health.loc,
        len(record.agents),
        len(sink_result.sinks),
        len(ctx.health.parse_errors),
    )


def scan(
    target: Annotated[
        str | None,
        typer.Argument(help="Local path, git URL, or prior scan output to scan/enrich"),
    ] = None,
    commit: Annotated[str | None, typer.Option("--commit", help="Commit SHA")] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            help="Commit to diff against for incremental analysis "
            "(defaults to the bundle's last-scanned commit; CI passes the pre-push SHA)",
        ),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Force a full scan, ignoring any cache/manifest"),
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Artifact directory")] = None,
    org_pack: Annotated[
        Path | None, typer.Option("--org-pack", help="Org tailoring pack (YAML)")
    ] = None,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="JSON log lines")] = False,
    adjudicate: Annotated[
        bool,
        typer.Option(
            "--adjudicate",
            help="Enable the bounded LLM adjudication tier "
            "(reads OPENAI_API_KEY + AISCAN_ADJUDICATE_* from ./.env or the environment)",
        ),
    ] = False,
    enrich: Annotated[
        bool,
        typer.Option(
            "--enrich",
            help="Draft LLM [E] summary fields for the record "
            "(same OpenAI-compatible credentials as --adjudicate)",
        ),
    ] = False,
    enrich_tests: Annotated[
        bool,
        typer.Option(
            "--enrich-tests",
            help="Also enrich test-suite agents/tools (skipped by default so the "
            "budget goes to the production estate)",
        ),
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Source tree for enrichment slices when the target is a prior "
            "scan output (enrich-in-place mode)",
        ),
    ] = None,
    rebuild_dataset: Annotated[
        Path | None,
        typer.Option(
            "--rebuild-dataset",
            help="Rebuild the SQLite+CSV inventory dataset from every "
            "record.json under this directory, then exit",
        ),
    ] = None,
    repos: Annotated[
        Path | None,
        typer.Option(
            "--repos",
            help="Fleet mode: scan every repo in this list file (one git URL or "
            "path per line, # comments) into one dataset + estate index.html",
        ),
    ] = None,
) -> None:
    """Statically scan a repository for AI/agent usage and emit inventory artifacts.

    The target may also be a prior scan output directory (containing
    record.json) together with --enrich: enrichment then runs in place without
    rescanning, rewriting record.json + report.html only.
    """
    if rebuild_dataset is not None:
        from aiscan.dataset import rebuild_dataset as _rebuild

        count = _rebuild(rebuild_dataset, rebuild_dataset)
        typer.echo(f"dataset rebuilt from {count} records -> {rebuild_dataset / 'inventory.db'}")
        return
    if repos is not None:
        from aiscan.fleet import run_fleet

        fleet = run_fleet(
            repos,
            out or Path("aiscan-fleet"),
            org_pack=org_pack,
            enrich=enrich,
            json_logs=json_logs,
            logger=_make_logger(json_logs),
        )
        typer.echo(
            f"fleet: {len(fleet.scanned)} scanned, {len(fleet.failed)} failed -> "
            f"{fleet.out / 'index.html'}"
        )
        raise typer.Exit(1 if not fleet.scanned else 0)
    if target is None:
        typer.echo(
            "error: provide a target to scan, --repos FILE, or --rebuild-dataset DIR",
            err=True,
        )
        raise typer.Exit(2)
    try:
        scan_out = run_scan(
            target,
            commit=commit,
            out=out,
            org_pack=org_pack,
            json_logs=json_logs,
            adjudicate=adjudicate,
            enrich=enrich,
            enrich_tests=enrich_tests,
            repo=repo,
            base=base,
            full=full,
        )
    except IngestError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(str(scan_out))


def main() -> None:
    """Entry point for the ``aiscan`` script (single default command)."""
    typer.run(scan)


if __name__ == "__main__":
    main()
