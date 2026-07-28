"""SPEC-3 V4: graph view — deterministic SVG, scale guards, click-through."""

from __future__ import annotations

from pathlib import Path

from aiscan.cli import run_scan
from aiscan.inventory.schema import Record
from aiscan.report.graph_svg import render_graph_svg
from tests.conftest import FIXTURES, fixture_repo


def _synthetic(n_test: int, n_prod: int = 2) -> Record:
    agents = []
    for i in range(n_test):
        agents.append(
            {
                "agent_id": f"t{i:04d}",
                "location": "test",
                "detection": {"method": "m", "confidence": "high", "evidence": ["t.py:1"]},
            }
        )
    for i in range(n_prod):
        agents.append(
            {
                "agent_id": f"prod{i}",
                "location": "production",
                "detection": {"method": "m", "confidence": "high", "evidence": ["p.py:1"]},
                "tools": ["send"],
                "handoffs": [f"prod{(i + 1) % n_prod}"] if n_prod > 1 else [],
            }
        )
    return Record.model_validate(
        {
            "bundle_id": "repo:x",
            "name": "x",
            "agents": agents,
            "tools": [{"tool_id": "send", "kind": "function", "evidence": ["p.py:2"]}],
        }
    )


class TestScaleGuards:
    def test_300_test_agents_collapse_into_aggregate(self) -> None:
        record = _synthetic(300)
        svg = render_graph_svg(record)
        assert "test agents (300)" in svg
        assert "Showing 2 of 302 agents individually" in svg
        assert "300 grouped" in svg
        # No individual test-agent nodes rendered.
        assert "t0001" not in svg

    def test_small_groups_render_individually(self) -> None:
        record = _synthetic(3)
        svg = render_graph_svg(record)
        assert "t0001" in svg
        assert "grouped" not in svg

    def test_deterministic(self) -> None:
        record = _synthetic(50)
        assert render_graph_svg(record) == render_graph_svg(record)


class TestGraphContent:
    def test_click_through_and_handoff_arcs(self) -> None:
        svg = render_graph_svg(_synthetic(0, n_prod=2))
        assert "href='#agent-prod0'" in svg and "href='#agent-prod1'" in svg
        assert ">handoff</text>" in svg
        assert "send" in svg  # tool node

    def test_framework_fixture_report_contains_graph(self, tmp_path: Path) -> None:
        org = FIXTURES / "fw_openai_agents_basic" / "org.yaml"
        out = run_scan(
            str(fixture_repo("fw_openai_agents_basic")),
            out=tmp_path,
            org_pack=org if org.is_file() else None,
        )
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "<h2>System graph</h2>" in report
        assert "<svg" in report
        assert "handoff" in report

    def test_no_ai_report_has_no_graph(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("no_ai_clean")), out=tmp_path / "n")
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "<svg" not in report
