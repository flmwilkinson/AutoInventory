"""SPEC-4 W1: TS module graph — specifier resolution, barrels, aliases,
workspaces, npm versions, and python/TS coexistence. End-to-end through the
real resolver where it matters."""

from __future__ import annotations

from pathlib import Path

from aiscan.context import ResolverBudgets, ResolverStats
from aiscan.ir.nodes import CallE, ModuleIR, NameE
from aiscan.ir.values import ClassRef, FuncRef, PackageRef
from aiscan.ir.walk import dotted_name, iter_exprs
from aiscan.modules.graph import ModuleGraph
from aiscan.modules.symbols import ModuleImport, SymbolImport
from aiscan.modules.ts_graph import TsConfig, read_ts_config, resolve_specifier
from aiscan.parse.registry import make_parser
from aiscan.resolve.engine import Resolver
from tests.test_resolver import resolve_at, the


def make_mixed(
    sources: dict[str, str], ts_config: TsConfig | None = None
) -> tuple[Resolver, ModuleGraph]:
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        parser = make_parser("." + path.rsplit(".", 1)[-1])
        assert parser is not None, path
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR), f"{path}: {mod}"
        modules[path] = mod
    graph = ModuleGraph(modules, {}, ts_config=ts_config)
    resolver = Resolver(
        graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats()
    )
    return resolver, graph


def use_of(graph: ModuleGraph, module: str, name: str) -> NameE:
    for e in iter_exprs(graph.by_name[module].body, into_defs=True):
        if (
            isinstance(e, CallE)
            and dotted_name(e.callee) == "use"
            and isinstance(e.args[0], NameE)
            and e.args[0].name == name
        ):
            arg = e.args[0]
            assert isinstance(arg, NameE)
            return arg
    raise AssertionError(f"use({name}) not found in {module}")


class TestSpecifierResolution:
    def test_relative_extension_omitted(self) -> None:
        _, graph = make_mixed(
            {
                "src/agent.ts": "export function run() { return 1; }\n",
                "src/app.ts": "import { run } from './agent';\nuse(run);\n",
            }
        )
        tables = graph.build_symbol_tables()
        binding = tables["src/app"].imports["run"]
        assert binding == SymbolImport(module="src/agent", name="run")

    def test_index_resolution(self) -> None:
        _, graph = make_mixed(
            {
                "src/core/index.ts": "export function boot() { return 1; }\n",
                "src/app.ts": "import { boot } from './core';\n",
            }
        )
        binding = graph.build_symbol_tables()["src/app"].imports["boot"]
        assert binding == SymbolImport(module="src/core/index", name="boot")

    def test_barrel_reexport_resolves_to_definition(self) -> None:
        resolver, graph = make_mixed(
            {
                "src/core/agent.ts": "export class Agent { run() { return 1; } }\n",
                "src/core/index.ts": "export { Agent } from './agent';\n",
                "src/app.ts": "import { Agent } from './core';\nuse(Agent);\n",
            }
        )
        value = the(resolve_at(resolver, graph, "src/app", use_of(graph, "src/app", "Agent")))
        assert isinstance(value, ClassRef)
        assert value.fq == "src/core/agent.Agent"  # chased through the barrel

    def test_external_package_with_npm_version(self) -> None:
        resolver, graph = make_mixed(
            {"src/app.ts": "import OpenAI from 'openai';\nuse(OpenAI);\n"},
            ts_config=TsConfig(npm_versions={"openai": "4.28.0"}),
        )
        value = the(resolve_at(resolver, graph, "src/app", use_of(graph, "src/app", "OpenAI")))
        assert value == PackageRef(name="openai.default", version="4.28.0")

    def test_namespace_import_of_internal_module(self) -> None:
        _resolver, graph = make_mixed(
            {
                "src/tools.ts": "export function send() { return 1; }\n",
                "src/app.ts": "import * as tools from './tools';\nuse(tools);\n",
            }
        )
        tables = graph.build_symbol_tables()
        assert tables["src/app"].imports["tools"] == ModuleImport(module="src/tools")

    def test_tsconfig_alias(self) -> None:
        config = TsConfig(aliases=(("", "@lib", "src/lib"),))
        _, graph = make_mixed(
            {
                "src/lib/client.ts": "export function ask() { return 1; }\n",
                "src/app.ts": "import { ask } from '@lib/client';\nuse(ask);\n",
            },
            ts_config=config,
        )
        binding = graph.build_symbol_tables()["src/app"].imports["ask"]
        assert binding == SymbolImport(module="src/lib/client", name="ask")

    def test_workspace_package(self) -> None:
        config = TsConfig(workspace_packages={"@docgen/core": "packages/core"})
        resolver, graph = make_mixed(
            {
                "packages/core/src/index.ts": "export function plan() { return 1; }\n",
                "apps/web/main.ts": "import { plan } from '@docgen/core';\nuse(plan);\n",
            },
            ts_config=config,
        )
        value = the(
            resolve_at(resolver, graph, "apps/web/main", use_of(graph, "apps/web/main", "plan"))
        )
        assert isinstance(value, FuncRef)
        assert value.fq == "packages/core/src/index.plan"

    def test_unresolvable_relative_stays_external(self) -> None:
        assert (
            resolve_specifier("src/app", "./missing", frozenset({"src/app"}), TsConfig())
            == "./missing"
        )

    def test_require_binds_module(self) -> None:
        _, graph = make_mixed({"app.js": "const oa = require('openai');\nuse(oa);\n"})
        assert graph.build_symbol_tables()["app"].imports["oa"] == ModuleImport(module="openai")


class TestMixedGraph:
    def test_python_unaffected_and_no_name_clashes(self) -> None:
        resolver, graph = make_mixed(
            {
                "pkg/__init__.py": "",
                "pkg/mod.py": "VALUE = 'py'\n",
                "app.py": "from pkg.mod import VALUE\nuse(VALUE)\n",
                "src/app.ts": "const x = 1;\n",
            }
        )
        assert "pkg.mod" in graph.by_name and "src/app" in graph.by_name
        assert graph.ts_modules == {"src/app"}
        value = the(resolve_at(resolver, graph, "app", use_of(graph, "app", "VALUE")))
        assert getattr(value, "s", None) == "py"


class TestReadTsConfig:
    def test_reads_aliases_workspaces_locks(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text(
            '{\n  // comment\n  "compilerOptions": {\n'
            '    "baseUrl": ".",\n    "paths": { "@lib/*": ["src/lib/*"], },\n  }\n}\n',
            encoding="utf-8",
        )
        pkg = tmp_path / "packages" / "core"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "@docgen/core"}', encoding="utf-8")
        (tmp_path / "package.json").write_text('{"name": "root"}', encoding="utf-8")
        (tmp_path / "package-lock.json").write_text(
            '{"packages": {"node_modules/openai": {"version": "4.28.0"}}}',
            encoding="utf-8",
        )
        (tmp_path / "pnpm-lock.yaml").write_text(
            "packages:\n  '@anthropic-ai/sdk@0.32.1':\n    resolution: {}\n",
            encoding="utf-8",
        )
        config = read_ts_config(tmp_path)
        assert ("", "@lib", "src/lib") in config.aliases
        assert config.workspace_packages["@docgen/core"] == "packages/core"
        assert config.npm_versions["openai"] == "4.28.0"
        assert config.npm_versions["@anthropic-ai/sdk"] == "0.32.1"
