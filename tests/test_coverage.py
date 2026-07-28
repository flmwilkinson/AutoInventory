"""SPEC-3 coverage honesty (the DocGen case): a TypeScript repo with real LLM
usage must never yield a false-comfort record — the npm AI dependency lands in
the BOM, the unanalysed code raises a finding, and the report says loudly that
coverage was partial."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiscan.cli import run_scan
from aiscan.ingest.triage import count_source_files
from tests.conftest import fixture_repo


def scan(name: str, tmp_path: Path) -> tuple[dict[str, Any], str]:
    out = run_scan(str(fixture_repo(name)), out=tmp_path / name)
    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    report = (out / "report.html").read_text(encoding="utf-8")
    return record, report


class TestPolyglotRepo:
    def test_ts_agent_now_detected(self, tmp_path: Path) -> None:
        # SPEC-4: the TS agent loop is analysed end-to-end, so a pure-TS AI repo
        # is now a full detection — never a false no_ai attestation.
        record, _ = scan("polyglot_ts", tmp_path)
        assert record["ai_verdict"] == "ai_detected"
        agents = record["agents"]
        assert any(a["language"] == "typescript" for a in agents)

    def test_npm_dep_in_bom_now_used(self, tmp_path: Path) -> None:
        record, _ = scan("polyglot_ts", tmp_path)
        rows = {r["package"]: r for r in record["ai_dependencies"]}
        assert rows["openai"]["ecosystem"] == "npm"
        assert rows["openai"]["source"] == "package.json"
        assert rows["openai"]["used"] is True  # imported in analysed TS
        assert "react" not in rows and "typescript" not in rows

    def test_unsupported_language_still_flagged(self, tmp_path: Path) -> None:
        # The Java file is a language aiscan cannot read — coverage honesty holds.
        record, report = scan("polyglot_ts", tmp_path)
        finding = next(
            f for f in record["findings"] if f["kind"] == "unanalysed_language_code"
        )
        assert finding["severity"] == "medium"  # AI signals + unanalysed code
        assert ".java" in finding["detail"]
        assert ".ts" not in finding["detail"]  # TS IS analysed now
        assert record["scan_health"]["language_files"][".java"] == 1
        assert "Partial coverage" in report

    def test_pure_python_repo_has_no_banner(self, tmp_path: Path) -> None:
        record, report = scan("bespoke_gateway_loop", tmp_path)
        assert "Partial coverage" not in report
        assert not any(
            f["kind"] == "unanalysed_language_code" for f in record["findings"]
        )

    def test_census_counts_all_source_languages(self) -> None:
        counts = count_source_files(fixture_repo("polyglot_ts"))
        assert counts.get(".ts") == 1 and counts.get(".py") == 1 and counts.get(".java") == 1
