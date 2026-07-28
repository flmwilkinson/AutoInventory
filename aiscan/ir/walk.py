"""Generic IR tree walking helpers (used by resolver and frontends)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    AugAssignS,
    BinOpE,
    BoolOpE,
    CallE,
    ClassDefS,
    ClassIR,
    CompareE,
    DictE,
    Expr,
    ExprS,
    ForS,
    FStrE,
    FuncDefS,
    FuncIR,
    IfS,
    LambdaE,
    ListE,
    ModuleIR,
    NameE,
    ReturnS,
    Span,
    Stmt,
    SubscriptE,
    TryS,
    TupleE,
    UnaryOpE,
    WhileS,
    WithS,
)


def stmt_child_stmts(stmt: Stmt) -> tuple[Stmt, ...]:
    """Direct child statements (not descending into nested defs)."""
    match stmt:
        case IfS(body=b, orelse=o):
            return (*b, *o)
        case WhileS(body=b):
            return b
        case ForS(body=b):
            return b
        case WithS(body=b):
            return b
        case TryS(body=b, handlers=handlers, final=f):
            return (*b, *(s for h in handlers for s in h.body), *f)
        case _:
            return ()


def stmt_exprs(stmt: Stmt) -> tuple[Expr, ...]:
    """Direct expressions of one statement (tests, values, targets)."""
    match stmt:
        case AssignS(targets=t, value=v):
            return (*t, v)
        case AugAssignS(target=t, value=v):
            return (t, v)
        case ExprS(expr=e):
            return (e,)
        case ReturnS(value=v):
            return (v,) if v is not None else ()
        case IfS(test=t):
            return (t,)
        case WhileS(test=t):
            return (t,)
        case ForS(target=t, iter=i):
            return (t, i)
        case WithS(items=items):
            return tuple(i.context for i in items)
        case _:
            return ()


def expr_children(expr: Expr) -> tuple[Expr, ...]:
    match expr:
        case AttrE(base=b):
            return (b,)
        case CallE(callee=c, args=a, kwargs=k):
            return (c, *a, *(kw.value for kw in k))
        case SubscriptE(base=b, index=i):
            return (b, i)
        case FStrE(parts=parts):
            return tuple(p for p in parts if not isinstance(p, str))
        case ListE(elems=e) | TupleE(elems=e):
            return e
        case DictE(entries=entries):
            keys = tuple(en.key for en in entries if en.key is not None)
            return (*keys, *(en.value for en in entries))
        case BinOpE(left=left, right=right):
            return (left, right)
        case BoolOpE(operands=ops):
            return ops
        case UnaryOpE(operand=op):
            return (op,)
        case CompareE(left=left, comparators=c):
            return (left, *c)
        case LambdaE(body=b):
            return (b,)
        case _:
            return ()


def iter_stmts(stmts: tuple[Stmt, ...] | list[Stmt], into_defs: bool = False) -> Iterator[Stmt]:
    """All statements, depth-first. ``into_defs`` also descends into nested
    function/class definitions."""
    stack: list[Stmt] = list(reversed(list(stmts)))
    while stack:
        stmt = stack.pop()
        yield stmt
        children = list(stmt_child_stmts(stmt))
        if into_defs:
            if isinstance(stmt, FuncDefS):
                children.extend(stmt.func.body)
            elif isinstance(stmt, ClassDefS):
                for m in stmt.cls.methods:
                    children.extend(m.body)
        stack.extend(reversed(children))


def iter_exprs(stmts: tuple[Stmt, ...] | list[Stmt], into_defs: bool = False) -> Iterator[Expr]:
    """All expressions in the statement tree, depth-first."""
    for stmt in iter_stmts(stmts, into_defs=into_defs):
        stack: list[Expr] = list(reversed(stmt_exprs(stmt)))
        while stack:
            e = stack.pop()
            yield e
            stack.extend(reversed(expr_children(e)))


def iter_calls(stmts: tuple[Stmt, ...] | list[Stmt], into_defs: bool = False) -> Iterator[CallE]:
    for e in iter_exprs(stmts, into_defs=into_defs):
        if isinstance(e, CallE):
            yield e


def enclosing_region(
    mod: ModuleIR, span: Span
) -> tuple[tuple[FuncIR, ...], ClassIR | None]:
    """The nested function stack containing ``span`` (outermost first) and,
    when the outermost function is a method, its owning class."""
    funcs: list[FuncIR] = []
    owner: ClassIR | None = None

    def descend(stmts: Sequence[Stmt], current_class: ClassIR | None) -> None:
        nonlocal owner
        for s in stmts:
            if not s.span.contains(span):
                continue
            if isinstance(s, FuncDefS):
                if not funcs and current_class is not None:
                    owner = current_class
                funcs.append(s.func)
                descend(list(s.func.body), None)
                return
            if isinstance(s, ClassDefS):
                for m in s.cls.methods:
                    if m.span.contains(span):
                        if not funcs:
                            owner = s.cls
                        funcs.append(m)
                        descend(list(m.body), None)
                        return
                return
            descend(list(stmt_child_stmts(s)), current_class)
            return

    descend(list(mod.body), None)
    return tuple(funcs), owner


def dotted_name(expr: Expr) -> str:
    """Best-effort dotted rendering of a name/attr chain ('' if not a chain)."""
    parts: list[str] = []
    node = expr
    while isinstance(node, AttrE):
        parts.append(node.attr)
        node = node.base
    if isinstance(node, NameE):
        parts.append(node.name)
        return ".".join(reversed(parts))
    return ""


def approx_source(expr: Expr) -> str:
    """Short human-readable rendering for template holes / diagnostics."""
    match expr:
        case NameE(name=n):
            return n
        case AttrE():
            return dotted_name(expr) or "<attr>"
        case CallE(callee=c):
            inner = dotted_name(c)
            return f"{inner}()" if inner else "<call>()"
        case SubscriptE(base=b):
            inner = dotted_name(b)
            return f"{inner}[...]" if inner else "<subscript>"
        case _:
            return "<expr>"
