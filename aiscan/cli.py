"""``aiscan`` CLI (SPEC §1): thin orchestration over the pipeline stages."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

import aiscan
from aiscan.context import OrgPack, Settings
from aiscan.core import (
    ScanRequest,
    discover_python_files,
    discover_source_files,
)
from aiscan.core import scan as core_scan
from aiscan.frontends.framework.engine import load_packs
from aiscan.incremental.gate import org_pack_hash, should_skip
from aiscan.ingest.git import IngestError, IngestResult, fetch_source
from aiscan.store import LocalDirStore, commit_tag

# discover_python_files / discover_source_files live in aiscan.core now; they are
# re-exported here because tests and callers import them from aiscan.cli.
__all__ = ["discover_python_files", "discover_source_files", "main", "run_scan", "scan"]


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
    # A standalone Logger instance per scan (SPEC-10 §4), NOT the shared
    # getLogger("aiscan") singleton: two scans running concurrently in one
    # process would otherwise rip out each other's handlers and cross-write
    # their scan.log. Constructing the instance directly keeps it out of the
    # global logger registry and out of the root-propagation hierarchy.
    logger = logging.Logger("aiscan")
    logger.setLevel(logging.INFO)
    logger.propagate = False
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


def _default_actor() -> str | None:
    """The OS user, for the audit event-log — best-effort (some environments
    have no resolvable user)."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return None


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
    actor: str | None = None,
    trigger: str = "manual",
) -> Path:
    """Run a scan; returns the artifact directory. Never executes repo code.

    ``base`` names the commit to diff against for incremental analysis (defaults
    to the bundle's last-scanned commit); ``full`` forces a from-scratch scan and
    ignores any cache (SPEC-8). ``actor``/``trigger`` are scan provenance for the
    audit event-log (who ran it / why: manual|ci|cron|pr|api) — never written to
    the deterministic record (SPEC_INVENTORY audit spine)."""
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
    # SPEC-10 §4: the CLI adapter resolves the LLM key from the environment here
    # and injects it into the scan request, so the core never touches os.environ.
    llm_api_key: str | None = None
    if needs_env:
        from aiscan.llm import resolve_api_key

        llm_api_key = resolve_api_key()
    if org_pack is not None:
        settings = settings.model_copy(update={"org_pack_path": org_pack})
    logger = _make_logger(json_logs or settings.json_logs)
    out_root = (out or settings.out_dir).resolve()
    if settings.org_registry_path is None:
        settings = settings.model_copy(
            update={"org_registry_path": out_root / "org_registry.json"}
        )

    # SPEC-10 §3: state (manifest, prior facts, artifacts) is addressed through a
    # store keyed by (bundle, commit) — LocalDirStore reproduces today's layout.
    # The org registry is org-scoped: pass the (possibly shared, e.g. fleet-root)
    # path so per-repo out dirs still share one registry (SPEC-10 §J).
    store = LocalDirStore(out_root, settings.org_registry_path)
    # SPEC-10 §2: the source is fetched through a provider. A URL is cloned
    # ephemerally into a temp dir that is removed when the `with` block exits —
    # there is no persistent `_clones/` cache; a local dir is scanned in place.
    with fetch_source(target, commit, logger) as source:
        ingest_result = source.result
        bundle = ingest_result.bundle_name
        scan_out = store.location(bundle, ingest_result.commit)

        # SPEC-8: cache gate. The manifest records the last-scanned commit + every
        # input whose change would invalidate the cache.
        # A dirty local tree is not addressable by HEAD: never skip, never cache.
        cacheable = not full and not ingest_result.dirty
        manifest = store.get_manifest(bundle) if cacheable else None
        scanner_version = f"aiscan {aiscan.__version__}"
        rulepack_versions = {p.framework: p.version for p in load_packs()}
        org_digest = org_pack_hash(settings.org_pack_path)
        if manifest is not None and should_skip(
            manifest,
            head_commit=ingest_result.commit,
            scanner_version=scanner_version,
            rulepack_versions=rulepack_versions,
            org_pack_digest=org_digest,
            record_present=store.has_record(bundle, manifest.last_scanned_commit),
        ):
            skip_dir = store.location(bundle, manifest.last_scanned_commit)
            logger.info(
                "commit %s already scanned with identical inputs; skipping (%s)",
                commit_tag(ingest_result.commit),
                skip_dir,
            )
            return skip_dir

        scan_out.mkdir(parents=True, exist_ok=True)
        log_handler = _attach_log_file(
            logger, scan_out / "scan.log", json_logs or settings.json_logs
        )
        try:
            logger.info(
                "scanning %s @ %s -> %s", ingest_result.repo_root, ingest_result.commit, scan_out
            )
            # The core builds the record/graph/facts in memory; this adapter routes
            # them to the store and chooses where the LLM-tier caches live
            # (SPEC-10 §1). A future service worker calls core.scan directly and
            # persists the ScanResult to its own store backend.
            result = core_scan(
                ScanRequest(
                    repo_root=ingest_result.repo_root,
                    repo_url=ingest_result.repo_url,
                    commit=ingest_result.commit,
                    bundle_name=bundle,
                    settings=settings,
                    org_pack=OrgPack.load(settings.org_pack_path),
                    logger=logger,
                    dirty=ingest_result.dirty,
                    adjudicate=adjudicate,
                    adjudicate_call_fn=adjudicate_call_fn,
                    adjudication_cache_path=scan_out / "adjudication_cache.json",
                    enrich=enrich,
                    enrich_call_fn=enrich_call_fn,
                    enrichment_cache_path=scan_out / "enrichment_cache.json",
                    enrich_tests=enrich_tests,
                    llm_api_key=llm_api_key,
                    manifest=manifest,
                    base=base,
                    store=store,
                )
            )
            store.put_artifacts(bundle, ingest_result.commit, result)
            # SPEC-10 §K: write-through the queryable dataset (a persistent
            # single-file SQLite keyed by (bundle, commit)) so the org-wide
            # inventory is a live DB, not a rebuild-on-demand artifact. Idempotent
            # — a rescan of the same commit replaces its own rows.
            from aiscan.dataset import (
                append_audit_entry,
                append_scan_event,
                upsert_record,
            )

            inventory_db = out_root / "inventory.db"
            scan_id = upsert_record(inventory_db, result.record.model_dump(mode="json"))
            # SPEC_INVENTORY audit spine: log who/when/why. append_scan_event is
            # the per-commit estate index; append_audit_entry is the per-run,
            # append-only, hash-chained tamper-evident ledger. Both are scan
            # provenance — never written to the deterministic record.
            bundle_id = result.record.bundle_id
            scanned_at = result.record.inventory_provenance.scanned_at
            commit_ = ingest_result.commit
            append_scan_event(
                inventory_db, scan_id=scan_id, bundle_id=bundle_id, commit=commit_,
                base_commit=base, scanned_at=scanned_at, actor=actor,
                trigger=trigger, scanner_ver=scanner_version,
            )
            append_audit_entry(
                inventory_db, scan_id=scan_id, bundle_id=bundle_id, commit=commit_,
                base_commit=base, scanned_at=scanned_at, actor=actor,
                trigger=trigger, scanner_ver=scanner_version,
            )
            if not ingest_result.dirty:
                _write_scan_manifest(
                    store,
                    ingest_result,
                    scanner_version=scanner_version,
                    rulepack_versions=rulepack_versions,
                    org_digest=org_digest,
                )
            return scan_out
        finally:
            logger.removeHandler(log_handler)
            log_handler.close()


