"""SPEC-3 §8 inventory dataset: records → SQLite + CSV, pure projection."""

from aiscan.dataset.store import (
    append_scan_event,
    bom_diff,
    rebuild_dataset,
    upsert_record,
)

__all__ = ["append_scan_event", "bom_diff", "rebuild_dataset", "upsert_record"]
