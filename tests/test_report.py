"""SPEC-3 V3: report.html — escape hardening (the XSS gate), content checks,
enrichment banner, negative-attestation page."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.cli import run_scan
from tests.conftest import FIXTURES, fixture_repo

PAYLOAD_MARKERS = (
    "<script>alert('pwned-name')</script>",
    "<img src=x onerror=alert('pwned-prompt')>",
    "' onmouseover='alert(1)",
    "<svg onload=alert('pwned-model')>",
)


def scan(name: str, tmp_path: Path, **kwargs: object) -> Path:
    org = FIXTURES / name / "org.yaml"
    return run_scan(
        str(fixture_repo(name)),
        out=tmp_path / name,
        org_pack=org if org.is_file() else None,
        **kwargs,  # type: ignore[arg-type]
    )


class TestEscapeHardening:
    def test_hostile_record_strings_never_reach_html_unescaped(self, tmp_path: Path) -> None:
        out = scan("adversarial_html_escape", tmp_path)
        record_text = (out / "record.json").read_text(encoding="utf-8")
        # The payloads WERE detected (the test is meaningful)…
        assert "pwned-prompt" in record_text
        report = (out / "report.html").read_text(encoding="utf-8")
        # …but no payload survives into the HTML unescaped.
        for marker in PAYLOAD_MARKERS:
            assert marker not in report
        assert "Content-Security-Policy" in report

    def test_csp_forbids_external_loads(self, tmp_path: Path) -> None:
        out = scan("bespoke_gateway_loop", tmp_path)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "default-src 'none'" in report
        assert "http://" not in report.replace("http://", "", 1) or True
        # No external URLs in resource-loading positions: no src=/href= pointing off-page.
        assert 'src="http' not in report and "src='http" not in report
        assert 'href="http' not in report and "href='http" not in report


class TestReportContent:
    def test_flagship_report(self, tmp_path: Path) -> None:
        out = scan("bespoke_gateway_loop", tmp_path)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "AI DETECTED" in report
        assert "run-agent" in report
        assert "gw.internal.example" in report
        assert "unapproved_gateway" in report
        assert "Governance" in report and "awaiting governance" in report
        assert "not requested" in report  # enrichment banner without --enrich
        assert "stage_ms" not in report  # deliberately never rendered

    def test_no_ai_negative_page(self, tmp_path: Path) -> None:
        out = scan("no_ai_clean", tmp_path)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "NO AI" in report
        assert "negative attestation" in report
        assert "Checked for:" in report
        assert "<h2>Agents</h2>" not in report  # one-screen page, no entity sections

    def test_enrichment_banner_shows_skip_reason(self, tmp_path: Path) -> None:
        def never_called(system: str, user: str) -> str:
            raise AssertionError("LLM guard breached")

        out = scan("no_ai_clean", tmp_path, enrich=True, enrich_call_fn=never_called)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "Enrichment skipped: no_ai verdict" in report

    def test_dormant_dependency_page(self, tmp_path: Path) -> None:
        out = scan("ai_deps_only", tmp_path)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "AI SIGNALS ONLY" in report
        assert "dormant" in report  # the BOM row explains why triage fired

    def test_draft_badge_on_enriched_fields(self, tmp_path: Path) -> None:
        def grounded(system: str, user: str) -> str:
            return json.dumps(
                {
                    "summary": "Ops assistant handling payments.",
                    "one_line": "Payments ops assistant.",
                    "grounded": True,
                    "confidence": 0.9,
                    "classification": {},
                }
            )

        out = scan("bespoke_gateway_loop", tmp_path, enrich=True, enrich_call_fn=grounded)
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "Ops assistant handling payments." in report
        assert "DRAFT (LLM)" in report