def _write_scan_manifest(
    store: LocalDirStore,
    ingest_result: IngestResult,
    *,
    scanner_version: str,
    rulepack_versions: dict[str, str],
    org_digest: str,
) -> None:
    """Record this scan's inputs so the next run can skip / go incremental."""
    from aiscan.incremental import ANALYSIS_VERSION, Manifest
    from aiscan.incremental.gate import deps_hash, tsconfig_hash
    from aiscan.modules.graph import detect_source_roots

    if ingest_result.commit in ("", "unknown", "unversioned"):
        return  # only commit-addressable scans are cache-safe
    repo_root = ingest_result.repo_root
    roots = detect_source_roots(discover_source_files(repo_root))
    store.put_manifest(
        Manifest(
            bundle=ingest_result.bundle_name,
            last_scanned_commit=ingest_result.commit,
            scanner_version=scanner_version,
            analysis_version=ANALYSIS_VERSION,
            rulepack_versions=rulepack_versions,
            source_roots=roots,
            deps_hash=deps_hash(repo_root),
            tsconfig_hash=tsconfig_hash(repo_root),
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
    actor: Annotated[
        str | None,
        typer.Option("--actor", help="Who ran this scan, for the audit event-log "
                     "(defaults to the OS user; a CI/webhook worker passes the PR author)"),
    ] = None,
    trigger: Annotated[
        str,
        typer.Option("--trigger", help="Why this scan ran, for the audit event-log: "
                     "manual | ci | cron | pr | api"),
    ] = "manual",
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
    verify_audit: Annotated[
        bool,
        typer.Option(
            "--verify-audit",
            help="Verify the tamper-evident audit ledger in --out (default "
            "aiscan-out), report any break, then exit",
        ),
    ] = False,
    unattested: Annotated[
        bool,
        typer.Option(
            "--unattested",
            help="List detected AI systems NOT approved in the governance register "
            "(the shadow-AI / un-attested report) from --out, then exit",
        ),
    ] = False,
    govern: Annotated[
        str | None,
        typer.Option(
            "--govern",
            help="Record a governance decision for this bundle_id in --out "
            "(with --owner/--risk/--approve), audited, then exit",
        ),
    ] = None,
    owner: Annotated[str | None, typer.Option("--owner", help="Owner, for --govern")] = None,
    risk: Annotated[str | None, typer.Option("--risk", help="Risk tier, for --govern")] = None,
    approve: Annotated[
        bool, typer.Option("--approve", help="Mark the --govern system approved")
    ] = False,
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
    if verify_audit or unattested or govern is not None:
        inventory_db = (out or Settings.load().out_dir).resolve() / "inventory.db"
        if verify_audit:
            from aiscan.dataset import verify_audit_log

            res = verify_audit_log(inventory_db)
            status = "OK" if res["ok"] else f"TAMPERED at seq {res['first_bad_seq']}"
            typer.echo(
                f"audit ledger: {res['entries']} entries — {status} "
                f"(tip {str(res['tip_hash'])[:16]})"
            )
            raise typer.Exit(0 if res["ok"] else 1)
        if unattested:
            from aiscan.dataset import unattested_systems

            rows = unattested_systems(inventory_db)
            if not rows:
                typer.echo("all detected AI systems are attested in the governance register")
            else:
                typer.echo(f"un-attested AI systems ({len(rows)}):")
                for bundle_id, verdict, appr in rows:
                    typer.echo(f"  {bundle_id}  [{verdict}]  approval={appr or 'none'}")
            raise typer.Exit(1 if rows else 0)
        if govern is not None:
            import datetime

            from aiscan.dataset import set_governance

            set_governance(
                inventory_db,
                govern,
                owner=owner,
                risk_tier=risk,
                approval_status="approved" if approve else None,
                actor=actor or _default_actor(),
                at=datetime.datetime.now(datetime.UTC).isoformat(),
            )
            typer.echo(f"governance recorded for {govern}")
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
            actor=actor or _default_actor(),
            trigger=trigger,
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
