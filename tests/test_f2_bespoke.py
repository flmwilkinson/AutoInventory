"""F2 bespoke frontend units (P5): wrapper fixed point, seeds, registry."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.cli import discover_python_files
from aiscan.context import KnownWrapper, OrgPack, ResolverBudgets, ResolverStats
from aiscan.frontends.bespoke.wrappers import (
    WrapperAnalyzer,
    WrapperResult,
    load_registry,
    save_registry,
)
from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import ModuleGraph
from aiscan.parse.py_ast import AstParser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import SinkEngine
from tests.conftest import fixture_repo


def wrapper_pass(
    repo: Path,
    org_pack: OrgPack | None = None,
    registry_path: Path | None = None,
) -> tuple[WrapperResult, SinkEngine]:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for rel in discover_python_files(repo):
        mod = parser.parse(rel, (repo / rel).read_text(encoding="utf-8"))
        assert isinstance(mod, ModuleIR)
        modules[rel] = mod
    graph = ModuleGraph(modules)
    resolver = Resolver(graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats())
    engine = SinkEngine(resolver, org_pack or OrgPack(), repo_root=repo)
    sinks = engine.scan_all().sinks
    analyzer = WrapperAnalyzer(
        resolver, engine, org_pack or OrgPack(), load_registry(registry_path)
    )
    return analyzer.fixed_point(sinks), engine


BANK_ORG = OrgPack(
    gateway_hosts=("llm.bank.internal",),
    known_wrapper_packages=(
        KnownWrapper(fqname="legacy_ai.OldClient", attribution="passthrough"),
    ),
    sensitive_hosts=("itsm.internal.example",),
)


class TestWrapperFixedPoint:
    def test_llmclient_classified_with_no_signature(self) -> None:
        """SPEC §2 item 3: the wrapper proof — classified by the fixed point."""
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        info = result.classified["bank_ai.client.LLMClient"]
        assert info.kind == "class"
        assert info.attribution == "default"
        assert info.resolved_model == "bank-small-1"
        assert info.endpoint == "https://llm.bank.internal/v1/chat/completions"
        assert info.ctor_model_param == "model"
        assert info.source == "derived"
        assert info.score is not None and info.score >= 3
        assert "bank_ai.client.LLMClient" in result.used_wrappers

    def test_wrapper_call_site_becomes_sink(self) -> None:
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        app_sinks = [s for s in result.wrapper_sinks if s.site.path == "app.py"]
        assert len(app_sinks) == 1
        assert app_sinks[0].model.model == "bank-small-1"
        assert app_sinks[0].model.method == "attribution:wrapper_default"

    def test_org_pack_seed_classifies_external_wrapper(self) -> None:
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        legacy_sinks = [s for s in result.wrapper_sinks if s.site.path == "reporting.py"]
        assert len(legacy_sinks) == 1
        assert legacy_sinks[0].model.model == "legacy-1"
        assert legacy_sinks[0].model.method == "attribution:literal"

    def test_second_order_wrapper(self) -> None:
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper_of_wrapper"))
        assert "helpers.ask_llm" in result.classified
        info = result.classified["helpers.ask_llm"]
        assert info.kind == "function"
        assert info.resolved_model == "bank-small-1"
        main_sinks = [s for s in result.wrapper_sinks if s.site.path == "main.py"]
        assert len(main_sinks) == 1
        assert main_sinks[0].model.model == "bank-small-1"

    def test_wrapper_internal_sinks_excluded(self) -> None:
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper_of_wrapper"))
        files_with_excluded = {s.file for s in result.wrapper_def_spans}
        assert "bank_ai/client.py" in files_with_excluded
        assert "helpers.py" in files_with_excluded

    def test_test_and_main_sinks_never_classify_wrappers(self) -> None:
        result, _ = wrapper_pass(fixture_repo("adversarial_test_sink"))
        assert result.used_wrappers == set()
        assert result.wrapper_sinks == []


class TestRegistry:
    def test_round_trip(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "org_registry.json"
        result, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        derived = {
            fq: info
            for fq, info in result.classified.items()
            if fq in result.used_wrappers and info.source == "derived"
        }
        save_registry(reg_path, derived)
        loaded = load_registry(reg_path)
        assert "bank_ai.client.LLMClient" in loaded
        assert loaded["bank_ai.client.LLMClient"].source == "registry"
        assert loaded["bank_ai.client.LLMClient"].content_hash is not None

    def test_reused_registry_produces_same_sinks(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "org_registry.json"
        first, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        save_registry(
            reg_path,
            {fq: i for fq, i in first.classified.items() if i.source == "derived"},
        )
        second, _ = wrapper_pass(
            fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG, registry_path=reg_path
        )
        assert {s.span for s in first.wrapper_sinks} == {s.span for s in second.wrapper_sinks}

    def test_stale_hash_rederives(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "org_registry.json"
        first, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        info = first.classified["bank_ai.client.LLMClient"]
        stale = info.model_copy(update={"content_hash": "deadbeef", "resolved_model": "wrong"})
        save_registry(reg_path, {stale.fq: stale})
        second, _ = wrapper_pass(
            fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG, registry_path=reg_path
        )
        assert second.classified["bank_ai.client.LLMClient"].resolved_model == "bank-small-1"

    def test_registry_file_shape(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "org_registry.json"
        first, _ = wrapper_pass(fixture_repo("bespoke_wrapper"), org_pack=BANK_ORG)
        save_registry(
            reg_path,
            {fq: i for fq, i in first.classified.items() if i.source == "derived"},
        )
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        assert isinstance(data["entries"], list)
        assert all("fq" in e and "evidence" in e for e in data["entries"])
