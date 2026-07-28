"""SPEC-3 V0: three-way ai_verdict, negative attestation, and the LLM guard
(zero network calls on a no_ai verdict, visible skip notes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiscan.cli import run_scan
from tests.conftest import fixture_repo


def scan_record(name: str, tmp_path: Path, **kwargs: object) -> dict[str, Any]:
    out = run_scan(str(fixture_repo(name)), out=tmp_path / name, **kwargs)  # type: ignore[arg-type]
    loaded: dict[str, Any] = json.loads((out / "record.json").read_text(encoding="utf-8"))
    return loaded


class TestVerdicts:
    def test_no_ai_clean(self, tmp_path: Path) -> None:
        record = scan_record("no_ai_clean", tmp_path)
        assert record["ai_verdict"] == "no_ai"
        assert record["no_ai_detected"] is True
        assert record["agents"] == [] and record["model_usages"] == []
        # Negative attestation is complete: what was checked, when, by what.
        assert record["scan_health"]["triage"] == {}
        assert record["scanned_commit"]
        assert record["inventory_provenance"]["scanner"].startswith("aiscan")

    def test_no_ai_is_a_fast_exit(self, tmp_path: Path) -> None:
        record = scan_record("no_ai_clean", tmp_path)
        stage_ms = record["scan_health"]["stage_ms"]
        # Structural proof of the fast path: only triage ran — no parse,
        # resolve, sink, or frontend stage ever started.
        assert set(stage_ms) == {"triage"}
        assert stage_ms["triage"] < 10_000  # loose SPEC-3 §2.2 budget

    def test_ai_deps_only(self, tmp_path: Path) -> None:
        record = scan_record("ai_deps_only", tmp_path)
        assert record["ai_verdict"] == "ai_signals_only"
        assert record["no_ai_detected"] is False
        assert record["agents"] == [] and record["model_usages"] == []
        # Triage explains why the full pipeline ran: the dormant dependency.
        triage = record["scan_health"]["triage"]
        assert any("openai" in " ".join(v) for v in triage.values())
        # Full pipeline DID run (no mid-pipeline bailout).
        assert "parse" in record["scan_health"]["stage_ms"]

    def test_ai_detected(self, tmp_path: Path) -> None:
        record = scan_record("bespoke_gateway_loop", tmp_path)
        assert record["ai_verdict"] == "ai_detected"
        assert record["no_ai_detected"] is False


class TestLlmGuard:
    def test_no_ai_makes_zero_llm_calls_with_visible_notes(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def counting(system: str, user: str) -> str:
            calls["n"] += 1
            return "{}"

        record = scan_record(
            "no_ai_clean",
            tmp_path,
            enrich=True,
            enrich_call_fn=counting,
            adjudicate=True,
            adjudicate_call_fn=counting,
        )
        assert calls["n"] == 0
        # The guard is visible, never a silent no-op (SPEC-3 §2.2).
        assert record["scan_health"]["enrichment"] == {
            "status": "skipped",
            "reason": "no_ai verdict",
        }
        assert record["scan_health"]["adjudication"] == {
            "status": "skipped",
            "reason": "no_ai verdict",
        }

    def test_signals_only_enriches_at_most_the_system_node(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def grounded(system: str, user: str) -> str:
            calls["n"] += 1
            return json.dumps(
                {
                    "summary": "A batch job with a dormant openai dependency.",
                    "one_line": "Dormant AI dependency.",
                    "grounded": True,
                    "confidence": 0.8,
                    "classification": {},
                }
            )

        record = scan_record("ai_deps_only", tmp_path, enrich=True, enrich_call_fn=grounded)
        assert calls["n"] == 1  # system node only — nothing else exists
        assert record["system_summary"]["value"] is not None
