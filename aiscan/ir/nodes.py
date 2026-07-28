"""IR node definitions (SPEC §6.2).

The IR is the abstraction boundary between parsing and everything downstream:
no module outside ``aiscan.parse`` may import :mod:`ast`. Nodes are frozen
dataclasses; sequences are tuples so modules are immutable after lowering.

Anything the lowering does not model becomes :class:`OpaqueS` / :class:`OpaqueE`
— the scanner never crashes on weird code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Span:
    """Source location ``file:line_start-line_end`` (file is repo-relative, posix).

    Column offsets disambiguate multiple expressions on one line (resolver memo
    keys); the evidence string stays line-based.
    """

    file: str
    line_start: int
    line_end: int
    col_start: int = 0
    col_end: int = 0

    def __str__(self) -> str:
        return f"{self.file}:{self.line_start}-{self.line_end}"

    def contains(self, other: Span) -> bool:
        if self.file != other.file:
            return False
        start_ok = (self.line_start, self.col_start) <= (other.line_start, other.col_start)
        end_ok = (self.line_end, self.col_end) >= (other.line_end, other.col_end)
        return start_ok and end_ok


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expr:
    """Base class for IR expressions."""

    span: Span


@dataclass(frozen=True, slots=True)
class NameE(Expr):
    name: str


@dataclass(frozen=True, slots=True)
class AttrE(Expr):
    base: Expr
    attr: str


@dataclass(frozen=True, slots=True)
class Kwarg:
    """Keyword argument; ``name is None`` means ``**spread``."""

    name: str | None
    value: Expr


@dataclass(frozen=True, slots=True)
class CallE(Expr):
    callee: Expr
    args: tuple[Expr, ...]
    kwargs: tuple[Kwarg, ...]


@dataclass(frozen=True, slots=True)
class SubscriptE(Expr):
    base: Expr
    index: Expr


@dataclass(frozen=True, slots=True)
class StrE(Expr):
    value: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class FStrE(Expr):
    """f-string: literal fragments interleaved with hole expressions."""

    parts: tuple[str | Expr, ...]


@dataclass(frozen=True, slots=True)
class NumE(Expr):
    value: int | float


@dataclass(frozen=True, slots=True)
class BoolE(Expr):
    value: bool


@dataclass(frozen=True, slots=True)
class NoneE(Expr):
    pass


@dataclass(frozen=True, slots=True)
class ListE(Expr):
    elems: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class TupleE(Expr):
    elems: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class DictEntry:
    """Dict entry; ``key is None`` means ``**spread``."""

    key: Expr | None
    value: Expr


@dataclass(frozen=True, slots=True)
class DictE(Expr):
    entries: tuple[DictEntry, ...]


@dataclass(frozen=True, slots=True)
class BinOpE(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class BoolOpE(Expr):
    """``and`` / ``or`` chains — kept (not opaque) so loop guards stay analysable."""

    op: str
    operands: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class UnaryOpE(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True, slots=True)
class CompareE(Expr):
    left: Expr
    ops: tuple[str, ...]
    comparators: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class LambdaE(Expr):
    params: tuple[str, ...]
    body: Expr


@dataclass(frozen=True, slots=True)
class OpaqueE(Expr):
    """Anything the lowering does not model."""


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stmt:
    """Base class for IR statements."""

    span: Span


@dataclass(frozen=True, slots=True)
class AssignS(Stmt):
    targets: tuple[Expr, ...]
    value: Expr


@dataclass(frozen=True, slots=True)
class AugAssignS(Stmt):
    target: Expr
    op: str
    value: Expr


@dataclass(frozen=True, slots=True)
class ExprS(Stmt):
    expr: Expr


@dataclass(frozen=True, slots=True)
class ReturnS(Stmt):
    value: Expr | None


@dataclass(frozen=True, slots=True)
class IfS(Stmt):
    test: Expr
    body: tuple[Stmt, ...]
    orelse: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class WhileS(Stmt):
    test: Expr
    body: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class ForS(Stmt):
    target: Expr
    iter: Expr
    body: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class WithItem:
    context: Expr
    asname: Expr | None


@dataclass(frozen=True, slots=True)
class WithS(Stmt):
    items: tuple[WithItem, ...]
    body: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class HandlerIR:
    """``except`` clause: exception type name (best-effort) plus body."""

    type_name: str | None
    body: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class TryS(Stmt):
    body: tuple[Stmt, ...]
    handlers: tuple[HandlerIR, ...]
    final: tuple[Stmt, ...]


@dataclass(frozen=True, slots=True)
class BreakS(Stmt):
    """``break`` — loop-termination guards need it visible (SPEC-7 F2)."""


@dataclass(frozen=True, slots=True)
class ContinueS(Stmt):
    """``continue`` — loop-iteration guards need it visible (SPEC-7 F2)."""


@dataclass(frozen=True, slots=True)
class OpaqueS(Stmt):
    """Anything the lowering does not model (raise, assert, del, …)."""


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    kind: str  # "pos" | "vararg" | "kwonly" | "kwarg"
    default: Expr | None = None


@dataclass(frozen=True, slots=True)
class FuncIR:
    name: str
    params: tuple[Param, ...]
    decorators: tuple[Expr, ...]
    body: tuple[Stmt, ...]
    is_async: bool
    span: Span


@dataclass(frozen=True, slots=True)
class ClassIR:
    name: str
    bases: tuple[Expr, ...]
    decorators: tuple[Expr, ...]
    body_assigns: tuple[AssignS, ...]
    methods: tuple[FuncIR, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class FuncDefS(Stmt):
    """A (possibly nested) function definition kept in statement position so
    enclosing-region analysis (wrappers, agent shapes) sees nesting."""

    func: FuncIR


@dataclass(frozen=True, slots=True)
class ClassDefS(Stmt):
    cls: ClassIR


@dataclass(frozen=True, slots=True)
class ImportIR:
    """``import m [as a]`` (kind="import", one entry in names per module) or
    ``from m import n [as a]`` (kind="from")."""

    kind: str  # "import" | "from"
    module: str
    names: tuple[tuple[str, str | None], ...]
    level: int
    star: bool
    span: Span


@dataclass(frozen=True, slots=True)
class ModuleIR:
    """Lowered module. ``body`` is the full statement list (defs appear as
    FuncDefS/ClassDefS); ``imports``/``defs``/``classes``/``assigns`` are
    top-level convenience views over it.

    ``exports`` (SPEC-4): explicit export map ``(public_name, local_name)`` for
    ES modules — drives barrel re-export canonicalisation. Python modules leave
    it empty (their exports are the symbol table)."""

    path: str  # repo-relative, posix separators
    content_hash: str
    loc: int
    imports: tuple[ImportIR, ...]
    defs: tuple[FuncIR, ...]
    classes: tuple[ClassIR, ...]
    assigns: tuple[AssignS, ...]
    body: tuple[Stmt, ...] = field(default=())
    exports: tuple[tuple[str, str], ...] = field(default=())
