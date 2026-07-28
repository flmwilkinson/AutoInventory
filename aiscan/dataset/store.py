"""SQLite + CSV dataset store (SPEC-3 §8), stdlib only.

``rebuild_dataset`` walks every record.json under a directory and writes
``inventory.db`` + ``csv/*.csv`` from scratch — a pure projection of the
records: rebuilding twice from the same records yields identical table
content and byte-identical CSVs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from aiscan.dataset.flatten import Row, flatten, scan_id_for

# SPEC_INVENTORY audit vault: genesis link of the hash chain.
_GENESIS = "0" * 64
_AUDIT_FIELDS = (
    "scan_id", "bundle_id", "commit", "base_commit",
    "scanned_at", "actor", "trigger", "scanner_ver",
)

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
        "model_endpoint", "api_style", "model_provider_class",
        "has_additional_models", "prompt_dynamic", "tool_count",
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
        "declared_tools", "approval_policy", "attached_agent_count",
    ),
    "model_usages": (
        "bundle_id", "scan_id", "usage_id", "model_value", "method",
        "confidence", "task", "endpoint", "in_agent",
    ),
    # SPEC_INVENTORY audit spine: the temporal event-log — one row per scanned
    # (bundle, commit). scan_id is the content hash (tamper-evident); actor/
    # trigger are scan provenance, NOT record content (so record.json stays
    # deterministic). "what changed" is bom_diff between consecutive commits.
    "scans": (
        "scan_id", "bundle_id", "commit", "base_commit", "scanned_at",
        "actor", "trigger", "scanner_ver",
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
    "scans": ("scan_id",),
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


def _ensure_audit_tables(conn: sqlite3.Connection) -> None:
    # SPEC_INVENTORY: the tamper-evident audit LEDGER (append-only, hash-chained,
    # one row per scan RUN) and the governance overlay + its own change-audit.
    conn.execute(
        'CREATE TABLE IF NOT EXISTS "audit_log" (seq INTEGER PRIMARY KEY, '
        'scan_id TEXT, bundle_id TEXT, "commit" TEXT, base_commit TEXT, '
        "scanned_at TEXT, actor TEXT, trigger TEXT, scanner_ver TEXT, "
        "prev_hash TEXT, entry_hash TEXT)"
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS "governance" (bundle_id TEXT PRIMARY KEY, '
        "owner TEXT, risk_tier TEXT, approval_status TEXT, lifecycle TEXT, "
        "updated_by TEXT, updated_at TEXT)"
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS "governance_audit" (seq INTEGER PRIMARY KEY, '
        "bundle_id TEXT, field TEXT, old_value TEXT, new_value TEXT, "
        "actor TEXT, at TEXT)"
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating) the persistent dataset DB in WAL mode with the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_tables(conn)
    _ensure_audit_tables(conn)
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


def append_scan_event(
    db_path: Path,
    *,
    scan_id: str,
    bundle_id: str | None,
    commit: str,
    base_commit: str | None,
    scanned_at: str | None,
    actor: str | None,
    trigger: str,
    scanner_ver: str | None,
) -> None:
    """Record one scan in the audit event-log (SPEC_INVENTORY audit spine).

    Idempotent per ``scan_id`` (one row per scanned ``(bundle, commit)``): the
    estate-change log — each commit is one event, and ``bom_diff`` between
    consecutive commits gives what changed. ``actor``/``trigger`` are scan
    provenance and live ONLY here, never in the deterministic record."""
    conn = _connect(db_path)
    try:
        conn.execute(
            'INSERT OR REPLACE INTO "scans" '
            '(scan_id, bundle_id, "commit", base_commit, scanned_at, actor, trigger, scanner_ver) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, bundle_id, commit, base_commit, scanned_at, actor, trigger, scanner_ver),
        )
        conn.commit()
    finally:
        conn.close()


def _entry_hash(seq: int, values: tuple[object, ...], prev_hash: str) -> str:
    basis = "|".join(str(v) for v in (seq, *values, prev_hash))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def append_audit_entry(
    db_path: Path,
    *,
    scan_id: str,
    bundle_id: str | None,
    commit: str,
    base_commit: str | None,
    scanned_at: str | None,
    actor: str | None,
    trigger: str,
    scanner_ver: str | None,
) -> str:
    """Append ONE scan run to the tamper-evident audit ledger and return the new
    tip hash (SPEC_INVENTORY audit vault).

    Append-only + hash-chained: each row links to the prior row's ``entry_hash``,
    so any later edit or mid-chain deletion breaks the chain and ``verify_audit_log``
    catches it — cryptographic tamper-EVIDENCE, entirely local, no storage
    product required. (Tamper-PREVENTION additionally needs OS/WORM controls.)
    One row per RUN, so re-scanning a commit is a distinct, logged event."""
    values = (scan_id, bundle_id, commit, base_commit, scanned_at, actor, trigger, scanner_ver)
    conn = _connect(db_path)
    try:
        tip = conn.execute(
            "SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (tip[0] + 1) if tip else 1
        prev_hash = tip[1] if tip else _GENESIS
        entry_hash = _entry_hash(seq, values, prev_hash)
        conn.execute(
            'INSERT INTO "audit_log" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (seq, *values, prev_hash, entry_hash),
        )
        conn.commit()
    finally:
        conn.close()
    return entry_hash


def verify_audit_log(db_path: Path) -> dict[str, object]:
    """Walk the ledger and check every link. Returns ``{ok, entries, tip_hash,
    first_bad_seq}``. A break means a historical row was edited or deleted.

    Detects edits and mid-chain deletions. Tail-truncation (deleting the newest
    rows) is not chain-detectable alone — compare ``tip_hash`` against an external
    record (e.g. a prior verify's output) to catch it."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT seq, scan_id, bundle_id, \"commit\", base_commit, scanned_at, "
            "actor, trigger, scanner_ver, prev_hash, entry_hash FROM audit_log ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    expected_prev = _GENESIS
    expected_seq = 1
    for r in rows:
        seq, *values, prev_hash, entry_hash = r
        recomputed = _entry_hash(seq, tuple(values), prev_hash)
        if seq != expected_seq or prev_hash != expected_prev or recomputed != entry_hash:
            return {
                "ok": False,
                "entries": len(rows),
                "tip_hash": expected_prev,
                "first_bad_seq": seq,
            }
        expected_prev = entry_hash
        expected_seq += 1
    return {"ok": True, "entries": len(rows), "tip_hash": expected_prev, "first_bad_seq": None}


def set_governance(
    db_path: Path,
    bundle_id: str,
    *,
    owner: str | None = None,
    risk_tier: str | None = None,
    approval_status: str | None = None,
    lifecycle: str | None = None,
    actor: str | None = None,
    at: str | None = None,
) -> None:
    """Record a governance decision for a system, auditing every changed field
    (SPEC_INVENTORY). Only the fields passed are updated; the rest are preserved.
    The decision is human, separate from the detected evidence — never overwrites it."""
    fields = ("owner", "risk_tier", "approval_status", "lifecycle")
    new = {
        "owner": owner,
        "risk_tier": risk_tier,
        "approval_status": approval_status,
        "lifecycle": lifecycle,
    }
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT owner, risk_tier, approval_status, lifecycle FROM governance WHERE bundle_id=?",
            (bundle_id,),
        ).fetchone()
        old = dict(zip(fields, cur, strict=True)) if cur else {f: None for f in fields}
        gseq = (conn.execute("SELECT max(seq) FROM governance_audit").fetchone()[0] or 0)
        for f in fields:
            if new[f] is not None and new[f] != old[f]:
                gseq += 1
                conn.execute(
                    'INSERT INTO "governance_audit" VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (gseq, bundle_id, f, old[f], new[f], actor, at),
                )
        merged = {f: (new[f] if new[f] is not None else old[f]) for f in fields}
        conn.execute(
            'INSERT OR REPLACE INTO "governance" '
            "(bundle_id, owner, risk_tier, approval_status, lifecycle, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bundle_id, merged["owner"], merged["risk_tier"],
             merged["approval_status"], merged["lifecycle"], actor, at),
        )
        conn.commit()
    finally:
        conn.close()


def unattested_systems(db_path: Path) -> list[tuple[str, str, str | None]]:
    """The shadow-AI / un-attested report: detected AI systems NOT approved in the
    governance register (SPEC_INVENTORY reconciliation). Liveness-agnostic and
    prominent — a bank must reconcile every detected AI system against the approved
    register, regardless of how live it is. Returns (bundle_id, verdict, status)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT s.bundle_id, s.ai_verdict, g.approval_status "
            'FROM systems s LEFT JOIN governance g ON s.bundle_id = g.bundle_id '
            "WHERE s.ai_verdict IN ('ai_detected', 'ai_signals_only') "
            "AND (g.approval_status IS NULL OR g.approval_status != 'approved') "
            "ORDER BY s.bundle_id"
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


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
