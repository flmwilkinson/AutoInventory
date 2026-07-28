"""Fleet runner (SPEC-3 §9): enumerate → scan → collect.

Input is an explicit repo-list file (one git URL or local path per line,
``#`` comments allowed). Repos are scanned **sequentially in input order** —
deterministic collection, and the shared wrapper registry
(``org_registry.json`` at the fleet root) is read/written without races so a
wrapper classified in repo A is reused in repo B. A repo failure is recorded
in the run summary and skipped, never fatal. After the run: the dataset is
rebuilt and the estate ``index.html`` written.
"""

from __future__ import annotations

import html as _html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiscan.context import Settings
from aiscan.dataset import rebuild_dataset

_SEV_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'none'; img-src data:"
)


@dataclass(slots=True)
class FleetResult:
    out: Path
    scanned: list[tuple[str, Path]] = field(default_factory=list)  # target, scan dir
    failed: list[tuple[str, str]] = field(default_factory=list)  # target, error
    dataset_systems: int = 0


def parse_repo_list(path: Path) -> list[str]:
    targets: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            targets.append(line)
    return targets


def run_fleet(
    repos_file: Path,
    out: Path,
    org_pack: Path | None = None,
    enrich: bool = False,
    json_logs: bool = False,
    logger: logging.Logger | None = None,
) -> FleetResult:
    """Scan every repo in the list into ``out/records``, then build the
    dataset + estate index at ``out``."""
    from aiscan.cli import run_scan  # local import: cli imports this module's package

    log = logger or logging.getLogger("aiscan.fleet")
    out = out.resolve()
    records_root = out / "records"
    result = FleetResult(out=out)

    settings = Settings.load().model_copy(
        update={
            "out_dir": records_root,
            "org_pack_path": org_pack,
            # One shared registry for the whole run (SPEC-3 §9.2).
            "org_registry_path": out / "org_registry.json",
        }
    )

    for i, target in enumerate(parse_repo_list(repos_file)):
        # Each target gets its own slot: two repos may share a basename
        # (every fixture is literally named "repo") and must never collide.
        base = "".join(c if c.isalnum() or c in "-_." else "-" for c in Path(target).name)
        slot = records_root / f"{i:03d}-{base or 'repo'}"
        try:
            scan_dir = run_scan(
                target,
                out=slot,
                org_pack=org_pack,
                json_logs=json_logs,
                settings=settings,
                enrich=enrich,
            )
            result.scanned.append((target, scan_dir))
            log.info("fleet: scanned %s -> %s", target, scan_dir)
        except Exception as exc:  # a repo failure never kills the run (§9.1)
            result.failed.append((target, str(exc)))
            log.warning("fleet: FAILED %s: %s", target, exc)

    result.dataset_systems = rebuild_dataset(out, out)
    (out / "index.html").write_text(_render_index(result), encoding="utf-8")
    _write_summary(out, result)
    return result


