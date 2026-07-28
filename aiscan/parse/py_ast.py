"""stdlib-``ast`` parser backend: lowers Python source to IR (SPEC §6.2).

Secret-shaped string literals are redacted here, at IR construction, before
anything is cached or persisted (SPEC §6.5): the literal is replaced with
``<REDACTED:kind>`` and a :class:`SecretFinding` is reported via the callback.

Anything not modelled lowers to ``OpaqueS``/``OpaqueE``; syntax errors return
:class:`ParseErrorInfo`. This module never raises on weird code.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Callable

from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    AugAssignS,
    BinOpE,
    BoolE,
    BoolOpE,
    BreakS,
    CallE,
    ClassDefS,
    ClassIR,
    CompareE,
    ContinueS,
    DictE,
    DictEntry,
    Expr,
    ExprS,
    ForS,
    FStrE,
    FuncDefS,
    FuncIR,
    HandlerIR,
    IfS,
    ImportIR,
    Kwarg,
    LambdaE,
    ListE,
    ModuleIR,
    NameE,
    NoneE,
    NumE,
    OpaqueE,
    OpaqueS,
    Param,
    ReturnS,
    Span,
    Stmt,
    StrE,
    SubscriptE,
    TryS,
    TupleE,
    UnaryOpE,
    WhileS,
    WithItem,
    WithS,
)
from aiscan.parse.base import ParseErrorInfo, SecretFinding

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

_BINOPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.BitOr: "|",
    ast.BitAnd: "&",
    ast.BitXor: "^",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.MatMult: "@",
}

_CMPOPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}

_UNARYOPS: dict[type[ast.unaryop], str] = {
    ast.Not: "not",
    ast.USub: "-",
    ast.UAdd: "+",
    ast.Invert: "~",
}


def classify_secret(text: str) -> str | None:
    """Return the secret kind if ``text`` matches a key shape, else None."""
    for kind, pat in _SECRET_PATTERNS:
        if pat.search(text):
            return kind
    return None


class AstParser:
    """MVP :class:`aiscan.parse.base.Parser` backend over stdlib ``ast``."""

    def __init__(self, on_secret: Callable[[SecretFinding], None] | None = None) -> None:
        self._on_secret = on_secret

    def parse(self, path: str, source: str) -> ModuleIR | ParseErrorInfo:
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            return ParseErrorInfo(path=path, message=exc.msg or "syntax error", line=exc.lineno)
        except (ValueError, RecursionError) as exc:  # e.g. NULs, pathological nesting
            return ParseErrorInfo(path=path, message=str(exc), line=None)
        lowering = _Lowering(path, self._on_secret)
        return lowering.lower_module(tree, source)


class _Lowering:
    def __init__(self, path: str, on_secret: Callable[[SecretFinding], None] | None) -> None:
        self.path = path
        self.on_secret = on_secret
        self.imports: list[ImportIR] = []

    # -- helpers ------------------------------------------------------------

    def span(self, node: ast.AST) -> Span:
        line = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", None) or line
        return Span(
            file=self.path,
            line_start=line,
            line_end=end,
            col_start=getattr(node, "col_offset", 0),
            col_end=getattr(node, "end_col_offset", None) or 0,
        )

    # -- module -------------------------------------------------------------

    def lower_module(self, tree: ast.Module, source: str) -> ModuleIR:
        body = self.stmts(tree.body)
        defs = tuple(s.func for s in body if isinstance(s, FuncDefS))
        classes = tuple(s.cls for s in body if isinstance(s, ClassDefS))
        assigns = tuple(s for s in body if isinstance(s, AssignS))
        return ModuleIR(
            path=self.path,
            content_hash=hashlib.sha256(source.encode("utf-8", "replace")).hexdigest(),
            loc=len(source.splitlines()),
            imports=tuple(self.imports),
            defs=defs,
            classes=classes,
            assigns=assigns,
            body=tuple(body),
        )

    # -- statements ---------------------------------------------------------

    def stmts(self, nodes: list[ast.stmt]) -> list[Stmt]:
        out: list[Stmt] = []
        for n in nodes:
            out.extend(self.stmt(n))
        return out

    def stmt(self, node: ast.stmt) -> list[Stmt]:
        sp = self.span(node)
        match node:
            case ast.Assign(targets=targets, value=value):
                return [
                    AssignS(
                        span=sp,
                        targets=tuple(self.expr(t) for t in targets),
                        value=self.expr(value),
                    )
                ]
            case ast.AnnAssign(target=target, value=value) if value is not None:
                return [AssignS(span=sp, targets=(self.expr(target),), value=self.expr(value))]
            case ast.AnnAssign():
                return []
            case ast.AugAssign(target=target, op=op, value=value):
                return [
                    AugAssignS(
                        span=sp,
                        target=self.expr(target),
                        op=_BINOPS.get(type(op), "?"),
                        value=self.expr(value),
                    )
                ]
            case ast.Expr(value=value):
                return [ExprS(span=sp, expr=self.expr(value))]
            case ast.Return(value=value):
                return [ReturnS(span=sp, value=self.expr(value) if value is not None else None)]
            case ast.If(test=test, body=body, orelse=orelse):
                return [
                    IfS(
                        span=sp,
                        test=self.expr(test),
                        body=tuple(self.stmts(body)),
                        orelse=tuple(self.stmts(orelse)),
                    )
                ]
            case ast.While(test=test, body=body, orelse=orelse):
                loop = WhileS(span=sp, test=self.expr(test), body=tuple(self.stmts(body)))
                return [loop, *self.stmts(orelse)]
            case ast.For(target=target, iter=it, body=body, orelse=orelse) | ast.AsyncFor(
                target=target, iter=it, body=body, orelse=orelse
            ):
                loop_f = ForS(
                    span=sp,
                    target=self.expr(target),
                    iter=self.expr(it),
                    body=tuple(self.stmts(body)),
                )
                return [loop_f, *self.stmts(orelse)]
            case ast.With(items=items, body=body) | ast.AsyncWith(items=items, body=body):
                return [
                    WithS(
                        span=sp,
                        items=tuple(
                            WithItem(
                                context=self.expr(i.context_expr),
                                asname=self.expr(i.optional_vars)
                                if i.optional_vars is not None
                                else None,
                            )
                            for i in items
                        ),
                        body=tuple(self.stmts(body)),
                    )
                ]
            case ast.Try(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody):
                return [
                    TryS(
                        span=sp,
                        body=tuple(self.stmts(body) + self.stmts(orelse)),
                        handlers=tuple(
                            HandlerIR(
                                type_name=self._handler_name(h.type),
                                body=tuple(self.stmts(h.body)),
                            )
                            for h in handlers
                        ),
                        final=tuple(self.stmts(finalbody)),
                    )
                ]
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                return [FuncDefS(span=sp, func=self.func(node))]
            case ast.ClassDef():
                return [ClassDefS(span=sp, cls=self.cls(node))]
            case ast.Import(names=names):
                for alias in names:
                    self.imports.append(
                        ImportIR(
                            kind="import",
                            module=alias.name,
                            names=((alias.name, alias.asname),),
                            level=0,
                            star=False,
                            span=sp,
                        )
                    )
                return [OpaqueS(span=sp)]
            case ast.ImportFrom(module=module, names=names, level=level):
                self.imports.append(
                    ImportIR(
                        kind="from",
                        module=module or "",
                        names=tuple((a.name, a.asname) for a in names),
                        level=level,
                        star=any(a.name == "*" for a in names),
                        span=sp,
                    )
                )
                return [OpaqueS(span=sp)]
            case ast.Match(subject=_, cases=cases):
                return [self._match_to_if_chain(sp, cases)]
            case ast.Break():
                return [BreakS(span=sp)]
            case ast.Continue():
                return [ContinueS(span=sp)]
            case ast.Pass():
                return []
            case _:
                return [OpaqueS(span=sp)]

    def _match_to_if_chain(self, sp: Span, cases: list[ast.match_case]) -> Stmt:
        """Lower ``match`` to an IfS chain with opaque tests (SPEC §6.2)."""
        chain: Stmt = OpaqueS(span=sp)
        for case in reversed(cases):
            chain = IfS(
                span=self.span(case.pattern),
                test=OpaqueE(span=self.span(case.pattern)),
                body=tuple(self.stmts(case.body)),
                orelse=(chain,) if not isinstance(chain, OpaqueS) else (),
            )
        return chain

    def _handler_name(self, type_expr: ast.expr | None) -> str | None:
        if type_expr is None:
            return None
        parts: list[str] = []
        node: ast.expr = type_expr
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    # -- definitions --------------------------------------------------------

    def func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> FuncIR:
        a = node.args
        params: list[Param] = []
        positional = list(a.posonlyargs) + list(a.args)
        defaults: list[ast.expr | None] = [None] * (len(positional) - len(a.defaults)) + list(
            a.defaults
        )
        for arg, dflt in zip(positional, defaults, strict=True):
            params.append(
                Param(
                    name=arg.arg,
                    kind="pos",
                    default=self.expr(dflt) if dflt is not None else None,
                )
            )
        if a.vararg is not None:
            params.append(Param(name=a.vararg.arg, kind="vararg"))
        for arg, kdflt in zip(a.kwonlyargs, a.kw_defaults, strict=True):
            params.append(
                Param(
                    name=arg.arg,
                    kind="kwonly",
                    default=self.expr(kdflt) if kdflt is not None else None,
                )
            )
        if a.kwarg is not None:
            params.append(Param(name=a.kwarg.arg, kind="kwarg"))
        return FuncIR(
            name=node.name,
            params=tuple(params),
            decorators=tuple(self.expr(d) for d in node.decorator_list),
            body=tuple(self.stmts(node.body)),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            span=self.span(node),
        )

    def cls(self, node: ast.ClassDef) -> ClassIR:
        body_assigns: list[AssignS] = []
        methods: list[FuncIR] = []
        for item in node.body:
            match item:
                case ast.Assign() | ast.AnnAssign():
                    lowered = self.stmt(item)
                    body_assigns.extend(s for s in lowered if isinstance(s, AssignS))
                case ast.FunctionDef() | ast.AsyncFunctionDef():
                    methods.append(self.func(item))
                case _:
                    pass
        return ClassIR(
            name=node.name,
            bases=tuple(self.expr(b) for b in node.bases),
            decorators=tuple(self.expr(d) for d in node.decorator_list),
            body_assigns=tuple(body_assigns),
            methods=tuple(methods),
            span=self.span(node),
        )

    # -- expressions --------------------------------------------------------

    def expr(self, node: ast.expr) -> Expr:
        sp = self.span(node)
        match node:
            case ast.Name(id=name):
                return NameE(span=sp, name=name)
            case ast.Attribute(value=value, attr=attr):
                return AttrE(span=sp, base=self.expr(value), attr=attr)
            case ast.Call(func=func, args=args, keywords=keywords):
                return CallE(
                    span=sp,
                    callee=self.expr(func),
                    args=tuple(
                        OpaqueE(span=self.span(x)) if isinstance(x, ast.Starred) else self.expr(x)
                        for x in args
                    ),
                    kwargs=tuple(Kwarg(name=k.arg, value=self.expr(k.value)) for k in keywords),
                )
            case ast.Subscript(value=value, slice=index):
                if isinstance(index, ast.Slice):
                    return SubscriptE(span=sp, base=self.expr(value), index=OpaqueE(span=sp))
                return SubscriptE(span=sp, base=self.expr(value), index=self.expr(index))
            case ast.Constant(value=v):
                if isinstance(v, bool):
                    return BoolE(span=sp, value=v)
                if isinstance(v, str):
                    return self._str_literal(sp, v)
                if isinstance(v, int | float):
                    return NumE(span=sp, value=v)
                if v is None:
                    return NoneE(span=sp)
                return OpaqueE(span=sp)
            case ast.JoinedStr(values=joined):
                parts: list[str | Expr] = []
                for piece in joined:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        parts.append(piece.value)
                    elif isinstance(piece, ast.FormattedValue):
                        parts.append(self.expr(piece.value))
                    else:
                        parts.append(OpaqueE(span=self.span(piece)))
                return FStrE(span=sp, parts=tuple(parts))
            case ast.List(elts=elts):
                return ListE(span=sp, elems=tuple(self.expr(e) for e in elts))
            case ast.Tuple(elts=elts):
                return TupleE(span=sp, elems=tuple(self.expr(e) for e in elts))
            case ast.Dict(keys=keys, values=values):
                return DictE(
                    span=sp,
                    entries=tuple(
                        DictEntry(
                            key=self.expr(k) if k is not None else None,
                            value=self.expr(v),
                        )
                        for k, v in zip(keys, values, strict=True)
                    ),
                )
            case ast.BinOp(left=left, op=op, right=right):
                return BinOpE(
                    span=sp,
                    op=_BINOPS.get(type(op), "?"),
                    left=self.expr(left),
                    right=self.expr(right),
                )
            case ast.BoolOp(op=op, values=values):
                return BoolOpE(
                    span=sp,
                    op="and" if isinstance(op, ast.And) else "or",
                    operands=tuple(self.expr(v) for v in values),
                )
            case ast.UnaryOp(op=op, operand=operand):
                return UnaryOpE(
                    span=sp, op=_UNARYOPS.get(type(op), "?"), operand=self.expr(operand)
                )
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                return CompareE(
                    span=sp,
                    left=self.expr(left),
                    ops=tuple(_CMPOPS.get(type(o), "?") for o in ops),
                    comparators=tuple(self.expr(c) for c in comparators),
                )
            case ast.Lambda(args=args, body=body):
                return LambdaE(
                    span=sp,
                    params=tuple(a.arg for a in list(args.posonlyargs) + list(args.args)),
                    body=self.expr(body),
                )
            case ast.Await(value=value):
                return self.expr(value)
            case ast.NamedExpr(value=value):
                return self.expr(value)
            case _:
                return OpaqueE(span=sp)

    def _str_literal(self, sp: Span, value: str) -> StrE:
        kind = classify_secret(value)
        if kind is None:
            return StrE(span=sp, value=value)
        if self.on_secret is not None:
            self.on_secret(
                SecretFinding(path=self.path, evidence=str(sp), secret_kind=kind)
            )
        return StrE(span=sp, value=f"<REDACTED:{kind}>", redacted=True)
