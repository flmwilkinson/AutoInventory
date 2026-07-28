"""Scan artifact shape and early redaction wiring."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.cli import run_scan
from tests.conftest import fixture_repo


def test_artifacts_written(tmp_path: Path) -> None:
    out = run_scan(str(fixture_repo("bespoke_llm_call_only")), out=tmp_path)
    for artifact in ("record.json", "scan_health.json", "facts.jsonl", "scan.log"):
        assert (out / artifact).exists(), artifact
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert record["bundle_id"] == "repo:repo"
    assert record["owner"] == {"value": None, "source": "governance", "candidate": None}
    # [E] slots are null-with-marker until --enrich fills them.
    assert record["description"] == {
        "value": None,
        "source": "enriched",
        "confirmed_by": None,
        "insufficient_evidence": None,
    }
    assert record["system_summary"]["source"] == "enriched"
    assert record["system_summary"]["value"] is None
    assert record["inventory_provenance"]["detection_basis"] == "static/declared"


def test_secret_literal_redacted_in_record(tmp_path: Path) -> None:
    out = run_scan(str(fixture_repo("secret_literal")), out=tmp_path)
    record_text = (out / "record.json").read_text(encoding="utf-8")
    assert "sk-test" not in record_text
    record = json.loads(record_text)
    findings = record["findings"]
    assert any(
        f["kind"] == "secret_literal_redacted"
        and f["evidence"] == ["config.py:3-3"]
        and f["severity"] == "high"
        for f in findings
    )
    # SPEC-3 derive adds orphan_model_usage for the bare call site; the health
    # counter always matches the emitted findings list. (`client.ask` is a
    # *function*-shaped wrapper candidate — SPEC-7 Z7 deliberately does not
    # flag those as dormant.)
    assert record["scan_health"]["findings"] == len(findings) == 2
