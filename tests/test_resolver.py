"""Resolver unit suite (P1 exit gate): every value-domain branch, bounds,
special forms, chain_root, memoisation."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aiscan.context import ResolverBudgets, ResolverStats
from aiscan.ir.nodes import AttrE, CallE, Expr, ModuleIR, NameE
from aiscan.ir.values import (
    Bool,
    BoundArgs,
    ClassInstance,
    ClassRef,
    DictVal,
    FuncRef,
    ListVal,
    NoneV,
    Num,
    PackageRef,
    Str,
    Symbolic,
    Template,
    Top,
    Value,
    ValueSet,
)
from aiscan.ir.walk import dotted_name, enclosing_region, iter_exprs
from aiscan.modules.graph import ModuleGraph
from aiscan.parse.py_ast import AstParser
from aiscan.resolve.engine import Resolver, Scope


def make_resolver(
    sources: dict[str, str],
    budgets: ResolverBudgets | None = None,
    versions: dict[str, str] | None = None,
) -> tuple[Resolver, ModuleGraph, ResolverStats]:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR), f"{path}: {mod}"
        modules[path] = mod
    graph = ModuleGraph(modules, versions or {})
    stats = ResolverStats()
    resolver = Resolver(graph, graph.build_symbol_tables(), budgets or ResolverBudgets(), stats)
    return resolver, graph, stats


def find_expr(
    graph: ModuleGraph, module: str, pred: Callable[[Expr], bool]
) -> Expr:
    mod = graph.by_name[module]
    for e in iter_exprs(mod.body, into_defs=True):
        if pred(e):
            return e
    raise AssertionError("expression not found")


def resolve_at(
    resolver: Resolver, graph: ModuleGraph, module: str, expr: Expr
) -> ValueSet:
    mod = graph.by_name[module]
    funcs, _cls = enclosing_region(mod, expr.span)
    scope: Scope | None = None
    if funcs:
        scope = resolver.function_scope(module, funcs, expr.span)
    return resolver.resolve(expr, module, scope)


def resolve_name_in(
    sources: dict[str, str], module: str, name: str, occurrence: int = 0
) -> ValueSet:
    """Resolve the Nth *use* of ``name`` (a NameE inside a marker call)."""
    resolver, graph, _ = make_resolver(sources)
    uses = [
        e
        for e in iter_exprs(graph.by_name[module].body, into_defs=True)
        if isinstance(e, CallE)
        and dotted_name(e.callee) == "use"
        and isinstance(e.args[0], NameE)
        and e.args[0].name == name
    ]
    expr = uses[occurrence].args[0]
    return resolve_at(resolver, graph, module, expr)


def the(vals: ValueSet) -> Value:
    assert len(vals) == 1, vals
    return next(iter(vals))


class TestLiterals:
    def test_scalars(self) -> None:
        resolver, graph, _ = make_resolver({"m.py": "use('s')\nuse(3)\nuse(True)\nuse(None)\n"})
        mod = graph.by_name["m"]
        exprs = [
            e.args[0]
            for e in iter_exprs(mod.body)
            if isinstance(e, CallE) and dotted_name(e.callee) == "use"
        ]
        assert the(resolver.resolve(exprs[0], "m")) == Str(s="s")
        assert the(resolver.resolve(exprs[1], "m")) == Num(v=3)
        assert the(resolver.resolve(exprs[2], "m")) == Bool(v=True)
        assert the(resolver.resolve(exprs[3], "m")) == NoneV()


class TestNames:
    def test_local_straight_line_last_wins(self) -> None:
        src = "def f():\n    x = 'a'\n    x = 'b'\n    use(x)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "x")) == Str(s="b")

    def test_branch_union(self) -> None:
        src = "def f(c):\n    if c:\n        x = 'a'\n    else:\n        x = 'b'\n    use(x)\n"
        assert resolve_name_in({"m.py": src}, "m", "x") == frozenset({Str(s="a"), Str(s="b")})

    def test_module_global_union_of_assigns(self) -> None:
        src = "MODEL = 'm1'\nMODEL = 'm2'\n\ndef f():\n    use(MODEL)\n"
        assert resolve_name_in({"m.py": src}, "m", "MODEL") == frozenset(
            {Str(s="m1"), Str(s="m2")}
        )

    def test_unbound_name(self) -> None:
        vals = resolve_name_in({"m.py": "def f():\n    use(mystery)\n"}, "m", "mystery")
        assert the(vals) == Top(reason="unbound")

    def test_star_import_is_top(self) -> None:
        sources = {"lib.py": "a = 1\n", "m.py": "from lib import *\n\ndef f():\n    use(a)\n"}
        assert the(resolve_name_in(sources, "m", "a")) == Top(reason="star_import")

    def test_import_chain_reexport(self) -> None:
        sources = {
            "bank_ai/__init__.py": "from bank_ai.client import LLMClient\n",
            "bank_ai/client.py": "class LLMClient:\n    pass\n",
            "m.py": "from bank_ai import LLMClient\n\ndef f():\n    use(LLMClient)\n",
        }
        v = the(resolve_name_in(sources, "m", "LLMClient"))
        assert isinstance(v, ClassRef)
        assert v.fq == "bank_ai.client.LLMClient"

    def test_external_import_is_package_ref_with_version(self) -> None:
        resolver, graph, _ = make_resolver(
            {"m.py": "from openai import OpenAI\n\ndef f():\n    use(OpenAI)\n"},
            versions={"openai": "1.59.7"},
        )
        expr = find_expr(
            graph, "m", lambda e: isinstance(e, NameE) and e.name == "OpenAI"
        )
        v = the(resolve_at(resolver, graph, "m", expr))
        assert v == PackageRef(name="openai.OpenAI", version="1.59.7")


class TestStringsAndTemplates:
    def test_fstring_folds_resolvable_holes(self) -> None:
        src = "BASE = 'https://gw'\n\ndef f():\n    url = f'{BASE}/v1/chat'\n    use(url)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "url")) == Str(s="https://gw/v1/chat")

    def test_fstring_unresolved_hole_is_template(self) -> None:
        src = "def f(city):\n    url = f'https://api?q={city}'\n    use(url)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "url"))
        assert isinstance(v, Template)
        assert v.literal_prefix() == "https://api?q="
        assert v.render() == "https://api?q={city}"

    def test_concat_folds_literals(self) -> None:
        src = "A = 'https://h'\n\ndef f():\n    u = A + '/x'\n    use(u)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "u")) == Str(s="https://h/x")

    def test_concat_with_symbolic_is_template(self) -> None:
        src = (
            "import os\n\ndef f():\n"
            "    u = 'Bearer ' + os.environ['TOKEN']\n    use(u)\n"
        )
        v = the(resolve_name_in({"m.py": src}, "m", "u"))
        assert isinstance(v, Template)
        assert v.literal_prefix() == "Bearer "


class TestContainers:
    def test_list_literal(self) -> None:
        src = "def f():\n    xs = ['a', 'b']\n    use(xs)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "xs"))
        assert isinstance(v, ListVal)
        assert [the(e) for e in v.elems] == [Str(s="a"), Str(s="b")]
        assert not v.open

    def test_list_append_resolvable_is_folded(self) -> None:
        src = "def f():\n    xs = ['a']\n    xs.append('b')\n    use(xs)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "xs"))
        assert isinstance(v, ListVal)
        assert len(v.elems) == 2 and not v.open

    def test_list_append_unresolvable_marks_open(self) -> None:
        src = "def f(item):\n    xs = ['a']\n    xs.append(item)\n    use(xs)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "xs"))
        assert isinstance(v, ListVal)
        assert v.open

    def test_dict_literal_and_lookup(self) -> None:
        src = "def f():\n    d = {'model': 'gpt-4o'}\n    use(d['model'])\n"
        resolver, graph, _ = make_resolver({"m.py": src})
        expr = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, CallE) and dotted_name(e.callee) == "use",
        )
        assert isinstance(expr, CallE)
        assert the(resolve_at(resolver, graph, "m", expr.args[0])) == Str(s="gpt-4o")

    def test_dict_spread_is_open(self) -> None:
        src = "def f(extra):\n    d = {'a': 1, **extra}\n    use(d)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "d"))
        assert isinstance(v, DictVal)
        assert v.open

    def test_dict_subscript_assign_folded_at_module_level(self) -> None:
        src = "d = {'a': 'x'}\nd['b'] = 'y'\n\ndef f():\n    use(d)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "d"))
        assert isinstance(v, DictVal)
        assert v.keys() == ("a", "b")


class TestClassesAndCalls:
    WRAPPER = (
        "class LLMClient:\n"
        "    def __init__(self, model='bank-small-1'):\n"
        "        self.model = model\n"
        "        self.base_url = 'https://llm.internal/v1'\n"
    )

    def test_ctor_yields_instance_with_args(self) -> None:
        src = self.WRAPPER + "\ndef f():\n    c = LLMClient(model='x')\n    use(c)\n"
        v = the(resolve_name_in({"m.py": src}, "m", "c"))
        assert isinstance(v, ClassInstance)
        assert v.class_fq == "m.LLMClient"

    def test_instance_attr_via_ctor_kwarg(self) -> None:
        src = self.WRAPPER + "\ndef f():\n    m2 = LLMClient(model='big').model\n    use(m2)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m2")) == Str(s="big")

    def test_instance_attr_via_ctor_default(self) -> None:
        src = self.WRAPPER + "\ndef f():\n    m3 = LLMClient().model\n    use(m3)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m3")) == Str(s="bank-small-1")

    def test_instance_attr_constant(self) -> None:
        src = self.WRAPPER + "\ndef f():\n    u = LLMClient().base_url\n    use(u)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "u")) == Str(s="https://llm.internal/v1")

    def test_func_return_resolution(self) -> None:
        src = "def pick():\n    return 'gpt-4o'\n\ndef f():\n    m = pick()\n    use(m)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="gpt-4o")

    def test_func_two_returns_union(self) -> None:
        src = (
            "def pick(c):\n"
            "    if c:\n        return 'a'\n"
            "    return 'b'\n\n"
            "def f():\n    m = pick(1)\n    use(m)\n"
        )
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset({Str(s="a"), Str(s="b")})

    def test_switch_getter_literal_arg_selects_arm(self) -> None:
        # SPEC-5 §2.3: with a literal argument the If tests fold, so only the
        # selected arm's return is reachable — the exact value, no union.
        src = (
            "def pick(c):\n"
            "    if c == 1:\n        return 'a'\n"
            "    if c == 2:\n        return 'b'\n"
            "    return 'c'\n\n"
            "def f():\n    m = pick(1)\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="a")

    def test_many_flow_free_returns_union(self) -> None:
        # SPEC-5 §2.3: >_K_RET reachable returns, but every value is flow-free
        # (module consts) — the cap lifts and the arms union.
        src = (
            "A = 'a'\nB = 'b'\nC = 'c'\n\n"
            "def pick(c):\n"
            "    if c == 1:\n        return A\n"
            "    if c == 2:\n        return B\n"
            "    return C\n\n"
            "def f(k):\n    m = pick(k)\n    use(m)\n"
        )
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {Str(s="a"), Str(s="b"), Str(s="c")}
        )

    def test_many_non_flow_free_returns_is_dynamic(self) -> None:
        # Locally-assigned return values with an undecided arm count stay Top.
        src = (
            "def pick(c):\n"
            "    x = source()\n"
            "    if c == 1:\n        return x\n"
            "    if c == 2:\n        x = other()\n        return x\n"
            "    return x\n\n"
            "def f(k):\n    m = pick(k)\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Top(reason="dynamic")

    def test_dead_code_after_selected_return_excluded(self) -> None:
        src = (
            "def pick(c):\n"
            "    if c == 1:\n        return 'a'\n"
            "    return 'z'\n\n"
            "def f():\n    m = pick(1)\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="a")

    def test_call_with_arg_binding(self) -> None:
        src = (
            "def wrap(model):\n    return model\n\n"
            "def f():\n    m = wrap('gpt-4o-mini')\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="gpt-4o-mini")

    def test_method_call_with_self_binding(self) -> None:
        src = (
            "class C:\n"
            "    def __init__(self, model='d1'):\n"
            "        self.model = model\n"
            "    def which(self):\n"
            "        return self.model\n\n"
            "def f():\n    m = C(model='m9').which()\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="m9")

    def test_external_ctor_keeps_identity(self) -> None:
        src = (
            "from openai import OpenAI\n\n"
            "def f():\n    c = OpenAI(base_url='https://gw/v1')\n    use(c)\n"
        )
        v = the(resolve_name_in({"m.py": src}, "m", "c"))
        assert isinstance(v, ClassInstance)
        assert v.class_fq == "openai.OpenAI"
        assert v.ctor_args.get("base_url") == frozenset({Str(s="https://gw/v1")})

    def test_boto3_client_special(self) -> None:
        src = (
            "import boto3\n\n"
            "def f():\n    c = boto3.client('bedrock-runtime')\n    use(c)\n"
        )
        v = the(resolve_name_in({"m.py": src}, "m", "c"))
        assert isinstance(v, ClassInstance)
        assert v.class_fq == "boto3.bedrock-runtime-client"


class TestBoolOps:
    """SPEC-5 §2.1/§2.2: BoolOp union semantics and literal folding."""

    def test_env_or_literal_fallback(self) -> None:
        src = (
            "import os\n\n"
            "def f():\n    m = os.getenv('MODEL') or 'gpt-4o-mini'\n    use(m)\n"
        )
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {Symbolic(kind="env", key="MODEL"), Str(s="gpt-4o-mini")}
        )

    def test_or_chain_unions_all_undecidable_operands(self) -> None:
        src = (
            "import os\n\n"
            "def f():\n"
            "    m = os.getenv('A') or os.getenv('B') or 'lit'\n"
            "    use(m)\n"
        )
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {
                Symbolic(kind="env", key="A"),
                Symbolic(kind="env", key="B"),
                Str(s="lit"),
            }
        )

    def test_or_truthy_literal_short_circuits(self) -> None:
        src = "def f():\n    m = 'fixed' or unknown()\n    use(m)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Str(s="fixed")

    def test_or_drops_none_from_non_last_operand(self) -> None:
        # g(k) is {None, 'x'}: None cannot escape a non-last ``or`` operand,
        # and the maybe-falsy 'x' must NOT short-circuit the fallback away.
        src = (
            "def g(c):\n"
            "    if c:\n        return None\n"
            "    return 'x'\n\n"
            "def f(k):\n    m = g(k) or 'fallback'\n    use(m)\n"
        )
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {Str(s="x"), Str(s="fallback")}
        )

    def test_or_keeps_top_member(self) -> None:
        src = "def f(x):\n    m = x or 'lit'\n    use(m)\n"
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {Top(reason="unbound"), Str(s="lit")}
        )

    def test_and_unions_operands(self) -> None:
        src = "def f():\n    m = 'a' and 'b'\n    use(m)\n"
        assert resolve_name_in({"m.py": src}, "m", "m") == frozenset(
            {Str(s="a"), Str(s="b")}
        )

    def test_compare_folds_to_bool(self) -> None:
        src = "def f():\n    m = 'a' == 'a'\n    use(m)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Bool(v=True)

    def test_not_folds_over_bool(self) -> None:
        src = "def f():\n    m = not ('a' == 'b')\n    use(m)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Bool(v=True)

    def test_unresolvable_compare_is_dynamic(self) -> None:
        src = "def f(x):\n    m = x == 'a'\n    use(m)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Top(reason="dynamic")


class TestSymbolics:
    def test_environ_subscript(self) -> None:
        src = "import os\n\ndef f():\n    k = os.environ['LLM_MODEL']\n    use(k)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "k")) == Symbolic(
            kind="env", key="LLM_MODEL"
        )

    def test_environ_get_and_getenv(self) -> None:
        src = (
            "import os\n\ndef f():\n"
            "    a = os.environ.get('A_KEY')\n    use(a)\n"
            "    b = os.getenv('B_KEY')\n    use(b)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "a")) == Symbolic(kind="env", key="A_KEY")
        assert the(resolve_name_in({"m.py": src}, "m", "b")) == Symbolic(kind="env", key="B_KEY")

    def test_config_load_key_path(self) -> None:
        src = (
            "import json\n\ndef f():\n"
            "    cfg = json.load(open('settings.json'))\n"
            "    v = cfg['llm']['model']\n    use(v)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "v")) == Symbolic(
            kind="config", key="settings.json:llm.model"
        )

    def test_file_read_symbolic(self) -> None:
        src = "def f():\n    p = open('prompts/system.md').read()\n    use(p)\n"
        assert the(resolve_name_in({"m.py": src}, "m", "p")) == Symbolic(
            kind="config", key="file:prompts/system.md"
        )

    def test_with_open_read(self) -> None:
        src = (
            "def f():\n"
            "    with open('prompts/sys.md') as fh:\n"
            "        content = fh.read()\n"
            "        use(content)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "content")) == Symbolic(
            kind="config", key="file:prompts/sys.md"
        )

    def test_pathlib_read_text(self) -> None:
        src = (
            "from pathlib import Path\n\ndef f():\n"
            "    t = Path('prompts/a.md').read_text()\n    use(t)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "t")) == Symbolic(
            kind="config", key="file:prompts/a.md"
        )

    def test_argparse_namespace(self) -> None:
        src = (
            "import argparse\n\ndef f():\n"
            "    parser = argparse.ArgumentParser()\n"
            "    args = parser.parse_args()\n"
            "    m = args.model\n    use(m)\n"
        )
        assert the(resolve_name_in({"m.py": src}, "m", "m")) == Symbolic(
            kind="cli", key="model"
        )


class TestBoundsAndMemo:
    def test_depth_bound(self) -> None:
        chain = "\n".join(f"x{i} = x{i + 1}" for i in range(12)) + "\nx12 = 'end'\n"
        src = chain + "\ndef f():\n    use(x0)\n"
        vals = resolve_name_in({"m.py": src}, "m", "x0")
        assert Top(reason="depth") in vals

    def test_scan_budget_exhausted(self) -> None:
        resolver, graph, stats = make_resolver(
            {"m.py": "x = 'v'\n"},
            budgets=ResolverBudgets(scan_budget_s=0),
        )
        expr = find_expr(graph, "m", lambda e: isinstance(e, Expr))
        assert the(resolver.resolve(expr, "m")) == Top(reason="timeout")
        assert stats.top.get("timeout", 0) == 1

    def test_memo_hit_on_repeat_query(self) -> None:
        resolver, graph, stats = make_resolver({"m.py": "MODEL = 'gpt'\nuse(MODEL)\n"})
        expr = find_expr(graph, "m", lambda e: isinstance(e, NameE) and e.name == "MODEL")
        first = resolver.resolve(expr, "m")
        second = resolver.resolve(expr, "m")
        assert first == second == frozenset({Str(s="gpt")})
        assert stats.memo_hits >= 1
        assert stats.queries == 2


class TestChainRoot:
    def test_sdk_client_chain(self) -> None:
        src = (
            "from openai import OpenAI\n"
            "client = OpenAI(base_url='https://gw.internal/v1')\n\n"
            "def f():\n"
            "    client.chat.completions.create(model='m')\n"
        )
        resolver, graph, _ = make_resolver({"m.py": src})
        call = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, CallE)
            and dotted_name(e.callee) == "client.chat.completions.create",
        )
        assert isinstance(call, CallE)
        mod = graph.by_name["m"]
        funcs, _ = enclosing_region(mod, call.span)
        scope = resolver.function_scope("m", funcs, call.span)
        base, path = resolver.chain_root(call.callee, "m", scope)
        v = the(base)
        assert isinstance(v, ClassInstance)
        assert v.class_fq == "openai.OpenAI"
        assert v.ctor_args.get("base_url") == frozenset({Str(s="https://gw.internal/v1")})
        assert path == ("chat", "completions", "create")

    def test_module_function_chain(self) -> None:
        src = "import requests\n\ndef f(url, payload):\n    requests.post(url, json=payload)\n"
        resolver, graph, _ = make_resolver({"m.py": src})
        call = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, CallE) and dotted_name(e.callee) == "requests.post",
        )
        assert isinstance(call, CallE)
        base, path = resolver.chain_root(call.callee, "m")
        assert the(base) == PackageRef(name="requests", version=None)
        assert path == ("post",)

    def test_intermediate_call_in_chain(self) -> None:
        src = "import httpx\n\ndef f(payload):\n    httpx.Client().post('u', json=payload)\n"
        resolver, graph, _ = make_resolver({"m.py": src})
        call = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, CallE)
            and isinstance(e.callee, AttrE)
            and e.callee.attr == "post",
        )
        assert isinstance(call, CallE)
        base, path = resolver.chain_root(call.callee, "m")
        v = the(base)
        assert isinstance(v, ClassInstance)
        assert v.class_fq == "httpx.Client"
        assert path == ("post",)


class TestSelfInScope:
    def test_self_attr_with_bound_instance(self) -> None:
        src = (
            "import httpx\n\n"
            "class LLMClient:\n"
            "    def __init__(self, model='bank-small-1'):\n"
            "        self.model = model\n"
            "    def complete(self, messages):\n"
            "        resp = httpx.post('https://gw/v1', json={'model': self.model})\n"
            "        return resp\n"
        )
        resolver, graph, _ = make_resolver({"m.py": src})
        attr = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, AttrE)
            and e.attr == "model"
            and isinstance(e.base, NameE)
            and e.base.name == "self",
        )
        mod = graph.by_name["m"]
        funcs, cls = enclosing_region(mod, attr.span)
        assert cls is not None and cls.name == "LLMClient"
        instance = ClassInstance(
            class_fq="m.LLMClient",
            ctor_args=BoundArgs(named=(("model", frozenset({Str(s="bank-x2")})),)),
        )
        scope = resolver.function_scope("m", funcs, attr.span, self_instance=instance)
        assert the(resolver.resolve(attr, "m", scope)) == Str(s="bank-x2")

    def test_self_attr_default_when_no_ctor_args(self) -> None:
        src = (
            "class C:\n"
            "    def __init__(self, model='dflt'):\n"
            "        self.model = model\n"
            "    def go(self):\n"
            "        return self.model\n"
        )
        resolver, graph, _ = make_resolver({"m.py": src})
        attr = find_expr(
            graph,
            "m",
            lambda e: isinstance(e, AttrE)
            and e.attr == "model"
            and isinstance(e.base, NameE)
            and e.base.name == "self",
        )
        mod = graph.by_name["m"]
        funcs, _ = enclosing_region(mod, attr.span)
        scope = resolver.function_scope(
            "m", funcs, attr.span, self_instance=ClassInstance(class_fq="m.C")
        )
        assert the(resolver.resolve(attr, "m", scope)) == Str(s="dflt")


class TestGatewayPayload:
    """End-to-end resolver check on the headline fixture's payload shape."""

    def test_gateway_loop_payload_resolves(self) -> None:
        repo_src = {
            "app/loop.py": (
                "import os\nimport requests\n\n"
                "GATEWAY_URL = 'https://gw.internal.example/llm/v1/chat'\n"
                "SYSTEM_PROMPT = 'You are the ops assistant.'\n\n"
                "def run_agent(text):\n"
                "    messages = [\n"
                "        {'role': 'system', 'content': SYSTEM_PROMPT},\n"
                "        {'role': 'user', 'content': text},\n"
                "    ]\n"
                "    turns = 0\n"
                "    while turns < 8:\n"
                "        resp = requests.post(\n"
                "            GATEWAY_URL,\n"
                "            json={'model': 'internal-x1', 'messages': messages,\n"
                "                  'temperature': 0},\n"
                "        )\n"
                "        body = resp.json()\n"
                "        message = body['choices'][0]['message']\n"
                "        messages.append(message)\n"
                "        turns += 1\n"
                "    return 'done'\n"
            )
        }
        resolver, graph, _ = make_resolver(repo_src)
        call = find_expr(
            graph,
            "app.loop",
            lambda e: isinstance(e, CallE) and dotted_name(e.callee) == "requests.post",
        )
        assert isinstance(call, CallE)
        mod = graph.by_name["app.loop"]
        funcs, _ = enclosing_region(mod, call.span)
        scope = resolver.function_scope("app.loop", funcs, call.span)

        url = the(resolver.resolve(call.args[0], "app.loop", scope))
        assert url == Str(s="https://gw.internal.example/llm/v1/chat")

        payload_expr = next(k.value for k in call.kwargs if k.name == "json")
        payload = the(resolver.resolve(payload_expr, "app.loop", scope))
        assert isinstance(payload, DictVal)
        assert the(payload.get("model") or frozenset()) == Str(s="internal-x1")
        msgs = the(payload.get("messages") or frozenset())
        assert isinstance(msgs, ListVal)
        assert msgs.open  # append of an unresolvable message marks it open
        first = the(msgs.elems[0])
        assert isinstance(first, DictVal)
        assert the(first.get("content") or frozenset()) == Str(s="You are the ops assistant.")


class TestBuiltinsFallback:
    def test_builtin_name_is_func_ref(self) -> None:
        resolver, graph, _ = make_resolver({"m.py": "def f():\n    use(len)\n"})
        expr = find_expr(graph, "m", lambda e: isinstance(e, NameE) and e.name == "len")
        v = the(resolve_at(resolver, graph, "m", expr))
        assert v == FuncRef(fq="builtins.len", def_site="")


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
