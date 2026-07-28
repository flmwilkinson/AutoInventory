"""Module dependency graph and reverse-dependency closure.

The incremental scan must re-analyse not just the changed modules but everything
whose *analysis result* could differ because of them — chiefly a changed
module's importers and callers ("how that diff is then called"). We derive that
set from the static import edges already present in the symbol tables, take the
reverse-reachable closure, and widen conservatively for the cases static edges
cannot see (lazy re-exports, star imports). When in doubt, a module is treated
as affected; the cost of an extra re-analysis is a slower scan, never a wrong
BOM.
"""

from __future__ import annotations

from aiscan.modules.symbols import ModuleImport, SymbolImport, SymbolTable


def build_dependency_edges(
    tables: dict[str, SymbolTable],
) -> dict[str, set[str]]:
    """``M -> {modules M depends on}`` over in-repo modules only.

    An edge M -> N means M's analysis reads N (M imports from N, or from a
    package whose prefix N provides). External packages are ignored — they never
    change within a scan. Edges are the *dependency* direction; the reverse of
    this graph answers "who is affected when N changes".
    """
    names = set(tables)
    edges: dict[str, set[str]] = {name: set() for name in names}
    for name, table in tables.items():
        for binding in table.imports.values():
            # Every binding is a ModuleImport or SymbolImport; both carry the
            # absolute source module in ``.module``.
            for dep in _in_repo_targets(binding.module, names):
                if dep != name:
                    edges[name].add(dep)
        for star in table.star_imports:
            for dep in _in_repo_targets(star, names):
                if dep != name:
                    edges[name].add(dep)
    return edges


def _in_repo_targets(module: str, names: set[str]) -> list[str]:
    """The in-repo modules an import of ``module`` could bind to: the module
    itself and — because ``from pkg import name`` may resolve through the
    package __init__ to a submodule — its package parents that we have as
    modules. Over-approximates toward more edges (sound)."""
    out: list[str] = []
    if module in names:
        out.append(module)
    # `from pkg import X` reads pkg/__init__; if X is itself a submodule
    # (pkg.X), a change there matters too. Add any known submodule one level
    # down as a candidate dependency and every package prefix we know.
    parts = module.split(".")
    for i in range(1, len(parts)):
        prefix = ".".join(parts[: i + 1])
        if prefix in names and prefix not in out:
            out.append(prefix)
    prefix = module + "."
    for candidate in names:
        if candidate.startswith(prefix) and candidate.count(".") == module.count(".") + 1:
            out.append(candidate)
    return out


def _lazy_reexport_packages(tables: dict[str, SymbolTable]) -> set[str]:
    """Packages whose ``__init__`` re-exports lazily via ``__getattr__`` (PEP
    562 — the ``agents.mcp`` case). Their edges are invisible to static import
    analysis, so a change to any such package invalidates *all* its importers by
    prefix."""
    lazy: set[str] = set()
    for name, table in tables.items():
        if "__getattr__" in table.functions:
            lazy.add(name)
    return lazy


def affected_modules(
    changed: set[str],
    edges: dict[str, set[str]],
    tables: dict[str, SymbolTable],
) -> set[str]:
    """Reverse-reachable closure of ``changed`` over the dependency edges: every
    module that (transitively) depends on a changed module, plus the changed
    modules themselves. Conservative widening for lazy re-exports.

    Deleted modules (in ``changed`` but not in ``edges``) still pull in their
    former importers via the reverse edges built from the *current* graph — an
    importer of a now-deleted module has an unresolved import and is re-analysed.
    """
    reverse: dict[str, set[str]] = {name: set() for name in edges}
    for src, deps in edges.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(src)

    # Lazy-reexport widening: a changed package with __getattr__ affects every
    # module importing anything under its prefix, edge or not.
    lazy = _lazy_reexport_packages(tables)
    changed_lazy = {m for m in changed if m in lazy}

    affected: set[str] = set(changed)
    stack = list(changed)
    while stack:
        node = stack.pop()
        for dependent in reverse.get(node, ()):
            if dependent not in affected:
                affected.add(dependent)
                stack.append(dependent)

    if changed_lazy:
        for pkg in changed_lazy:
            prefix = pkg + "."
            for name, table in tables.items():
                if name in affected:
                    continue
                imports_pkg = any(
                    _binding_module(b) == pkg or _binding_module(b).startswith(prefix)
                    for b in table.imports.values()
                ) or any(s == pkg or s.startswith(prefix) for s in table.star_imports)
                if imports_pkg:
                    affected.add(name)
    return affected


def _binding_module(binding: object) -> str:
    if isinstance(binding, ModuleImport | SymbolImport):
        return binding.module
    return ""
