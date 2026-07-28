"""IR lowering unit tests: every statement/expression kind (SPEC §8)."""

from __future__ import annotations

from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    AugAssignS,
    BinOpE,
    BoolE,
    BoolOpE,
    CallE,
    ClassDefS,
    CompareE,
    DictE,
    ExprS,
    ForS,
    FStrE,
    FuncDefS,
    IfS,
    LambdaE,
    ListE,
    ModuleIR,
    NameE,
    NoneE,
    NumE,
    OpaqueE,
    OpaqueS,
    ReturnS,
    StrE,
    SubscriptE,
    TryS,
    TupleE,
    UnaryOpE,
    WhileS,
    WithS,
)
from aiscan.parse.base import ParseErrorInfo, SecretFinding
from aiscan.parse.py_ast import AstParser


def lower(source: str) -> ModuleIR:
    result = AstParser().parse("m.py", source)
    assert isinstance(result, ModuleIR), result
    return result


def only_stmt(source: str) -> object:
    mod = lower(source)
    assert len(mod.body) == 1, mod.body
    return mod.body[0]


def only_expr(source: str) -> object:
    stmt = only_stmt(source)
    assert isinstance(stmt, ExprS)
    return stmt.expr


class TestStatements:
    def test_assign(self) -> None:
        s = only_stmt("x = 1")
        assert isinstance(s, AssignS)
        assert isinstance(s.targets[0], NameE)
        assert isinstance(s.value, NumE)

    def test_ann_assign_with_value(self) -> None:
        s = only_stmt("x: int = 2")
        assert isinstance(s, AssignS)

    def test_ann_assign_bare_is_dropped(self) -> None:
        assert lower("x: int").body == ()

    def test_aug_assign(self) -> None:
        s = only_stmt("x += 1")
        assert isinstance(s, AugAssignS)
        assert s.op == "+"

    def test_return(self) -> None:
        mod = lower("def f():\n    return 1")
        fn = mod.defs[0]
        assert isinstance(fn.body[0], ReturnS)

    def test_if_else(self) -> None:
        s = only_stmt("if a:\n    b = 1\nelse:\n    b = 2")
        assert isinstance(s, IfS)
        assert len(s.body) == 1 and len(s.orelse) == 1

    def test_while(self) -> None:
        s = only_stmt("while True:\n    x = 1")
        assert isinstance(s, WhileS)
        assert isinstance(s.test, BoolE)

    def test_for(self) -> None:
        s = only_stmt("for i in xs:\n    y = i")
        assert isinstance(s, ForS)
        assert isinstance(s.iter, NameE)

    def test_with(self) -> None:
        s = only_stmt("with open('f') as fh:\n    data = fh.read()")
        assert isinstance(s, WithS)
        assert isinstance(s.items[0].context, CallE)
        assert isinstance(s.items[0].asname, NameE)

    def test_try(self) -> None:
        s = only_stmt(
            "try:\n    x = 1\nexcept ValueError:\n    x = 2\nfinally:\n    x = 3"
        )
        assert isinstance(s, TryS)
        assert s.handlers[0].type_name == "ValueError"
        assert len(s.final) == 1

    def test_func_def_params(self) -> None:
        mod = lower("def f(a, b=1, *args, c, **kw):\n    pass")
        fn = mod.defs[0]
        kinds = [p.kind for p in fn.params]
        assert kinds == ["pos", "pos", "vararg", "kwonly", "kwarg"]
        assert fn.params[1].default is not None

    def test_nested_def_kept_in_body(self) -> None:
        mod = lower("def outer():\n    def inner():\n        return 1\n    return inner")
        outer = mod.defs[0]
        assert isinstance(outer.body[0], FuncDefS)
        assert outer.body[0].func.name == "inner"

    def test_class_def(self) -> None:
        mod = lower(
            "class C(Base):\n    attr = 1\n    def m(self):\n        return self.attr"
        )
        assert isinstance(mod.body[0], ClassDefS)
        cls = mod.classes[0]
        assert cls.name == "C"
        assert len(cls.body_assigns) == 1
        assert cls.methods[0].name == "m"

    def test_match_lowered_to_if_chain(self) -> None:
        s = only_stmt(
            "match x:\n    case 1:\n        a = 1\n    case _:\n        a = 2"
        )
        assert isinstance(s, IfS)
        assert isinstance(s.test, OpaqueE)
        assert len(s.orelse) == 1 and isinstance(s.orelse[0], IfS)

    def test_import_collected(self) -> None:
        mod = lower("import os\nfrom a.b import c as d\nfrom . import e\nfrom x import *")
        kinds = [(i.kind, i.module, i.level, i.star) for i in mod.imports]
        assert ("import", "os", 0, False) in kinds
        assert ("from", "a.b", 0, False) in kinds
        assert ("from", "", 1, False) in kinds
        assert ("from", "x", 0, True) in kinds

    def test_unmodelled_stmt_is_opaque(self) -> None:
        s = only_stmt("del x")
        assert isinstance(s, OpaqueS)

    def test_async_constructs(self) -> None:
        mod = lower(
            "async def f():\n    async with s() as c:\n        pass\n"
            "    async for i in c:\n        await g(i)"
        )
        fn = mod.defs[0]
        assert fn.is_async
        assert isinstance(fn.body[0], WithS)
        assert isinstance(fn.body[1], ForS)
        inner = fn.body[1]
        assert isinstance(inner, ForS)
        call_stmt = inner.body[0]
        assert isinstance(call_stmt, ExprS)
        assert isinstance(call_stmt.expr, CallE)  # await unwrapped

    def test_syntax_error_reported_not_raised(self) -> None:
        result = AstParser().parse("bad.py", "def f(:\n")
        assert isinstance(result, ParseErrorInfo)
        assert result.line is not None


