"""SPEC-3 V6: dataset — pure projection, rebuild identity, named queries."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiscan.dataset.queries import QUERIES, run_query
from aiscan.dataset.store import dump_tables, rebuild_dataset
from tests.harness import run_fixture

CORPUS = ("derived_indicators", "ai_deps_only", "no_ai_clean", "bespoke_llm_call_only")


@pytest.fixture(scope="module")
def records_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("records")
    for name in CORPUS:
        run_fixture(name, root / name)
    return root


@pytest.fixture(scope="module")
def dataset(records_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dataset")
    count = rebuild_dataset(records_root, out)
    assert count == len(CORPUS)
    return out


class TestRebuildIdentity:
    def test_rebuild_twice_is_identical(
        self, records_root: Path, tmp_path: Path, dataset: Path
    ) -> None:
        again = tmp_path / "again"
        rebuild_dataset(records_root, again)
        assert dump_tables(dataset / "inventory.db") == dump_tables(again / "inventory.db")
        for name in ("systems", "agents", "tools", "models", "findings"):
            a = (dataset / "csv" / f"{name}.csv").read_bytes()
            b = (again / "csv" / f"{name}.csv").read_bytes()
            assert a == b, f"csv/{name}.csv not byte-identical"


class TestTables:
    def test_systems_rows(self, dataset: Path) -> None:
        rows = dump_tables(dataset / "inventory.db")["systems"]
        assert len(rows) == len(CORPUS)
        verdicts = {r[0]: r[5] for r in rows}  # bundle_id -> ai_verdict
        assert verdicts["repo:repo"] in ("no_ai", "ai_signals_only", "ai_detected")

    def test_agents_capability_columns(self, dataset: Path) -> None:
        rows = run_query(dataset / "inventory.db", "agents_move_money")
        # derived_indicators' run_agent moves money (payment_hosts declared).
        assert any(r[1] == "run-agent" for r in rows)


class TestQueries:
    def test_all_queries_execute(self, dataset: Path) -> None:
        for name in QUERIES:
            run_query(dataset / "inventory.db", name)  # no exceptions

    def test_dormant_repos(self, dataset: Path) -> None:
        rows = run_query(dataset / "inventory.db", "dormant_ai_repos")
        assert len(rows) == 1  # ai_deps_only only

    def test_models_by_provider_class(self, dataset: Path) -> None:
        rows = run_query(dataset / "inventory.db", "models_by_provider_class")
        classes = {r[0] for r in rows}
        assert "internal_gateway" in classes  # derived_indicators gateway model
        assert "vendor_external" in classes  # bespoke_llm_call_only SDK call

    def test_high_findings(self, dataset: Path) -> None:
        rows = run_query(dataset / "inventory.db", "high_findings")
        kinds = {r[1] for r in rows}
        assert "high_privilege_agent" in kinds
