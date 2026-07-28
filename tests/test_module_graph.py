"""Module graph and symbol table tests (SPEC §6.3)."""

from __future__ import annotations

from pathlib import Path

from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import (
    ModuleGraph,
    detect_source_roots,
    module_name_for,
    read_package_versions,
)
from aiscan.modules.symbols import ModuleImport, SymbolImport
from aiscan.parse.py_ast import AstParser


def build_graph(sources: dict[str, str]) -> ModuleGraph:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR), f"{path}: {mod}"
        modules[path] = mod
    return ModuleGraph(modules)


class TestNaming:
    def test_plain_module(self) -> None:
        assert module_name_for("app/loop.py", [""]) == "app.loop"

    def test_package_init(self) -> None:
        assert module_name_for("pkg/__init__.py", [""]) == "pkg"

    def test_src_layout(self) -> None:
        roots = detect_source_roots(["src/mypkg/__init__.py", "src/mypkg/core.py"])
        assert roots == ["src", ""]
        assert module_name_for("src/mypkg/core.py", roots) == "mypkg.core"

    def test_root_module(self) -> None:
        assert module_name_for("main.py", [""]) == "main"


class TestImportResolution:
    def test_absolute_and_alias(self) -> None:
        graph = build_graph(
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "VALUE = 1\n",
                "main.py": "import pkg.util as u\nfrom pkg.util import VALUE\n",
            }
        )
        tables = graph.build_symbol_tables()
        main = tables["main"]
        assert main.imports["u"] == ModuleImport(module="pkg.util")
        assert main.imports["VALUE"] == SymbolImport(module="pkg.util", name="VALUE")

    def test_plain_import_binds_top_package(self) -> None:
        graph = build_graph({"main.py": "import os.path\n"})
        tables = graph.build_symbol_tables()
        assert tables["main"].imports["os"] == ModuleImport(module="os")

    def test_relative_from_module(self) -> None:
        graph = build_graph(
            {
                "pkg/__init__.py": "",
                "pkg/a.py": "from .b import thing\n",
                "pkg/b.py": "thing = 1\n",
            }
        )
        tables = graph.build_symbol_tables()
        assert tables["pkg.a"].imports["thing"] == SymbolImport(module="pkg.b", name="thing")

    def test_relative_from_package_init(self) -> None:
        graph = build_graph(
            {
                "pkg/__init__.py": "from .client import C\n",
                "pkg/client.py": "class C:\n    pass\n",
            }
        )
        tables = graph.build_symbol_tables()
        assert tables["pkg"].imports["C"] == SymbolImport(module="pkg.client", name="C")

    def test_relative_two_levels(self) -> None:
        graph = build_graph(
            {
                "pkg/__init__.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/mod.py": "from ..top import x\n",
                "pkg/top.py": "x = 1\n",
            }
        )
        tables = graph.build_symbol_tables()
        assert tables["pkg.sub.mod"].imports["x"] == SymbolImport(module="pkg.top", name="x")

    def test_star_import_recorded(self) -> None:
        graph = build_graph(
            {
                "lib.py": "a = 1\n",
                "main.py": "from lib import *\n",
            }
        )
        tables = graph.build_symbol_tables()
        assert tables["main"].star_imports == ("lib",)

    def test_symbols_capture_defs_and_assigns(self) -> None:
        graph = build_graph(
            {
                "m.py": "X = 1\nX = 2\n\ndef f():\n    return X\n\nclass K:\n    pass\n",
            }
        )
        table = graph.build_symbol_tables()["m"]
        assert len(table.assignments["X"]) == 2
        assert "f" in table.functions
        assert "K" in table.classes

    def test_namespace_package(self) -> None:
        graph = build_graph({"ns/mod.py": "y = 1\n"})
        assert graph.has_module("ns.mod")
        assert "ns" in graph.packages


class TestLockfiles:
    def test_requirements_versions(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text(
            "openai==1.59.7\nlangchain-openai==0.2.14  # pinned\nunpinned>=1.0\n",
            encoding="utf-8",
        )
        versions = read_package_versions(tmp_path)
        assert versions["openai"] == "1.59.7"
        assert versions["langchain_openai"] == "0.2.14"
        assert "unpinned" not in versions

    def test_poetry_lock_versions(self, tmp_path: Path) -> None:
        (tmp_path / "poetry.lock").write_text(
            '[[package]]\nname = "anthropic"\nversion = "0.43.0"\n', encoding="utf-8"
        )
        versions = read_package_versions(tmp_path)
        assert versions["anthropic"] == "0.43.0"

    def test_graph_version_lookup(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("PyYAML==6.0.3\n", encoding="utf-8")
        graph = ModuleGraph({}, read_package_versions(tmp_path))
        assert graph.version_of("yaml") is None  # import name != distribution name
        assert graph.version_of("pyyaml") == "6.0.3"
