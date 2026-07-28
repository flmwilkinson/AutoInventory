"""Golden end-to-end tests: scanner output must match fixture goldens exactly
(timestamps excluded). The list grows as phases land detectors."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.harness import compare_fixture

GOLDEN_FIXTURES = [
    "fw_openai_agents_basic",  # P3
    "fw_langgraph_react",  # P4
    "fw_langgraph_nodes",  # P4
    "fw_crewai_crew",  # P4
    "adversarial_agent_name",  # P4: must stay empty
    "bespoke_gateway_loop",  # P5: headline gateway attribution proof
    "bespoke_raw_openai_loop",  # P5
    "bespoke_wrapper",  # P5: wrapper fixed-point proof
    "bespoke_wrapper_of_wrapper",  # P5
    "bespoke_llm_call_only",  # P5: LLMCallSite, no agent
    "azure_deployment",  # P5
    "dynamic_prompt",  # P5
    "adversarial_test_sink",  # P5: flagged locations only
    "adversarial_no_exec",  # P5
    "secret_literal",  # P5
]


@pytest.mark.parametrize("name", GOLDEN_FIXTURES)
def test_golden(name: str, tmp_path: Path) -> None:
    diffs = compare_fixture(name, tmp_path)
    assert not diffs, "\n".join(diffs)
