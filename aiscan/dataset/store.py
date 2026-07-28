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

from aiscan.dataset.flatten import Row, flatten, scan_id_for

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
        "autonomy_level", "liveness", "is_entrypoint", "model_value",
        "model_endpoint", "api_style", "prompt_dynamic", "tool_count",
        "reachable_tool_count", "moves_money", "executes_code",
        "mutates_identities", "sends_external", "reads_sensitive",
        "detection_method", "confidence", "agent_summary",
    ),
    "agent_tools": ("bundle_id", "scan_id", "agent_id", "tool_id"),
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
    "mcp_servers": (
        "bundle_id", "scan_id", "server_id", "server", "transport",
        "declared_tools", "approval_policy",
    ),
    "model_usages": (
        "bundle_id", "scan_id", "usage_id", "model_value", "method",
        "confidence", "task", "endpoint", "in_agent",
    ),
}

_KEYS: dict[str, tuple[str, ...]] = {
    "systems": ("bundle_id", "scan_id"),
    "agents": ("bundle_id", "scan_id", "agent_id"),
    "agent_tools": ("bundle_id", "scan_id", "agent_id", "tool_id"),
    "tools": ("bundle_id", "scan_id", "tool_id"),
    "models": ("bundle_id", "scan_id", "model_key"),
    "findings": ("bundle_id", "scan_id", "finding_id"),
    "mcp_servers": ("bundle_id", "scan_id", "server_id"),
    "model_usages": ("bundle_id", "scan_id", "usage_id"),
}


def _cell(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return json.dumps(value, sort_keys=True)


def find_records(root: Path) -> list[Path]:
    return sorted(root.rglob("record.json"))


def _ensure_tables(conn: sqlite3.Connection) -> None:
    for name, columns in _COLUMNS.items():
        cols = ", ".join(f'"{c}"' for c in columns)
        pk = ", ".join(f'"{c}"' for c in _KEYS[name])
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({cols}, PRIMARY KEY ({pk}))')


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating) the persistent dataset DB in WAL mode with the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_tables(conn)
    return conn


def upsert_record(db_path: Path, record: dict[str, object]) -> str:
    """Write-through one record's rows into the persistent dataset (SPEC-10 §K).

    Idempotent per ``(bundle_id, scan_id)``: this scan's rows are replaced
    wholesale (a rescan may have fewer entities), so re-scanning a commit
    converges rather than accumulating duplicates. Returns the scan_id."""
    if "bundle_id" not in record:
        raise ValueError("record has no bundle_id")
    tables = flatten(record)
    scan_id = scan_id_for(record)
    bundle_id = record.get("bundle_id")
    conn = _connect(db_path)
    try:
        for name, rows in tables.items():
            conn.execute(
                f'DELETE FROM "{name}" WHERE bundle_id=? AND scan_id=?',
                (bundle_id, scan_id),
            )
            columns = _COLUMNS[name]
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "{name}" VALUES ({placeholders})',
                [tuple(_cell(row.get(c)) for c in columns) for row in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return scan_id


def bom_diff(
    db_path: Path, base_commit: str, head_commit: str
) -> dict[str, dict[str, list[str]]]:
    """Per-table entities added/removed between two commits' scans — for PR
    review. Each commit is resolved to its scan via the systems table."""
    conn = _connect(db_path)
    try:
        def ids(table: str, id_col: str, commit: str) -> set[str]:
            rows = conn.execute(
                f'SELECT DISTINCT t."{id_col}" FROM "{table}" t '
                'JOIN systems s ON t.bundle_id=s.bundle_id AND t.scan_id=s.scan_id '
                'WHERE s."commit"=?',
                (commit,),
            ).fetchall()
            return {str(r[0]) for r in rows}

        out: dict[str, dict[str, list[str]]] = {}
        for table, id_col in (
            ("agents", "agent_id"),
            ("tools", "tool_id"),
            ("models", "model_value"),
            ("mcp_servers", "server_id"),
        ):
            base = ids(table, id_col, base_commit)
            head = ids(table, id_col, head_commit)
            out[table] = {
                "added": sorted(head - base),
                "removed": sorted(base - head),
            }
        return out
    finally:
        conn.close()


def rebuild_dataset(records_root: Path, out_dir: Path) -> int:
    """Rebuild inventory.db + csv/ from every record under ``records_root``.

    A from-records recovery path (the write-through ``upsert_record`` is the
    steady-state writer). Returns the number of system rows (= records)."""
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
        _ensure_tables(conn)
        for name, columns in _COLUMNS.items():
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
