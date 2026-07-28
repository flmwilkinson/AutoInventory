"""SPEC §2 item 4: two consecutive scans produce byte-identical graph.json
and identical record.json apart from timestamps."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.cli import run_scan
from tests.conftest import FIXTURES, fixture_repo
from tests.harness import normalize_record, normalize_report


def test_double_scan_byte_identical(tmp_path: Path) -> None:
    repo = fixture_repo("bespoke_gateway_loop")
    org = FIXTURES / "bespoke_gateway_loop" / "org.yaml"
    out1 = run_scan(str(repo), out=tmp_path / "a", org_pack=org)
    out2 = run_scan(str(repo), out=tmp_path / "b", org_pack=org)

    assert (out1 / "graph.json").read_bytes() == (out2 / "graph.json").read_bytes()
    assert (out1 / "facts.jsonl").read_bytes() == (out2 / "facts.jsonl").read_bytes()

    r1 = normalize_record(json.loads((out1 / "record.json").read_text(encoding="utf-8")))
    r2 = normalize_record(json.loads((out2 / "record.json").read_text(encoding="utf-8")))
    assert r1 == r2

    # SPEC-3 §10: the determinism gate covers report.html too.
    h1 = normalize_report((out1 / "report.html").read_text(encoding="utf-8"))
    h2 = normalize_report((out2 / "report.html").read_text(encoding="utf-8"))
    assert h1 == h2


def test_ts_double_scan_byte_identical(tmp_path: Path) -> None:
    """SPEC-4 W7: determinism holds for the tree-sitter frontend too (pinned
    grammar versions ⇒ stable spans)."""
    repo = fixture_repo("ts_bespoke_gateway_loop")
    org = FIXTURES / "ts_bespoke_gateway_loop" / "org.yaml"
    out1 = run_scan(str(repo), out=tmp_path / "a", org_pack=org)
    out2 = run_scan(str(repo), out=tmp_path / "b", org_pack=org)

    assert (out1 / "graph.json").read_bytes() == (out2 / "graph.json").read_bytes()
    assert (out1 / "facts.jsonl").read_bytes() == (out2 / "facts.jsonl").read_bytes()
    r1 = normalize_record(json.loads((out1 / "record.json").read_text(encoding="utf-8")))
    r2 = normalize_record(json.loads((out2 / "record.json").read_text(encoding="utf-8")))
    assert r1 == r2
