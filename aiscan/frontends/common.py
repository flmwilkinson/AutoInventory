"""Shared entity builders and IR/value helpers used by both frontends."""

from __future__ import annotations

from aiscan.context import OrgPack
from aiscan.facts.models import SignatureF, ToolDefF
from aiscan.ir.nodes import AssignS, AttrE, CallE, Expr, FuncIR, NameE, Span, SubscriptE
from aiscan.ir.values import ClassInstance, ClassRef, FuncRef, ModuleRef, PackageRef
from aiscan.ir.walk import expr_children, iter_stmts
from aiscan.resolve.engine import Resolver
from aiscan.sinks.side_effects import classify_body


def subtree(expr: Expr) -> list[Expr]:
    """Every expression node in ``expr``'s subtree (pre-order)."""
    out: list[Expr] = []
    stack = [expr]
    while stack:
        e = stack.pop()
        out.append(e)
        stack.extend(expr_children(e))
    return out


def root_name(expr: Expr) -> str | None:
    """The base name of an attribute/subscript/call chain (``a.b.c()`` -> "a")."""
    node = expr
    while True:
        match node:
            case AttrE(base=b) | SubscriptE(base=b):
                node = b
            case CallE(callee=c):
                node = c
            case NameE(name=n):
                return n
            case _:
                return None


def derived_names(fn: FuncIR, sink_calls: list[CallE]) -> set[str]:
    """Local names in ``fn`` whose value derives from a sink call (directly, or
    transitively through another derived name) — the response-flow closure."""
    derived: set[str] = set()
    for stmt in iter_stmts(fn.body):
        if not isinstance(stmt, AssignS):
            continue
        contains = any(e is c for e in subtree(stmt.value) for c in sink_calls)
        root = root_name(stmt.value)
        if contains or (root is not None and root in derived):
            for t in stmt.targets:
                if isinstance(t, NameE):
                    derived.add(t.name)
    return derived


def value_fqs(v: object, path: tuple[str, ...]) -> set[str]:
    """The fully-qualified identity(ies) a resolved value denotes, with an
    attribute ``path`` suffix — the value->identity mapping both frontends match
    call sites against."""
    suffix = ("." + ".".join(path)) if path else ""
    match v:
        case PackageRef(name=n) | ModuleRef(name=n):
            return {n + suffix}
        case ClassRef(fq=fq) | FuncRef(fq=fq):
            return {fq + suffix}
        case ClassInstance(class_fq=fq):
            return {fq + suffix}
        case _:
            return set()


def build_tool_from_fq(
    resolver: Resolver,
    org_pack: OrgPack,
    llm_sink_spans: frozenset[Span],
    fq: str,
    method: str,
    source_path: str,
) -> ToolDefF | None:
    """ToolDef(kind=function) from a resolved function fq: signature from the
    def, side effects from its body (SPEC §6.5)."""
    entry = resolver.lookup_def(fq)
    if entry is None:
        return None
    fn_module, fn = entry
    report = classify_body(fn, fn_module, resolver, org_pack, llm_sink_spans)
    params = tuple(
        p.name for p in fn.params if p.kind in ("pos", "kwonly") and p.name not in ("self", "cls")
    )
    return ToolDefF(
        id=f"tool:{fq}",
        evidence=(str(fn.span),),
        confidence="high",
        method=method,
        source_files=(source_path,),
        name=fn.name,
        kind="function",
        signature=SignatureF(params=params),
        side_effects=report.effects,
        external_target=report.external_target,
        credential_ref=report.credential_ref,
        auth_verb=report.http_verb,
    )
