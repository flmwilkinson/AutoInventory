"""Per-module symbol tables (SPEC §6.3).

A symbol table maps a module's top-level names to what they are: an import
binding, a function/class definition, or the ordered right-hand sides of
top-level assignments (the resolver unions reaching assignments).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aiscan.ir.nodes import AssignS, ClassIR, Expr, FuncIR, ModuleIR, NameE

RelativeResolver = Callable[[int, str], str]
"""``(level, module) -> absolute module name`` for relative imports."""


@dataclass(frozen=True, slots=True)
class ModuleImport:
    """``import m`` / ``import m as a`` — the alias binds a module."""

    module: str


@dataclass(frozen=True, slots=True)
class SymbolImport:
    """``from m import n [as a]`` — the alias binds symbol ``n`` of module ``m``.

    ``module`` is absolute (relative imports are resolved at table build time).
    """

    module: str
    name: str


ImportBinding = ModuleImport | SymbolImport


@dataclass(slots=True)
class SymbolTable:
    """Top-level namespace of one module."""

    module: str
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    star_imports: tuple[str, ...] = ()
    functions: dict[str, FuncIR] = field(default_factory=dict)
    classes: dict[str, ClassIR] = field(default_factory=dict)
    assignments: dict[str, tuple[Expr, ...]] = field(default_factory=dict)


def build_symbol_table(
    name: str,
    mod: ModuleIR,
    resolve_relative: RelativeResolver,
) -> SymbolTable:
    """Build the symbol table for module ``name``.

    ``resolve_relative(level, module)`` converts a relative import in this
    module to an absolute module name.
    """
    table = SymbolTable(module=name)
    stars: list[str] = []
    for imp in mod.imports:
        if imp.kind == "import":
            for mod_name, asname in imp.names:
                if asname is not None:
                    table.imports[asname] = ModuleImport(module=mod_name)
                else:
                    # ``import a.b`` binds ``a`` in the namespace.
                    top = mod_name.split(".")[0]
                    table.imports[top] = ModuleImport(module=top)
        else:
            base = resolve_relative(imp.level, imp.module) if imp.level else imp.module
            if imp.star:
                stars.append(base)
                continue
            for sym, asname in imp.names:
                table.imports[asname or sym] = SymbolImport(module=base, name=sym)
    table.star_imports = tuple(stars)
    for fn in mod.defs:
        table.functions[fn.name] = fn
    for cls in mod.classes:
        table.classes[cls.name] = cls
    for a in mod.assigns:
        _record_assign(table, a)
    return table


def _record_assign(table: SymbolTable, a: AssignS) -> None:
    for target in a.targets:
        if isinstance(target, NameE):
            prev = table.assignments.get(target.name, ())
            table.assignments[target.name] = (*prev, a.value)
