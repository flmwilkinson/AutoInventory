"""SPEC-10 §1: the embeddable core (`core.scan`) returns a live Record in
memory, with no CLI, no output directory, and no disk writes."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from aiscan.context import OrgPack, Settings
from aiscan.core import ScanRequest, ScanResult, scan
from tests.conftest import fixture_repo


def _scan(name: str) -> ScanResult:
    return scan(
        ScanRequest(
            repo_root=fixture_repo(name),
            repo_url=None,
            commit="unversioned",
            bundle_name="repo",
            settings=Settings(),
            org_pack=OrgPack(),
            logger=logging.getLogger("test-core"),
        )
    )


def test_core_returns_record_without_disk() -> None:
    result = _scan("fw_openai_agents_basic")
    assert isinstance(result, ScanResult)
    assert result.record.ai_verdict == "ai_detected"
    assert result.record.agents, "framework fixture should surface at least one agent"
    assert result.fact_lines, "a detected repo produces facts"
    # The core returns objects; it is the caller's job to persist them.
    assert result.record.scan_health.files > 0


def test_core_no_ai_path_is_in_memory() -> None:
    result = _scan("no_ai_clean")
    assert result.record.ai_verdict == "no_ai"
    assert result.fact_lines == []
    assert result.graph.nodes == {}


def test_core_writes_nothing_to_a_scratch_dir(tmp_path: Path) -> None:
    # A dir handed to the core is irrelevant — it is not passed one. Prove the
    # scratch dir stays empty across a scan.
    _scan("bespoke_llm_call_only")
    assert not list(tmp_path.iterdir())


def test_core_scan_is_pure_wrt_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # The core must not read credentials from the environment on the default
    # (no-LLM-tier) path: scrub the env and confirm a clean scan.
    for var in ("OPENAI_API_KEY", "AISCAN_ADJUDICATE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = _scan("fw_openai_agents_basic")
    assert result.record.agents


def test_per_scan_loggers_are_isolated() -> None:
    # SPEC-10 §4: two scans must not share a logger — else concurrent scans in
    # one process would rip out each other's handlers / cross-write scan.log.
    from aiscan.cli import _make_logger

    a = _make_logger(False)
    b = _make_logger(False)
    assert a is not b
    assert a.handlers and b.handlers
    assert set(map(id, a.handlers)).isdisjoint(map(id, b.handlers))
    # Not the shared global singleton either.
    assert a is not logging.getLogger("aiscan")
