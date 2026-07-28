"""SPEC-3 V5: location-aware enrichment planning, --enrich-tests,
enrich-in-place, and E→G candidate wiring."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiscan.cli import run_scan
from aiscan.enrich.engine import enrich_record
from aiscan.inventory.schema import Record
from tests.conftest import FIXTURES, fixture_repo

LOGGER = logging.getLogger("test.enrich.integration")


def grounded(system: str, user: str) -> str:
    return json.dumps(
        {
            "summary": "Ops assistant handling account lookups and payments.",
            "one_line": "Payments ops assistant.",
            "capability_summary": "Account lookup; payment submission.",
            "data_interaction_summary": "Reads accounts; sends payments.",
            "human_oversight_summary": "None detected.",
            "responsibilities": "Account lookup and payment submission.",
            "guardrails_summary": "None detected.",
            "purpose": "Automate outbound payment instruction drafting.",
            "classification": {"suggested_aia_risk_category": "high"},
            "grounded": True,
            "confidence": 0.9,
        }
    )


def _record_with_test_agent() -> Record:
    return Record.model_validate(
        {
            "bundle_id": "repo:x",
            "name": "x",
            "ai_verdict": "ai_detected",
            "agents": [
                {
                    "agent_id": "prod-agent",
                    "location": "production",
                    "detection": {"method": "m", "confidence": "high", "evidence": ["p.py:1-9"]},
                },
                {
                    "agent_id": "test-agent",
                    "location": "test",
                    "detection": {
                        "method": "m",
                        "confidence": "high",
                        "evidence": ["tests/t.py:1-9"],
                    },
                },
            ],
        }
    )


class TestLocationAwarePlanning:
    def test_test_agents_skipped_by_default(self, tmp_path: Path) -> None:
        result = enrich_record(_record_with_test_agent(), tmp_path, LOGGER, call_fn=grounded)
        assert result.skipped_test_nodes == 1
        agents = {a.agent_id: a for a in result.record.agents}
        assert agents["prod-agent"].agent_summary.value is not None
        assert agents["test-agent"].agent_summary.value is None  # skipped, stays null
        note = result.record.scan_health.enrichment
        assert note is not None and note["skipped_test_nodes"] == 1

    def test_enrich_tests_flag_includes_them(self, tmp_path: Path) -> None:
        result = enrich_record(
            _record_with_test_agent(), tmp_path, LOGGER, call_fn=grounded, include_tests=True
        )
        assert result.skipped_test_nodes == 0
        agents = {a.agent_id: a for a in result.record.agents}
        assert agents["test-agent"].agent_summary.value is not None

    def test_system_and_production_consume_budget_first(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def spy(system: str, user: str) -> str:
            if "agent_id" in user:
                calls.append(user.split('"agent_id": "')[1].split('"')[0])
            else:
                calls.append("system")
            return grounded(system, user)

        enrich_record(
            _record_with_test_agent(), tmp_path, LOGGER, call_fn=spy, max_nodes=2,
            include_tests=True, max_workers=1,
        )
        # Budget of 2 with a test agent included: system + production win.
        assert sorted(calls) == ["prod-agent", "system"]


class TestCandidates:
    def test_purpose_and_regulatory_candidates(self, tmp_path: Path) -> None:
        org = FIXTURES / "bespoke_gateway_loop" / "org.yaml"
        out = run_scan(
            str(fixture_repo("bespoke_gateway_loop")),
            out=tmp_path,
            org_pack=org,
            enrich=True,
            enrich_call_fn=grounded,
        )
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        # Candidates filled, values never written (SPEC-3 §4.3).
        assert record["purpose"]["value"] is None
        assert record["purpose"]["candidate"] == "Automate outbound payment instruction drafting."
        assert record["regulatory_scope"]["value"] is None
        assert record["regulatory_scope"]["candidate"] == "high"


class TestEnrichInPlace:
    def test_round_trip_leaves_detection_artifacts_untouched(self, tmp_path: Path) -> None:
        org = FIXTURES / "bespoke_gateway_loop" / "org.yaml"
        out = run_scan(
            str(fixture_repo("bespoke_gateway_loop")), out=tmp_path, org_pack=org
        )
        graph_before = (out / "graph.json").read_bytes()
        facts_before = (out / "facts.jsonl").read_bytes()
        record_before = json.loads((out / "record.json").read_text(encoding="utf-8"))
        assert record_before["system_summary"]["value"] is None

        result_dir = run_scan(
            str(out),
            enrich=True,
            enrich_call_fn=grounded,
            repo=fixture_repo("bespoke_gateway_loop"),
        )
        assert result_dir == out
        assert (out / "graph.json").read_bytes() == graph_before
        assert (out / "facts.jsonl").read_bytes() == facts_before

        record_after = json.loads((out / "record.json").read_text(encoding="utf-8"))
        assert record_after["system_summary"]["value"] is not None
        note = record_after["scan_health"]["enrichment"]
        assert note["status"] == "ok" and "grounding" not in note  # real slices used
        report = (out / "report.html").read_text(encoding="utf-8")
        assert "DRAFT (LLM)" in report

    def test_without_repo_degrades_to_facts_only(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("bespoke_gateway_loop")), out=tmp_path)
        run_scan(str(out), enrich=True, enrich_call_fn=grounded)
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        assert record["scan_health"]["enrichment"]["grounding"] == "facts_only"
        assert record["system_summary"]["value"] is not None  # facts still enriched

    def test_requires_enrich_flag(self, tmp_path: Path) -> None:
        out = run_scan(str(fixture_repo("no_ai_clean")), out=tmp_path)
        try:
            run_scan(str(out))
        except Exception as exc:
            assert "--enrich" in str(exc)
        else:
            raise AssertionError("expected IngestError without --enrich")