def _write_summary(out: Path, result: FleetResult) -> None:
    summary = {
        "scanned": [
            {"target": t, "record_dir": p.name} for t, p in result.scanned
        ],
        "failed": [{"target": t, "error": e} for t, e in result.failed],
        "dataset_systems": result.dataset_systems,
    }
    (out / "fleet_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# -- estate index -----------------------------------------------------------


def _esc(value: object) -> str:
    return _html.escape(str(value), quote=True)


def _load_record(scan_dir: Path) -> dict[str, Any] | None:
    path = scan_dir / "record.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _derived(record: dict[str, Any], fld: str) -> object:
    slot = (record.get("derived") or {}).get(fld) or {}
    return slot.get("value") if isinstance(slot, dict) else None


def _index_row(target: str, scan_dir: Path, record: dict[str, Any], out: Path) -> str:
    verdict = str(record.get("ai_verdict", "ai_detected"))
    by_loc = _derived(record, "agents_by_location")
    by_loc = by_loc if isinstance(by_loc, dict) else {}
    flags = _derived(record, "capability_flags")
    flags = flags if isinstance(flags, dict) else {}
    chips = "".join(
        f"<span class='chip on'>{_esc(k)}</span>" for k, v in sorted(flags.items()) if v
    )
    severities = [
        str(f.get("severity"))
        for f in record.get("findings") or []
        if isinstance(f, dict) and f.get("severity")
    ]
    worst = min(severities, key=lambda s: _SEV_RANK.get(s, 9), default=None)
    enrichment = (record.get("scan_health") or {}).get("enrichment")
    if isinstance(enrichment, dict):
        enrich_text = str(enrichment.get("status", "?"))
    else:
        enrich_text = "not requested"
    agents_text = " / ".join(
        f"{loc}: {by_loc.get(loc, 0)}" for loc in ("production", "example", "test")
    )
    # SPEC-6 Tier-1: type + canonical models, and the estate row opens the
    # inventory record (the annex is one click further, from the record).
    st = _derived(record, "system_type")
    type_text = ""
    if isinstance(st, dict):
        components = st.get("components")
        comp = (
            f" ({', '.join(str(c) for c in components)})"
            if isinstance(components, list) and components
            else ""
        )
        type_text = f"{st.get('value', '')}{comp}"
    models = _derived(record, "models_used")
    displays: list[str] = []
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict) and m.get("display") and m["display"] not in displays:
                displays.append(str(m["display"]))
    link = (scan_dir / "inventory.html").relative_to(out).as_posix()
    return (
        "<tr>"
        f"<td><a href='{_esc(link)}'>{_esc(record.get('name', target))}</a></td>"
        f"<td><span class='badge {_esc(verdict)}'>"
        f"{_esc(verdict.replace('_', ' ').upper())}</span></td>"
        f"<td>{_esc(type_text) or '&mdash;'}</td>"
        f"<td>{_esc(agents_text)}</td>"
        f"<td>{_esc('; '.join(displays)) or '&mdash;'}</td>"
        f"<td>{chips or '&mdash;'}</td>"
        f"<td>{_esc(_derived(record, 'autonomy_profile') or '—')}</td>"
        f"<td>{_esc(worst.upper()) if worst else '&mdash;'}</td>"
        f"<td>{_esc(enrich_text)}</td>"
        "</tr>"
    )


_INDEX_CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1a1d21}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.4rem}
table{border-collapse:collapse;width:100%;font-size:.85rem;background:#fff}
th,td{border:1px solid #d9dde3;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#eceff3}
a{color:#1f5eb8}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:600}
.badge.ai_detected{background:#0b6b3a;color:#fff}
.badge.ai_signals_only{background:#b7791f;color:#fff}
.badge.no_ai{background:#5a6270;color:#fff}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:#c62828;color:#fff;
font-size:.75rem;margin:1px 2px}
.fail{background:#fdecea;border:1px solid #e4a09a;border-radius:6px;padding:8px 12px;
font-size:.85rem;margin-top:14px}
.meta{color:#5a6270;font-size:.85rem}
""".strip()


def _render_index(result: FleetResult) -> str:
    rows: list[str] = []
    for target, scan_dir in result.scanned:
        record = _load_record(scan_dir)
        if record is not None:
            rows.append(_index_row(target, scan_dir, record, result.out))
    failures = "".join(
        f"<div class=fail><b>{_esc(t)}</b> failed: {_esc(e)}</div>"
        for t, e in result.failed
    )
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>AI estate inventory</title>"
        f"<style>{_INDEX_CSS}</style></head><body><main>"
        f"<h1>AI estate inventory</h1>"
        f"<p class=meta>{len(result.scanned)} systems scanned, "
        f"{len(result.failed)} failed. Dataset: inventory.db + csv/. "
        "Per-system reports are the drill-down.</p>"
        "<table><tr><th>System</th><th>Verdict</th><th>Type</th>"
        "<th>Agents (by location)</th><th>Models</th>"
        "<th>Capability flags</th><th>Autonomy</th><th>Worst finding</th>"
        f"<th>Enrichment</th></tr>{''.join(rows)}</table>"
        f"{failures}"
        "</main></body></html>"
    )
