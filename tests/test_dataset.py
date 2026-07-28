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


class TestWriteThrough:
    """SPEC-10 §K: the persistent, idempotent write-through path."""

    def _load(self, records_root: Path, name: str) -> dict[str, object]:
        import json

        path = next((records_root / name).rglob("record.json"))
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def test_upsert_idempotent_and_distinct(
        self, records_root: Path, tmp_path: Path
    ) -> None:
        from aiscan.dataset.store import dump_tables, upsert_record

        db = tmp_path / "wt.db"
        a = self._load(records_root, "bespoke_llm_call_only")
        b = self._load(records_root, "derived_indicators")
        sid1 = upsert_record(db, a)
        sid2 = upsert_record(db, a)  # same content -> same scan_id -> replaced
        assert sid1 == sid2
        assert len(dump_tables(db)["systems"]) == 1  # idempotent, not duplicated
        upsert_record(db, b)
        assert len(dump_tables(db)["systems"]) == 2  # distinct records both kept

    def test_new_tables_present_and_populated(self, dataset: Path) -> None:
        tables = dump_tables(dataset / "inventory.db")
        assert "mcp_servers" in tables and "model_usages" in tables
        # bespoke_llm_call_only issues a bare LLM call -> at least one usage row.
        assert len(tables["model_usages"]) >= 1

    def test_agent_tools_bridge_and_liveness(self, tmp_path: Path) -> None:
        # SPEC_INVENTORY: the agent->tool join is now answerable, and the
        # liveness tier is surfaced onto the agent row.
        import json

        from aiscan.dataset.store import _COLUMNS, dump_tables, upsert_record
        from tests.harness import run_fixture

        scan_dir = run_fixture("fw_openai_agents_basic", tmp_path / "s")
        rec = json.loads((scan_dir / "record.json").read_text(encoding="utf-8"))
        db = tmp_path / "inv.db"
        upsert_record(db, rec)
        tables = dump_tables(db)
        assert tables["agent_tools"], "agent->tool bridge should link >=1 tool"
        live_idx = _COLUMNS["agents"].index("liveness")
        tiers = {r[live_idx] for r in tables["agents"]}
        assert "invoked" in tiers  # the Runner.run target
        assert tiers <= {"invoked", "reachable", "defined"}  # never a hard "dormant"

    def test_bom_diff_reports_removed_agent(
        self, records_root: Path, tmp_path: Path
    ) -> None:
        import copy

        from aiscan.dataset.store import bom_diff, upsert_record

        db = tmp_path / "diff.db"
        rec = self._load(records_root, "derived_indicators")
        assert rec.get("agents"), "fixture should have agents to remove"
        base = copy.deepcopy(rec)
        base["scanned_commit"] = "aaaaaaaa"
        head = copy.deepcopy(rec)
        head["scanned_commit"] = "bbbbbbbb"
        removed = head["agents"].pop(0)["agent_id"]  # type: ignore[index]
        upsert_record(db, base)
        upsert_record(db, head)
        diff = bom_diff(db, "aaaaaaaa", "bbbbbbbb")
        assert removed in diff["agents"]["removed"]
