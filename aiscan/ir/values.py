"""Abstract value domain (SPEC §5.1).

The resolver returns frozen sets of these values. ``Symbolic`` and ``Template``
are first-class results — the scanner records the symbol or template shape,
never a guess. ``Top`` never silently enters a record; it surfaces as
``{"unresolved": reason}`` via :func:`to_repr`.

All models are frozen (hashable) so they can live in ``frozenset[Value]``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TopReason = Literal["depth", "unbound", "dynamic", "timeout", "star_import", "opaque"]
SymbolicKind = Literal["env", "config", "cli", "wrapper_default", "external"]


class _V(BaseModel):
    """Base for all abstract values: frozen, hashable."""

    model_config = ConfigDict(frozen=True)


class Str(_V):
    t: Literal["str"] = "str"
    s: str


class Num(_V):
    t: Literal["num"] = "num"
    v: int | float


class Bool(_V):
    t: Literal["bool"] = "bool"
    v: bool


class NoneV(_V):
    t: Literal["none"] = "none"


class Hole(_V):
    """An unresolved slot inside a Template; ``expr`` is source text of the hole."""

    t: Literal["hole"] = "hole"
    expr: str


class Template(_V):
    """Partly-literal string (f-string / concat with unresolved holes)."""

    t: Literal["template"] = "template"
    parts: tuple[str | Hole, ...]

    def render(self) -> str:
        """Render with ``{expr}`` markers in hole positions."""
        out: list[str] = []
        for p in self.parts:
            out.append(p if isinstance(p, str) else "{" + p.expr + "}")
        return "".join(out)

    def literal_prefix(self) -> str:
        """Leading literal text before the first hole (used for host matching)."""
        first = self.parts[0] if self.parts else ""
        return first if isinstance(first, str) else ""


class Symbolic(_V):
    """A value that only exists at runtime, recorded by reference."""

    t: Literal["symbolic"] = "symbolic"
    kind: SymbolicKind
    key: str


class BoundArgs(_V):
    """Constructor/call arguments captured at a call site (values are reprs of
    resolved sets, kept JSON-simple to stay hashable)."""

    t: Literal["bound_args"] = "bound_args"
    positional: tuple[ValueSet, ...] = ()
    named: tuple[tuple[str, ValueSet], ...] = ()

    def get(self, name: str, position: int | None = None) -> ValueSet | None:
        for k, vs in self.named:
            if k == name:
                return vs
        if position is not None and position < len(self.positional):
            return self.positional[position]
        return None


class ClassInstance(_V):
    t: Literal["instance"] = "instance"
    class_fq: str
    ctor_args: BoundArgs = Field(default_factory=lambda: BoundArgs())


class FuncRef(_V):
    t: Literal["func_ref"] = "func_ref"
    fq: str
    def_site: str  # "file:line_start-line_end"


class ClassRef(_V):
    t: Literal["class_ref"] = "class_ref"
    fq: str
    def_site: str


class ModuleRef(_V):
    t: Literal["module_ref"] = "module_ref"
    name: str


class PackageRef(_V):
    """Third-party import target (source not in the analysis set)."""

    t: Literal["package_ref"] = "package_ref"
    name: str
    version: str | None = None


class ListVal(_V):
    t: Literal["list"] = "list"
    elems: tuple[ValueSet, ...] = ()
    open: bool = False  # True when appends/extends were not fully tracked


class DictVal(_V):
    t: Literal["dict"] = "dict"
    entries: tuple[tuple[str, ValueSet], ...] = ()
    open: bool = False  # True for ** spreads / computed keys

    def get(self, key: str) -> ValueSet | None:
        for k, vs in self.entries:
            if k == key:
                return vs
        return None

    def keys(self) -> tuple[str, ...]:
        return tuple(k for k, _ in self.entries)


class Top(_V):
    """Unknown, with the reason. Never enters a record silently."""

    t: Literal["top"] = "top"
    reason: TopReason


Value = (
    Str
    | Num
    | Bool
    | NoneV
    | Hole
    | Template
    | Symbolic
    | BoundArgs
    | ClassInstance
    | FuncRef
    | ClassRef
    | ModuleRef
    | PackageRef
    | ListVal
    | DictVal
    | Top
)

ValueSet = frozenset[Value]

BoundArgs.model_rebuild()
ClassInstance.model_rebuild()
ListVal.model_rebuild()
DictVal.model_rebuild()


def single(v: Value) -> ValueSet:
    """Convenience: a one-element value set."""
    return frozenset((v,))


TOP_KINDS = (Top,)

JsonRepr = str | int | float | bool | None | dict[str, object] | list[object]


def _repr_one(v: Value) -> JsonRepr:
    match v:
        case Str(s=s):
            return s
        case Num(v=n):
            return n
        case Bool(v=b):
            return b
        case NoneV():
            return None
        case Template() as tp:
            return {"template": tp.render(), "dynamic": True}
        case Symbolic(kind=kind, key=key):
            return {"symbolic": f"{kind}:{key}"}
        case Hole(expr=e):
            return {"template": "{" + e + "}", "dynamic": True}
        case FuncRef(fq=fq) | ClassRef(fq=fq):
            return {"ref": fq}
        case ClassInstance(class_fq=fq):
            return {"instance": fq}
        case ModuleRef(name=name):
            return {"module": name}
        case PackageRef(name=name, version=ver):
            return {"package": name, "version": ver}
        case ListVal() as lv:
            return {"list": [to_repr(e) for e in lv.elems], "open": lv.open}
        case DictVal() as dv:
            return {
                "dict": {k: to_repr(vs) for k, vs in sorted(dv.entries)},
                "open": dv.open,
            }
        case Top(reason=reason):
            return {"unresolved": reason}
        case BoundArgs():
            return {"unresolved": "opaque"}


def to_repr(values: ValueSet) -> JsonRepr:
    """JSON-serialisable projection of a resolved value set (SPEC §5.2 ValueRepr).

    A single value projects directly; multiple values project as a
    deterministically-ordered ``{"union": [...]}``; the empty set (which the
    resolver never returns, but defensively) projects as unresolved.
    """
    if not values:
        return {"unresolved": "unbound"}
    if len(values) == 1:
        return _repr_one(next(iter(values)))
    reprs = sorted((_repr_one(v) for v in values), key=lambda r: repr(r))
    return {"union": list(reprs)}