class TestExpressions:
    def test_name_attr_chain(self) -> None:
        e = only_expr("a.b.c")
        assert isinstance(e, AttrE) and e.attr == "c"
        assert isinstance(e.base, AttrE) and e.base.attr == "b"
        assert isinstance(e.base.base, NameE)

    def test_call_args_kwargs(self) -> None:
        e = only_expr("f(1, x=2, **kw)")
        assert isinstance(e, CallE)
        assert isinstance(e.args[0], NumE)
        assert e.kwargs[0].name == "x"
        assert e.kwargs[1].name is None  # **spread

    def test_subscript(self) -> None:
        e = only_expr("d['k']")
        assert isinstance(e, SubscriptE)
        assert isinstance(e.index, StrE)

    def test_subscript_slice_opaque(self) -> None:
        e = only_expr("xs[1:2]")
        assert isinstance(e, SubscriptE)
        assert isinstance(e.index, OpaqueE)

    def test_constants(self) -> None:
        assert isinstance(only_expr("'s'"), StrE)
        assert isinstance(only_expr("1"), NumE)
        assert isinstance(only_expr("1.5"), NumE)
        assert isinstance(only_expr("True"), BoolE)
        assert isinstance(only_expr("None"), NoneE)
        assert isinstance(only_expr("b'raw'"), OpaqueE)

    def test_fstring(self) -> None:
        e = only_expr("f'hello {name} bye'")
        assert isinstance(e, FStrE)
        assert e.parts[0] == "hello "
        assert isinstance(e.parts[1], NameE)
        assert e.parts[2] == " bye"

    def test_containers(self) -> None:
        assert isinstance(only_expr("[1, 2]"), ListE)
        assert isinstance(only_expr("(1, 2)"), TupleE)
        d = only_expr("{'a': 1, **rest}")
        assert isinstance(d, DictE)
        assert isinstance(d.entries[0].key, StrE)
        assert d.entries[1].key is None  # **spread

    def test_operators(self) -> None:
        b = only_expr("'a' + x")
        assert isinstance(b, BinOpE) and b.op == "+"
        bo = only_expr("a and b or c")
        assert isinstance(bo, BoolOpE)
        u = only_expr("not a")
        assert isinstance(u, UnaryOpE) and u.op == "not"
        c = only_expr("a == b")
        assert isinstance(c, CompareE) and c.ops == ("==",)

    def test_lambda(self) -> None:
        e = only_expr("lambda a, b: a")
        assert isinstance(e, LambdaE)
        assert e.params == ("a", "b")

    def test_unmodelled_expr_is_opaque(self) -> None:
        assert isinstance(only_expr("[x for x in xs]"), OpaqueE)
        assert isinstance(only_expr("x if c else y"), OpaqueE)


class TestSecretRedaction:
    def test_openai_key_redacted(self) -> None:
        found: list[SecretFinding] = []
        parser = AstParser(on_secret=found.append)
        mod = parser.parse("cfg.py", 'KEY = "sk-test0000000000000000000000000000"\n')
        assert isinstance(mod, ModuleIR)
        assign = mod.assigns[0]
        assert isinstance(assign.value, StrE)
        assert assign.value.value == "<REDACTED:openai_key>"
        assert assign.value.redacted
        assert found and found[0].secret_kind == "openai_key"
        assert found[0].evidence == "cfg.py:1-1"

    def test_pem_and_aws_shapes(self) -> None:
        found: list[SecretFinding] = []
        parser = AstParser(on_secret=found.append)
        src = 'A = "AKIAABCDEFGHIJKLMNOP"\nB = "-----BEGIN RSA PRIVATE KEY-----"\n'
        assert isinstance(parser.parse("k.py", src), ModuleIR)
        assert sorted(f.secret_kind for f in found) == ["aws_access_key", "pem_private_key"]

    def test_normal_strings_untouched(self) -> None:
        found: list[SecretFinding] = []
        mod = AstParser(on_secret=found.append).parse("ok.py", 'x = "hello world"\n')
        assert isinstance(mod, ModuleIR)
        assert not found
