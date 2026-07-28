"""TypeScript/JavaScript parser backend (SPEC-4 §2) over tree-sitter.

Lowers TS/TSX/JS to the same IR as :mod:`aiscan.parse.py_ast` — the IR is the
abstraction boundary, so everything downstream (resolver, sinks, F1/F2) is
untouched. Per SPEC-4 §2.1: types/generics/JSX are stripped or ``Opaque``;
``await`` unwraps; destructuring lowers to member/subscript assigns (F2's
dispatch detection depends on it); ternary and ``??``/``||`` lower to
``BoolOpE`` so the resolver unions both branches; everything unmapped becomes
``Opaque`` — never a crash. tree-sitter recovers from syntax errors, so a file
with ERROR nodes still lowers (the broken subtrees turn Opaque); only an
undecodable/empty parse yields :class:`ParseErrorInfo`.

Secret redaction (SPEC-1 §6.5) applies at lowering time, same patterns as the
Python backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from tree_sitter import Language, Node
from tree_sitter import Parser as TSParser

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
    UnaryOpE,
    WhileS,
)
from aiscan.parse.base import ParseErrorInfo, SecretFinding
from aiscan.parse.py_ast import classify_secret

_CMP_OPS = {"==": "==", "===": "==", "!=": "!=", "!==": "!=", "<": "<", ">": ">",
            "<=": "<=", ">=": ">=", "in": "in", "instanceof": "isinstance"}
_BOOL_OPS = {"&&": "and", "||": "or", "??": "or"}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`", "0": "\0"}

_languages: dict[str, Language] = {}


def _language(ext: str) -> Language:
    key = "tsx" if ext in (".tsx", ".jsx") else ("ts" if ext == ".ts" else "js")
    if key not in _languages:
        if key == "tsx":
            import tree_sitter_typescript

            _languages[key] = Language(tree_sitter_typescript.language_tsx())
        elif key == "ts":
            import tree_sitter_typescript

            _languages[key] = Language(tree_sitter_typescript.language_typescript())
        else:
            import tree_sitter_javascript

            _languages[key] = Language(tree_sitter_javascript.language())
    return _languages[key]


class TsParser:
    """SPEC-4 :class:`aiscan.parse.base.Parser` backend for TS/TSX/JS."""

    def __init__(self, on_secret: Callable[[SecretFinding], None] | None = None) -> None:
        self._on_secret = on_secret

    def parse(self, path: str, source: str) -> ModuleIR | ParseErrorInfo:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        try:
            parser = TSParser(_language(ext))
            tree = parser.parse(source.encode("utf-8", "replace"))
        except Exception as exc:  # grammar/binding failure — structured, never raised
            return ParseErrorInfo(path=path, message=str(exc), line=None)
        root = tree.root_node
        if root is None or root.type != "program":
            return ParseErrorInfo(path=path, message="unparseable", line=None)
        lowering = _TsLowering(path, source, self._on_secret)
        return lowering.lower_module(root)


class _TsLowering:
    def __init__(
        self, path: str, source: str, on_secret: Callable[[SecretFinding], None] | None
    ) -> None:
        self.path = path
        self.source = source
        self.src = source.encode("utf-8", "replace")
        self.on_secret = on_secret
        self.imports: list[ImportIR] = []
        self.exports: list[tuple[str, str]] = []

    # -- helpers ------------------------------------------------------------

    def span(self, node: Node) -> Span:
        return Span(
            file=self.path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            col_start=node.start_point[1],
            col_end=node.end_point[1],
        )

    def text(self, node: Node) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    @staticmethod
    def field(node: Node, name: str) -> Node | None:
        return node.child_by_field_name(name)

    @staticmethod
    def named(node: Node) -> list[Node]:
        return [c for c in node.named_children if c.type != "comment"]

    # -- module -------------------------------------------------------------

    def lower_module(self, root: Node) -> ModuleIR:
        body = self.stmts(root.named_children)
        defs = tuple(s.func for s in body if isinstance(s, FuncDefS))
        classes = tuple(s.cls for s in body if isinstance(s, ClassDefS))
        assigns = tuple(s for s in body if isinstance(s, AssignS))
        return ModuleIR(
            path=self.path,
            content_hash=hashlib.sha256(self.src).hexdigest(),
            loc=len(self.source.splitlines()),
            imports=tuple(self.imports),
            defs=defs,
            classes=classes,
            assigns=assigns,
            body=tuple(body),
            exports=tuple(self.exports),
        )

    # -- statements ---------------------------------------------------------

    def stmts(self, nodes: list[Node]) -> list[Stmt]:
        out: list[Stmt] = []
        for n in nodes:
            out.extend(self.stmt(n))
        return out

    def block(self, node: Node | None) -> tuple[Stmt, ...]:
        if node is None:
            return ()
        if node.type == "statement_block":
            return tuple(self.stmts(node.named_children))
        return tuple(self.stmt(node))

    def stmt(self, node: Node) -> list[Stmt]:
        sp = self.span(node)
        t = node.type
        if t == "comment" or t in (
            "interface_declaration", "type_alias_declaration", "ambient_declaration",
            "empty_statement", "import_alias",
        ):
            return []
        if t == "import_statement":
            self._lower_import(node)
            return []
        if t == "export_statement":
            return self._lower_export(node)
        if t in ("lexical_declaration", "variable_declaration"):
            out: list[Stmt] = []
            for decl in self.named(node):
                if decl.type == "variable_declarator":
                    out.extend(self._declarator(decl))
            return out
        if t == "expression_statement":
            inner = self.named(node)
            if not inner:
                return [OpaqueS(span=sp)]
            return self._expr_stmt(inner[0])
        if t in ("function_declaration", "generator_function_declaration"):
            return [FuncDefS(span=sp, func=self._function(node))]
        if t in ("class_declaration", "abstract_class_declaration"):
            return [ClassDefS(span=sp, cls=self._class(node))]
        if t == "if_statement":
            alt = self.field(node, "alternative")
            # else_clause wraps the else body / else-if statement.
            orelse: tuple[Stmt, ...] = ()
            if alt is not None:
                inner_nodes = self.named(alt) or []
                orelse = tuple(self.stmts(inner_nodes)) if inner_nodes else ()
            return [
                IfS(
                    span=sp,
                    test=self.expr_opt(self.field(node, "condition")),
                    body=self.block(self.field(node, "consequence")),
                    orelse=orelse,
                )
            ]
        if t in ("while_statement", "do_statement"):
            return [
                WhileS(
                    span=sp,
                    test=self.expr_opt(self.field(node, "condition")),
                    body=self.block(self.field(node, "body")),
                )
            ]
        if t == "for_statement":
            out = []
            init = self.field(node, "initializer")
            if init is not None:
                out.extend(self.stmt(init))
            increment = self.field(node, "increment")
            body = list(self.block(self.field(node, "body")))
            if increment is not None:
                body.extend(self._expr_stmt(increment))
            cond = self.field(node, "condition")
            test: Expr = (
                self.expr_opt(self._condition_expr(cond))
                if cond
                else BoolE(span=sp, value=True)
            )
            out.append(WhileS(span=sp, test=test, body=tuple(body)))
            return out
        if t == "for_in_statement":
            left = self.field(node, "left")
            return [
                ForS(
                    span=sp,
                    target=self.expr_opt(left) if left is not None else OpaqueE(span=sp),
                    iter=self.expr_opt(self.field(node, "right")),
                    body=self.block(self.field(node, "body")),
                )
            ]
        if t == "return_statement":
            vals = self.named(node)
            return [ReturnS(span=sp, value=self.expr(vals[0]) if vals else None)]
        if t == "try_statement":
            handler = self.field(node, "handler")
            finalizer = self.field(node, "finalizer")
            handlers: tuple[HandlerIR, ...] = ()
            if handler is not None:
                handlers = (
                    HandlerIR(type_name=None, body=self.block(self.field(handler, "body"))),
                )
            final = self.block(self.field(finalizer, "body")) if finalizer is not None else ()
            return [
                TryS(
                    span=sp,
                    body=self.block(self.field(node, "body")),
                    handlers=handlers,
                    final=final,
                )
            ]
        if t == "break_statement":
            return [BreakS(span=sp)]
        if t == "continue_statement":
            return [ContinueS(span=sp)]
        if t == "switch_statement":
            return [self._switch(node, sp)]
        if t == "statement_block":
            return list(self.block(node))
        if t == "labeled_statement":
            labeled = self.field(node, "body")
            return list(self.stmt(labeled)) if labeled is not None else [OpaqueS(span=sp)]
        return [OpaqueS(span=sp)]

    def _condition_expr(self, cond: Node | None) -> Node | None:
        # for(;cond;) condition is wrapped in an expression node already.
        return cond

    def _expr_stmt(self, node: Node) -> list[Stmt]:
        sp = self.span(node)
        if node.type == "assignment_expression":
            left = self.field(node, "left")
            right = self.field(node, "right")
            if left is not None and right is not None:
                if left.type in ("object_pattern", "array_pattern"):
                    return self._destructure(left, self.expr(right), sp)
                return [AssignS(span=sp, targets=(self.expr(left),), value=self.expr(right))]
        if node.type == "augmented_assignment_expression":
            left = self.field(node, "left")
            right = self.field(node, "right")
            op_node = self.field(node, "operator")
            if left is not None and right is not None:
                op = (self.text(op_node) if op_node is not None else "?=").rstrip("=") or "+"
                return [
                    AugAssignS(span=sp, target=self.expr(left), op=op, value=self.expr(right))
                ]
        if node.type == "sequence_expression":
            return [s for child in self.named(node) for s in self._expr_stmt(child)]
        return [ExprS(span=sp, expr=self.expr(node))]

    # -- imports / exports ---------------------------------------------------

    def _string_value(self, node: Node) -> str:
        if node.type in ("string", "template_string"):
            parts = []
            for child in node.children:
                if child.type == "string_fragment":
                    parts.append(self.text(child))
                elif child.type == "escape_sequence":
                    raw = self.text(child)
                    parts.append(_ESCAPES.get(raw[1:2], raw[1:]))
            return "".join(parts)
        return self.text(node).strip("'\"`")

    def _lower_import(self, node: Node) -> None:
        source = self.field(node, "source")
        if source is None:
            return
        module = self._string_value(source)
        sp = self.span(node)
        # `import type {...}` — the `type` keyword sits on the statement itself.
        if any(c.type == "type" and not c.is_named for c in node.children):
            return
        names: list[tuple[str, str | None]] = []
        star = False
        kind = "from"
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for item in clause.named_children:
                if item.type == "identifier":  # default import
                    names.append(("default", self.text(item)))
                elif item.type == "namespace_import":
                    ns = [c for c in item.named_children if c.type == "identifier"]
                    if ns:
                        kind = "import"
                        names.append((module, self.text(ns[0])))
                elif item.type == "named_imports":
                    for spec in item.named_children:
                        if spec.type != "import_specifier":
                            continue
                        if any(c.type == "type" for c in spec.children):
                            continue  # `import {type X}`
                        name_node = self.field(spec, "name")
                        alias_node = self.field(spec, "alias")
                        if name_node is not None:
                            names.append(
                                (
                                    self.text(name_node),
                                    self.text(alias_node) if alias_node is not None else None,
                                )
                            )
        self.imports.append(
            ImportIR(kind=kind, module=module, names=tuple(names), level=0, star=star, span=sp)
        )

    def _maybe_require(self, decl_name: Node, value: Node, sp: Span) -> bool:
        """``const x = require('m')`` / ``const {a} = require('m')`` → ImportIR."""
        if value.type != "call_expression":
            return False
        fn = self.field(value, "function")
        args = self.field(value, "arguments")
        if fn is None or self.text(fn) != "require" or args is None:
            return False
        arg_nodes = self.named(args)
        if len(arg_nodes) != 1 or arg_nodes[0].type != "string":
            return False
        module = self._string_value(arg_nodes[0])
        if decl_name.type == "identifier":
            self.imports.append(
                ImportIR(
                    kind="import",
                    module=module,
                    names=((module, self.text(decl_name)),),
                    level=0,
                    star=False,
                    span=sp,
                )
            )
            return True
        if decl_name.type == "object_pattern":
            names: list[tuple[str, str | None]] = []
            for prop in self.named(decl_name):
                if prop.type == "shorthand_property_identifier_pattern":
                    names.append((self.text(prop), None))
                elif prop.type == "pair_pattern":
                    key = self.field(prop, "key")
                    val = self.field(prop, "value")
                    if key is not None and val is not None and val.type == "identifier":
                        names.append((self.text(key), self.text(val)))
            self.imports.append(
                ImportIR(
                    kind="from", module=module, names=tuple(names), level=0, star=False, span=sp
                )
            )
            return True
        return False

    def _lower_export(self, node: Node) -> list[Stmt]:
        sp = self.span(node)
        source = self.field(node, "source")
        if source is not None:
            # export {A as B} from './x'  |  export * from './x'
            module = self._string_value(source)
            names: list[tuple[str, str | None]] = []
            star = any(c.type == "*" for c in node.children)
            for clause in node.named_children:
                if clause.type == "export_clause":
                    for spec in clause.named_children:
                        if spec.type != "export_specifier":
                            continue
                        name_node = self.field(spec, "name")
                        alias_node = self.field(spec, "alias")
                        if name_node is None:
                            continue
                        local = self.text(name_node)
                        public = self.text(alias_node) if alias_node is not None else local
                        names.append((local, None))
                        self.exports.append((public, local))
            self.imports.append(
                ImportIR(
                    kind="from", module=module, names=tuple(names), level=0, star=star, span=sp
                )
            )
            return []

        decl = self.field(node, "declaration")
        is_default = any(c.type == "default" for c in node.children)
        if decl is not None:
            lowered = self.stmt(decl)
            for s in lowered:
                if isinstance(s, FuncDefS):
                    self.exports.append(("default" if is_default else s.func.name, s.func.name))
                elif isinstance(s, ClassDefS):
                    self.exports.append(("default" if is_default else s.cls.name, s.cls.name))
                elif isinstance(s, AssignS):
                    for target in s.targets:
                        if isinstance(target, NameE):
                            self.exports.append((target.name, target.name))
            return lowered

        value = self.field(node, "value")
        if is_default and value is not None:
            # export default <expr> — bind it to the local name "default".
            self.exports.append(("default", "default"))
            return [
                AssignS(span=sp, targets=(NameE(span=sp, name="default"),), value=self.expr(value))
            ]
        for clause in node.named_children:
            if clause.type == "export_clause":  # export {a, b as c}
                for spec in clause.named_children:
                    if spec.type != "export_specifier":
                        continue
                    name_node = self.field(spec, "name")
                    alias_node = self.field(spec, "alias")
                    if name_node is None:
                        continue
                    local = self.text(name_node)
                    public = self.text(alias_node) if alias_node is not None else local
                    self.exports.append((public, local))
        return []

    # -- declarations ---------------------------------------------------------

    def _declarator(self, decl: Node) -> list[Stmt]:
        sp = self.span(decl)
        name = self.field(decl, "name")
        value = self.field(decl, "value")
        if name is None:
            return [OpaqueS(span=sp)]
        if value is None:
            return []  # bare `let x;` — nothing to bind
        if self._maybe_require(name, value, sp):
            return []
        if value.type in ("arrow_function", "function_expression", "generator_function") and (
            name.type == "identifier"
        ):
            func = self._function(value, name_override=self.text(name))
            return [FuncDefS(span=sp, func=func)]
        if name.type == "identifier":
            return [
                AssignS(span=sp, targets=(NameE(span=self.span(name), name=self.text(name)),),
                        value=self.expr(value))
            ]
        if name.type in ("object_pattern", "array_pattern"):
            return self._destructure(name, self.expr(value), sp)
        return [OpaqueS(span=sp)]

    def _destructure(self, pattern: Node, value: Expr, sp: Span) -> list[Stmt]:
        """``const {choices} = resp`` → ``choices = resp.choices`` etc.
        (SPEC-4 §2.1: F2 dispatch detection depends on this.)"""
        out: list[Stmt] = []
        if pattern.type == "object_pattern":
            for prop in self.named(pattern):
                psp = self.span(prop)
                if prop.type == "shorthand_property_identifier_pattern":
                    prop_name = self.text(prop)
                    out.append(
                        AssignS(
                            span=psp,
                            targets=(NameE(span=psp, name=prop_name),),
                            value=AttrE(span=psp, base=value, attr=prop_name),
                        )
                    )
                elif prop.type == "pair_pattern":
                    key = self.field(prop, "key")
                    val = self.field(prop, "value")
                    if key is not None and val is not None and val.type == "identifier":
                        out.append(
                            AssignS(
                                span=psp,
                                targets=(NameE(span=psp, name=self.text(val)),),
                                value=AttrE(span=psp, base=value, attr=self.text(key)),
                            )
                        )
                elif prop.type == "object_assignment_pattern":
                    # {a = default} — bind a to the member; default dropped (union-ish).
                    left = self.field(prop, "left")
                    if left is not None and left.type == "shorthand_property_identifier_pattern":
                        prop_name = self.text(left)
                        out.append(
                            AssignS(
                                span=psp,
                                targets=(NameE(span=psp, name=prop_name),),
                                value=AttrE(span=psp, base=value, attr=prop_name),
                            )
                        )
        elif pattern.type == "array_pattern":
            for i, elem in enumerate(self.named(pattern)):
                if elem.type == "identifier":
                    esp = self.span(elem)
                    out.append(
                        AssignS(
                            span=esp,
                            targets=(NameE(span=esp, name=self.text(elem)),),
                            value=SubscriptE(span=esp, base=value, index=NumE(span=esp, value=i)),
                        )
                    )
        return out or [OpaqueS(span=sp)]

    def _params(self, node: Node | None) -> tuple[Param, ...]:
        if node is None:
            return ()
        if node.type == "identifier":  # single-param arrow without parens
            return (Param(name=self.text(node), kind="pos"),)
        params: list[Param] = []
        for i, child in enumerate(self.named(node)):
            t = child.type
            if t in ("required_parameter", "optional_parameter"):
                pattern = self.field(child, "pattern")
                default = self.field(child, "value")
                name = (
                    self.text(pattern)
                    if pattern is not None and pattern.type == "identifier"
                    else f"arg{i}"
                )
                params.append(
                    Param(
                        name=name,
                        kind="pos",
                        default=self.expr(default) if default is not None else None,
                    )
                )
            elif t == "identifier":
                params.append(Param(name=self.text(child), kind="pos"))
            elif t == "assignment_pattern":
                left = self.field(child, "left")
                right = self.field(child, "right")
                params.append(
                    Param(
                        name=(
                            self.text(left)
                            if left is not None and left.type == "identifier"
                            else f"arg{i}"
                        ),
                        kind="pos",
                        default=self.expr(right) if right is not None else None,
                    )
                )
            elif t in ("rest_parameter", "rest_pattern"):
                inner = [c for c in child.named_children if c.type == "identifier"]
                params.append(
                    Param(name=self.text(inner[0]) if inner else f"arg{i}", kind="vararg")
                )
            else:  # destructured / typed exotic params
                params.append(Param(name=f"arg{i}", kind="pos"))
        return tuple(params)

    def _function(self, node: Node, name_override: str | None = None) -> FuncIR:
        name_node = self.field(node, "name")
        name = name_override or (
            self.text(name_node) if name_node is not None else "<anonymous>"
        )
        body_node = self.field(node, "body")
        if body_node is not None and body_node.type == "statement_block":
            body = tuple(self.stmts(body_node.named_children))
        elif body_node is not None:  # arrow expression body ⇒ implicit return
            body = (ReturnS(span=self.span(body_node), value=self.expr(body_node)),)
        else:
            body = ()
        is_async = any(
            c.type == "async" or (not c.is_named and self.text(c) == "async")
            for c in node.children
        )
        return FuncIR(
            name=name,
            params=self._params(self.field(node, "parameters") or self.field(node, "parameter")),
            decorators=(),
            body=body,
            is_async=is_async,
            span=self.span(node),
        )

    def _class(self, node: Node) -> ClassIR:
        name_node = self.field(node, "name")
        heritage_bases: list[Expr] = []
        for child in node.children:
            if child.type == "class_heritage":
                for h in child.named_children:
                    if h.type == "extends_clause":
                        heritage_bases.extend(
                            self.expr(v) for v in self.named(h)
                        )
                    elif h.type not in ("implements_clause",):
                        heritage_bases.append(self.expr(h))
        body_assigns: list[AssignS] = []
        methods: list[FuncIR] = []
        body = self.field(node, "body")
        for member in self.named(body) if body is not None else []:
            if member.type == "method_definition":
                methods.append(self._method(member))
            elif member.type in (
                "public_field_definition",
                "field_definition",
                "property_definition",
            ):
                fname = self.field(member, "name") or self.field(member, "property")
                value = self.field(member, "value")
                if fname is None or value is None:
                    continue
                if value.type in ("arrow_function", "function_expression"):
                    methods.append(self._function(value, name_override=self.text(fname)))
                else:
                    msp = self.span(member)
                    body_assigns.append(
                        AssignS(
                            span=msp,
                            targets=(NameE(span=msp, name=self.text(fname)),),
                            value=self.expr(value),
                        )
                    )
        return ClassIR(
            name=self.text(name_node) if name_node is not None else "<anonymous>",
            bases=tuple(heritage_bases),
            decorators=(),
            body_assigns=tuple(body_assigns),
            methods=tuple(methods),
            span=self.span(node),
        )

    def _method(self, node: Node) -> FuncIR:
        name_node = self.field(node, "name")
        name = self.text(name_node) if name_node is not None else "<anonymous>"
        func = self._function(node, name_override=name)
        if name != "constructor":
            return func
        # `this` param convention: downstream ctor-attr logic (W2) keys on the
        # method name; body already lowers `this.x = e` via NameE("this").
        return func

    def _switch(self, node: Node, sp: Span) -> Stmt:
        subject_node = self.field(node, "value")
        subject = self.expr_opt(subject_node)
        body = self.field(node, "body")
        cases: list[tuple[Expr | None, tuple[Stmt, ...]]] = []
        for case in self.named(body) if body is not None else []:
            # The clause statements are the children in the "body" field slots.
            # Selection must be by field name: py-tree-sitter wraps tree nodes
            # in fresh objects, so identity against field(case, "value") never
            # excludes the label — it lowered as a spurious leading OpaqueS,
            # making fallthrough arms look non-empty (SPEC-5 §2.3 regression).
            clause_stmts = tuple(
                s
                for i, c in enumerate(case.children)
                if case.field_name_for_child(i) == "body"
                for s in self.stmt(c)
            )
            if case.type == "switch_case":
                test: Expr | None = CompareE(
                    span=self.span(case),
                    left=subject,
                    ops=("==",),
                    comparators=(self.expr_opt(self.field(case, "value")),),
                )
                cases.append((test, clause_stmts))
            elif case.type == "switch_default":
                cases.append((None, clause_stmts))
        result: tuple[Stmt, ...] = ()
        for test, stmts in reversed(cases):
            if test is None:
                result = stmts
            else:
                result = (IfS(span=sp, test=test, body=stmts, orelse=result),)
        return result[0] if len(result) == 1 and isinstance(result[0], IfS) else OpaqueS(span=sp)

    # -- expressions --------------------------------------------------------

    def expr_opt(self, node: Node | None) -> Expr:
        if node is None:
            return OpaqueE(span=Span(file=self.path, line_start=1, line_end=1))
        return self.expr(node)

    def expr(self, node: Node) -> Expr:
        sp = self.span(node)
        t = node.type

        if t == "identifier":
            return NameE(span=sp, name=self.text(node))
        if t == "this":
            return NameE(span=sp, name="this")
        if t == "meta_property":  # import.meta → AttrE(NameE("import"), "meta")
            return AttrE(span=sp, base=NameE(span=sp, name="import"), attr="meta")
        if t in ("member_expression",):
            obj = self.field(node, "object")
            prop = self.field(node, "property")
            if obj is None or prop is None:
                return OpaqueE(span=sp)
            return AttrE(span=sp, base=self.expr(obj), attr=self.text(prop))
        if t == "subscript_expression":
            obj = self.field(node, "object")
            index = self.field(node, "index")
            if obj is None or index is None:
                return OpaqueE(span=sp)
            return SubscriptE(span=sp, base=self.expr(obj), index=self.expr(index))
        if t in ("call_expression", "new_expression"):
            fn = self.field(node, "function") or self.field(node, "constructor")
            args_node = self.field(node, "arguments")
            if fn is None:
                return OpaqueE(span=sp)
            args: list[Expr] = []
            kwargs: list[Kwarg] = []
            if args_node is not None and args_node.type == "arguments":
                for arg in self.named(args_node):
                    if arg.type == "spread_element":
                        inner = self.named(arg)
                        spread_value = self.expr(inner[0]) if inner else OpaqueE(span=sp)
                        kwargs.append(Kwarg(name=None, value=spread_value))
                    else:
                        args.append(self.expr(arg))
            elif args_node is not None and args_node.type == "template_string":
                args.append(self.expr(args_node))  # tagged template
            return CallE(span=sp, callee=self.expr(fn), args=tuple(args), kwargs=tuple(kwargs))
        if t == "await_expression":
            inner = self.named(node)
            return self.expr(inner[0]) if inner else OpaqueE(span=sp)
        if t in ("parenthesized_expression", "non_null_expression", "as_expression",
                 "satisfies_expression", "type_assertion"):
            inner = self.named(node)
            return self.expr(inner[0]) if inner else OpaqueE(span=sp)
        if t == "string":
            return self._str_literal(sp, self._string_value(node))
        if t == "template_string":
            parts: list[str | Expr] = []
            for child in node.children:
                if child.type == "string_fragment":
                    fragment = self.text(child)
                    kind = classify_secret(fragment)
                    if kind is not None:
                        self._report_secret(sp, kind)
                        fragment = f"<REDACTED:{kind}>"
                    parts.append(fragment)
                elif child.type == "template_substitution":
                    inner = self.named(child)
                    parts.append(self.expr(inner[0]) if inner else OpaqueE(span=self.span(child)))
            if all(isinstance(p, str) for p in parts):
                return self._str_literal(sp, "".join(p for p in parts if isinstance(p, str)))
            return FStrE(span=sp, parts=tuple(parts))
        if t == "number":
            raw = self.text(node).replace("_", "")
            try:
                return NumE(span=sp, value=int(raw, 0))
            except ValueError:
                try:
                    return NumE(span=sp, value=float(raw))
                except ValueError:
                    return OpaqueE(span=sp)
        if t == "true":
            return BoolE(span=sp, value=True)
        if t == "false":
            return BoolE(span=sp, value=False)
        if t in ("null", "undefined"):
            return NoneE(span=sp)
        if t == "object":
            entries: list[DictEntry] = []
            for prop in self.named(node):
                if prop.type == "pair":
                    key = self.field(prop, "key")
                    value = self.field(prop, "value")
                    if value is None:
                        continue
                    entries.append(DictEntry(key=self._object_key(key), value=self.expr(value)))
                elif prop.type == "shorthand_property_identifier":
                    prop_name = self.text(prop)
                    psp = self.span(prop)
                    entries.append(
                        DictEntry(
                            key=StrE(span=psp, value=prop_name),
                            value=NameE(span=psp, name=prop_name),
                        )
                    )
                elif prop.type == "spread_element":
                    inner = self.named(prop)
                    entries.append(
                        DictEntry(
                            key=None,
                            value=self.expr(inner[0]) if inner else OpaqueE(span=self.span(prop)),
                        )
                    )
                elif prop.type == "method_definition":
                    name_node = self.field(prop, "name")
                    if name_node is not None:
                        entries.append(
                            DictEntry(
                                key=StrE(span=self.span(prop), value=self.text(name_node)),
                                value=OpaqueE(span=self.span(prop)),
                            )
                        )
            return DictE(span=sp, entries=tuple(entries))
        if t == "array":
            return ListE(
                span=sp,
                elems=tuple(
                    self.expr(e) if e.type != "spread_element" else OpaqueE(span=self.span(e))
                    for e in self.named(node)
                ),
            )
        if t == "binary_expression":
            left = self.field(node, "left")
            right = self.field(node, "right")
            op_node = self.field(node, "operator")
            op = self.text(op_node) if op_node is not None else "?"
            if left is None or right is None:
                return OpaqueE(span=sp)
            if op in _CMP_OPS:
                return CompareE(
                    span=sp,
                    left=self.expr(left),
                    ops=(_CMP_OPS[op],),
                    comparators=(self.expr(right),),
                )
            if op in _BOOL_OPS:
                return BoolOpE(
                    span=sp, op=_BOOL_OPS[op], operands=(self.expr(left), self.expr(right))
                )
            return BinOpE(span=sp, op=op, left=self.expr(left), right=self.expr(right))
        if t == "unary_expression":
            operand = self.field(node, "argument")
            op_node = self.field(node, "operator")
            op = self.text(op_node) if op_node is not None else "?"
            if operand is None:
                return OpaqueE(span=sp)
            return UnaryOpE(span=sp, op="not" if op == "!" else op, operand=self.expr(operand))
        if t == "ternary_expression":
            # cond ? a : b — union both branches; the guard is dropped, matching
            # ADG over-approximation semantics. op="either" (not "or"): both
            # branches are live results, so the resolver must apply none of the
            # ||-specific falsy-drop/short-circuit refinements (SPEC-5 §2.1).
            consequence = self.field(node, "consequence")
            alternative = self.field(node, "alternative")
            if consequence is None or alternative is None:
                return OpaqueE(span=sp)
            return BoolOpE(
                span=sp,
                op="either",
                operands=(self.expr(consequence), self.expr(alternative)),
            )
        if t == "arrow_function":
            body_node = self.field(node, "body")
            if body_node is not None and body_node.type != "statement_block":
                params = self._params(
                    self.field(node, "parameters") or self.field(node, "parameter")
                )
                return LambdaE(
                    span=sp, params=tuple(p.name for p in params), body=self.expr(body_node)
                )
            return OpaqueE(span=sp)  # block-bodied inline arrow (W4 revisits)
        if t == "assignment_expression":  # assignment used as expression
            right = self.field(node, "right")
            return self.expr(right) if right is not None else OpaqueE(span=sp)
        return OpaqueE(span=sp)

    def _object_key(self, key: Node | None) -> Expr | None:
        if key is None:
            return None
        ksp = self.span(key)
        if key.type in ("property_identifier", "identifier"):
            return StrE(span=ksp, value=self.text(key))
        if key.type == "string":
            return self._str_literal(ksp, self._string_value(key))
        if key.type == "computed_property_name":
            inner = self.named(key)
            return self.expr(inner[0]) if inner else OpaqueE(span=ksp)
        return OpaqueE(span=ksp)

    def _report_secret(self, sp: Span, kind: str) -> None:
        if self.on_secret is not None:
            self.on_secret(SecretFinding(path=self.path, evidence=str(sp), secret_kind=kind))

    def _str_literal(self, sp: Span, value: str) -> StrE:
        kind = classify_secret(value)
        if kind is None:
            return StrE(span=sp, value=value)
        self._report_secret(sp, kind)
        return StrE(span=sp, value=f"<REDACTED:{kind}>", redacted=True)
