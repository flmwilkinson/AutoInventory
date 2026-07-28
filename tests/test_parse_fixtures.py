"""P0 exit gate: every fixture repo parses cleanly into IR."""

from __future__ import annotations

import pytest

from aiscan.cli import discover_source_files
from aiscan.ir.nodes import ModuleIR
from aiscan.parse.registry import make_parser
from tests.conftest import all_fixture_names, fixture_repo

EXPECTED_FIXTURES = {
    "adversarial_agent_name",
    "adversarial_html_escape",
    "adversarial_no_exec",
    "adversarial_test_sink",
    "ai_deps_only",
    "azure_deployment",
    "bespoke_gateway_loop",
    "bespoke_llm_call_only",
    "bespoke_raw_openai_loop",
    "bespoke_wrapper",
    "bespoke_wrapper_of_wrapper",
    "derived_indicators",
    "dynamic_prompt",
    "fw_crewai_crew",
    "fw_langgraph_nodes",
    "fw_langgraph_react",
    "fw_openai_agents_basic",
    "no_ai_clean",
    "polyglot_ts",
    "secret_literal",
    "ts_bespoke_gateway_loop",
    "ts_fw_openai_agents",
    "ts_no_exec",
}


def test_all_fixtures_present() -> None:
    assert set(all_fixture_names()) == EXPECTED_FIXTURES


@pytest.mark.parametrize("name", sorted(EXPECTED_FIXTURES))
def test_fixture_parses(name: str) -> None:
    repo = fixture_repo(name)
    files = discover_source_files(repo)
    assert files, f"fixture {name} has no source files"
    for rel in files:
        ext = "." + rel.rsplit(".", 1)[-1].lower()
        parser = make_parser(ext)
        assert parser is not None
        source = (repo / rel).read_text(encoding="utf-8")
        result = parser.parse(rel, source)
        assert isinstance(result, ModuleIR), f"{name}/{rel}: {result}"
