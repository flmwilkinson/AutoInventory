"""SPEC-3 §8 inventory dataset: records → SQLite + CSV, pure projection."""

from aiscan.dataset.store import bom_diff, rebuild_dataset, upsert_record

__all__ = ["bom_diff", "rebuild_dataset", "upsert_record"]
