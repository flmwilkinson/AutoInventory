"""SQLite + CSV dataset store (SPEC-3 §8), stdlib only.

``rebuild_dataset`` walks every record.json under a directory and writes
``inventory.db`` + ``csv/*.csv`` from scratch — a pure projection of the
records: rebuilding twice from the same records yields identical table
content and byte-identical CSVs.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from aiscan.dataset.flatten import Row, flatten

_COLUMNS: dict[str, tuple[str, ...]] = {
    "systems": (
        "bundle_id", "scan_id", "name", "repo_url", "commit", "ai_verdict",
        "agent_count", "tool_count", "loc_production", "loc_example", "loc_test",
        "autonomy_profile", "moves_money", "executes_code", "mutates_identities",
        "sends_external", "reads_sensitive", "has_unapproved_endpoint",
        "has_dynamic_prompts", "has_unresolved_models",
        "suggested_aia_risk_category", "system_summary", "owner_value",
        "owner_candidate", "risk_tier", "lifecycle_status", "approval_status",
        "scanned_at", "scanner_ver",
    ),
    "agents": (
        "bundle_id", "scan_id", "agent_id", "location", "language", "role_class",
        "autonomy_level", "model_value", "model_endpoint", "api_style",
        "prompt_dynamic", "tool_count", "reachable_tool_count", "moves_money",
        "executes_code", "mutates_identities", "sends_external",
        "reads_sensitive", "detection_method", "confidence", "agent_summary",
    ),
    "tools": (
        "bundle_id", "scan_id", "tool_id", "kind", "location", "side_effects",
        "external_target", "is_sensitive", "declared_authorisation",
        "credential_ref", "capability_class", "tool_summary",
    ),
    "models": (
        "bundle_id", "scan_id", "model_key", "model_value", "endpoint",
        "api_style", "provider_class",
    ),
    "findings": (
        "bundle_id", "scan_id", "finding_id", "kind", "severity",
        "subject_ref", "detail",
    ),
}

_KEYS: dict[str, tuple[str, ...]] = {
    "systems": ("bundle_id", "scan_id"),
    "agents": ("bundle_id", "scan_id", "agent_id"),
    "tools": ("bundle_id", "scan_id", "tool_id"),
    "models": ("bundle_id", "scan_id", "model_key"),
    "findings": ("bundle_id", "scan_id", "finding_id"),
}


def _cell(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return json.dumps(value, sort_keys=True)


def find_records(root: Path) -> list[Path]:
    return sorted(root.rglob("record.json"))


def rebuild_dataset(records_root: Path, out_dir: Path) -> int:
    """Rebuild inventory.db + csv/ from every record under ``records_root``.

    Returns the number of system rows (= records ingested)."""
    tables: dict[str, list[Row]] = {name: [] for name in _COLUMNS}
    for record_path in find_records(records_root):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or "bundle_id" not in record:
            continue
        for name, rows in flatten(record).items():
            tables[name].extend(rows)

    for name, rows in tables.items():
        rows.sort(key=lambda r: tuple(str(r.get(k)) for k in _KEYS[name]))

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "inventory.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        for name, columns in _COLUMNS.items():
            cols = ", ".join(f'"{c}"' for c in columns)
            pk = ", ".join(f'"{c}"' for c in _KEYS[name])
            conn.execute(f'CREATE TABLE "{name}" ({cols}, PRIMARY KEY ({pk}))')
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT OR REPLACE INTO "{name}" VALUES ({placeholders})',
                [tuple(_cell(row.get(c)) for c in columns) for row in tables[name]],
            )
        conn.commit()
    finally:
        conn.close()

    csv_dir = out_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    for name, columns in _COLUMNS.items():
        with (csv_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(columns)
            for row in tables[name]:
                writer.writerow(["" if row.get(c) is None else _cell(row.get(c)) for c in columns])

    return len(tables["systems"])


def dump_tables(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Ordered content dump per table — the rebuild-identity gate compares this."""
    conn = sqlite3.connect(db_path)
    try:
        out: dict[str, list[tuple[object, ...]]] = {}
        for name in _COLUMNS:
            order = ", ".join(f'"{c}"' for c in _KEYS[name])
            out[name] = [
                tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {order}')
            ]
        return out
    finally:
        conn.close()
