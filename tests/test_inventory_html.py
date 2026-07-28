"""SPEC-6 §4: inventory.html — the record body, its jargon guard, and the
report = record + annex invariant, exercised on real fixture scans."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiscan.cli import run_scan
from tests.harness import FIXTURES

# Scanner jargon that must never reach the record body (the annex may use it).
_JARGON = ("sink", "symbolic", "resolver", "unresolved:", '"union"')


@pytest.fixture(scope="module")
def gateway_scan(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("inv")
    return run_scan(str(FIXTURES / "bespoke_gateway_loop" / "repo"), out=out)


def _inventory(scan_dir: Path) -> str:
    return (scan_dir / "inventory.html").read_text(encoding="utf-8")


class TestInventoryRecord:
    def test_artifact_written(self, gateway_scan: Path) -> None:
        assert (gateway_scan / "inventory.html").is_file()
        assert (gateway_scan / "report.html").is_file()

    def test_no_scanner_jargon_in_record_body(self, gateway_scan: Path) -> None:
        body = _inventory(gateway_scan).lower()
        for term in _JARGON:
            assert term not in body, f"jargon {term!r} leaked into the record"

    def test_record_sections_present(self, gateway_scan: Path) -> None:
        body = _inventory(gateway_scan)
        for heading in (
            "What this system is",
            "Agents",
            "Models used",
            "Connections &amp; dependencies",
            "About this record",
        ):
            assert heading in body
        assert "technical annex" in body
        assert "report.html" in body

    def test_absence_phrasing(self, gateway_scan: Path) -> None:
        # D∅ discipline: absences say "detected", never bare "none".
        body = _inventory(gateway_scan)
        assert "no human-approval gate detected in code" in body

    def test_report_embeds_record_body(self, gateway_scan: Path) -> None:
        """report.html = the record body + annex — same sections, one renderer."""
        report = (gateway_scan / "report.html").read_text(encoding="utf-8")
        assert "What this system is" in report
        assert "Technical annex" in report
        # The record body precedes the annex.
        assert report.index("What this system is") < report.index("Technical annex")

    def test_report_annex_has_drilldown(self, gateway_scan: Path) -> None:
        report = (gateway_scan / "report.html").read_text(encoding="utf-8")
        for heading in ("Governance", "Findings", "Scan health"):
            assert heading in report

    def test_inventory_has_no_findings_table(self, gateway_scan: Path) -> None:
        body = _inventory(gateway_scan)
        assert "<h2>Findings</h2>" not in body
        assert "Governance" not in body


class TestNoAiInventory:
    def test_negative_attestation(self, tmp_path: Path) -> None:
        out = run_scan(str(FIXTURES / "no_ai_clean" / "repo"), out=tmp_path)
        body = _inventory(out)
        assert "No AI detected" in body
        assert "negative attestation" in body


def test_canonical_model_shown_not_union(gateway_scan: Path) -> None:
    """The record renders canonical displays; raw union JSON must not appear."""
    body = _inventory(gateway_scan)
    assert not re.search(r"\{['\"]union", body)


def test_tools_grouped_by_location_like_agents() -> None:
    """Example/test tools collapse into locgroups; production tools stay flat
    (same affordance as the agents section)."""
    from aiscan.inventory.schema import Record, ToolRecord
    from aiscan.report.inventory import _tools_section

    record = Record(
        bundle_id="b",
        name="n",
        tools=(
            ToolRecord(tool_id="tool:prod", kind="function", location="production"),
            ToolRecord(tool_id="tool:demo", kind="function", location="example"),
            ToolRecord(tool_id="tool:fake", kind="function", location="test"),
        ),
    )
    html = _tools_section(record)
    assert "<details class=locgroup><summary>example tools (1)</summary>" in html
    assert "<details class=locgroup><summary>test tools (1)</summary>" in html
    # The production card is a plain (open) card, not inside a locgroup.
    assert html.index("tool:prod") < html.index("locgroup")


def test_models_endpoint_union_renders_plainly() -> None:
    """A union endpoint (env-configurable base_url) renders as the canonical
    reading, never the annex-grade '∪ … (symbolic)' machine form."""
    from aiscan.inventory.schema import DerivedBlock, DerivedValue, Record
    from aiscan.report.inventory import _models_section

    record = Record(
        bundle_id="b",
        name="n",
        derived=DerivedBlock(
            models_used=DerivedValue(
                value=[
                    {
                        "display": "set via env EXAMPLE_MODEL_NAME",
                        "qualifiers": [],
                        "provider_class": "unknown",
                        "task": "chat",
                        "endpoint": {"union": ["", {"symbolic": "env:EXAMPLE_BASE_URL"}]},
                        "locations": ["example"],
                    }
                ]
            )
        ),
    )
    html = _models_section(record)
    assert "set via env EXAMPLE_BASE_URL" in html
    assert "symbolic" not in html
    assert "&cup;" not in html
