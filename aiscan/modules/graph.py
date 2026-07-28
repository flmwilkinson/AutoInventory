"""Module graph: repo-internal import resolution (SPEC §6.3).

Maps module names to parsed files, resolves absolute and relative imports,
detects ``src/`` layouts, and reads dependency versions from lockfiles so
external imports resolve to ``PackageRef(name, version)``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace as dc_replace
from pathlib import Path

from aiscan.ir.nodes import ModuleIR
from aiscan.modules.symbols import SymbolTable, build_symbol_table
from aiscan.modules.ts_graph import TsConfig, resolve_specifier, ts_module_name

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


def _norm_pkg(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def read_package_versions(repo_root: Path) -> dict[str, str]:
    """Best-effort ``{normalised package name: version}`` from lockfiles."""
    versions: dict[str, str] = {}
    for req in sorted(repo_root.glob("requirements*.txt")):
        try:
            text = req.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _REQ_LINE.match(line)
            if m:
                versions[_norm_pkg(m.group(1))] = m.group(2)
    for lock_name in ("poetry.lock", "uv.lock"):
        lock = repo_root / lock_name
        if not lock.is_file():
            continue
        try:
            data = tomllib.loads(lock.read_text(encoding="utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, OSError):
            continue
        packages = data.get("package", [])
        if isinstance(packages, list):
            for pkg in packages:
                if isinstance(pkg, dict):
                    name = pkg.get("name")
                    version = pkg.get("version")
                    if isinstance(name, str) and isinstance(version, str):
                        versions[_norm_pkg(name)] = version
    return versions


def detect_source_roots(paths: list[str]) -> list[str]:
    """Repo-relative source roots, longest first. Always includes the repo
    root (``""``); adds ``src`` when a ``src/<pkg>/__init__.py`` layout exists."""
    roots = [""]
    for p in paths:
        parts = p.split("/")
        if len(parts) == 3 and parts[0] == "src" and parts[2] == "__init__.py":
            roots.append("src")
            break
    return sorted(roots, key=len, reverse=True)


def module_name_for(path: str, roots: list[str]) -> str:
    """Dotted module name for a repo-relative posix path, or "" if unmappable."""
    for root in roots:
        prefix = root + "/" if root else ""
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix) :]
        if not rel.endswith(".py"):
            return ""
        dotted = rel[: -len(".py")].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        elif dotted == "__init__":
            dotted = ""
        return dotted
    return ""


class ModuleGraph:
    """Name↔file maps plus import resolution over the parsed module set."""

    def __init__(
        self,
        modules_by_path: dict[str, ModuleIR],
        package_versions: dict[str, str] | None = None,
        ts_config: TsConfig | None = None,
    ) -> None:
        self.modules_by_path = modules_by_path
        self.package_versions = package_versions or {}
        self.ts_config = ts_config or TsConfig()
        self.source_roots = detect_source_roots(sorted(modules_by_path))
        self.by_name: dict[str, ModuleIR] = {}
        self.name_by_path: dict[str, str] = {}
        self.packages: set[str] = set()
        self.ts_modules: set[str] = set()
        for path in sorted(modules_by_path):
            name = module_name_for(path, self.source_roots)
            if not name:
                # SPEC-4: TS/JS modules are named by path-without-extension.
                ts_name = ts_module_name(path)
                if ts_name and ts_name not in self.by_name:
                    self.by_name[ts_name] = modules_by_path[path]
                    self.name_by_path[path] = ts_name
                    self.ts_modules.add(ts_name)
                continue
            if path.endswith("/__init__.py") or path == "__init__.py":
                self.packages.add(name)
            # First mapping wins (roots are longest-first, so src/ takes precedence).
            if name not in self.by_name:
                self.by_name[name] = modules_by_path[path]
                self.name_by_path[path] = name
            # Namespace packages: every dotted prefix acts as a package.
            parts = name.split(".")
            for i in range(1, len(parts)):
                self.packages.add(".".join(parts[:i]))

    def has_module(self, name: str) -> bool:
        return name in self.by_name

    def resolve_relative(self, importer: str, level: int, module: str) -> str:
        """Absolute module name for ``from <'.'*level><module> import ...``."""
        parts = importer.split(".") if importer else []
        if importer not in self.packages and parts:
            parts = parts[:-1]  # plain module: '.' refers to its parent package
        drop = level - 1
        if drop:
            parts = parts[: max(0, len(parts) - drop)]
        if module:
            parts = [*parts, *module.split(".")]
        return ".".join(parts)

    def version_of(self, package: str) -> str | None:
        first = package.split(".")[0]
        npm = self.ts_config.npm_versions.get(first)
        if npm is not None:
            return npm
        return self.package_versions.get(_norm_pkg(first))

    def build_symbol_tables(self) -> dict[str, SymbolTable]:
        tables: dict[str, SymbolTable] = {}
        ts_names = frozenset(self.ts_modules)
        for name in sorted(self.by_name):
            mod = self.by_name[name]

            def resolve(level: int, module: str, _importer: str = name) -> str:
                return self.resolve_relative(_importer, level, module)

            table = build_symbol_table(name, mod, resolve)
            if name in self.ts_modules:
                self._remap_ts_imports(table, mod.path, ts_names)
            tables[name] = table
        return tables

    def _remap_ts_imports(
        self, table: SymbolTable, path: str, ts_names: frozenset[str]
    ) -> None:
        """SPEC-4 §3: rewrite raw TS import specifiers to internal module
        names where the target was parsed; unresolved specifiers stay raw and
        become external ``PackageRef``s downstream."""
        for alias, binding in list(table.imports.items()):
            resolved = resolve_specifier(path, binding.module, ts_names, self.ts_config)
            if resolved != binding.module:
                table.imports[alias] = dc_replace(binding, module=resolved)
        if table.star_imports:
            table.star_imports = tuple(
                resolve_specifier(path, star, ts_names, self.ts_config)
                for star in table.star_imports
            )
