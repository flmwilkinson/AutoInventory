"""SPEC-3 §8 inventory dataset: records → SQLite + CSV, pure projection."""

from aiscan.dataset.store import (
    append_audit_entry,
    bom_diff,
    rebuild_dataset,
    set_governance,
    unattested_systems,
    upsert_record,
    verify_audit_log,
)

__all__ = [
    "append_audit_entry",
    "bom_diff",
    "rebuild_dataset",
    "set_governance",
    "unattested_systems",
    "upsert_record",
    "verify_audit_log",
]
