"""SPEC-4 W2: resolver additions for TS — process.env, JSON.stringify unwrap,
this/constructor attribute flow, .push list growth. All exercised through the
real resolver on TS-lowered IR."""

from __future__ import annotations

from aiscan.ir.nodes import AttrE, CallE, NameE, Span
from aiscan.ir.values import ClassInstance, DictVal, ListVal, Str, Symbolic
from aiscan.ir.walk import iter_exprs
from tests.test_resolver import resolve_at, the
from tests.test_ts_modules import make_mixed, use_of


def resolve_use(source: str, name: str, module: str = "m") -> object:
    resolver, graph = make_mixed({f"{module}.ts": source})
    return the(resolve_at(resolver, graph, module, use_of(graph, module, name)))


class TestProcessEnv:
    def test_member_access(self) -> None:
        value = resolve_use("const m = process.env.LLM_MODEL;\nuse(m);\n", "m")
        assert value == Symbolic(kind="env", key="LLM_MODEL")

    def test_subscript_access(self) -> None:
        value = resolve_use("const k = process.env['API_KEY'];\nuse(k);\n", "k")
        assert value == Symbolic(kind="env", key="API_KEY")

    def test_import_meta_env(self) -> None:
        value = resolve_use("const u = import.meta.env.VITE_URL;\nuse(u);\n", "u")
        assert value == Symbolic(kind="env", key="VITE_URL")


class TestJsonStringify:
    def test_stringify_unwraps_payload(self) -> None:
        # body: JSON.stringify({model, messages}) still exposes the dict.
        value = resolve_use(
            "const payload = { model: 'gpt-4o', temperature: 0 };\n"
            "const body = JSON.stringify(payload);\n"
            "use(body);\n",
            "body",
        )
        assert isinstance(value, DictVal)
        model = value.get("model")
        assert model is not None and Str(s="gpt-4o") in model

    def test_inline_stringify(self) -> None:
        value = resolve_use(
            "const body = JSON.stringify({ model: 'internal-x1' });\nuse(body);\n", "body"
        )
        assert isinstance(value, DictVal)


class TestThisAndConstructor:
    def test_this_field_returned_from_method_call(self) -> None:
        # this.endpoint resolves when the method is reached through the
        # instance (standalone `this`, like Python `self`, is unbound by design).
        resolver, graph = make_mixed(
            {
                "m.ts": (
                    "class Client {\n"
                    "  constructor() { this.endpoint = 'https://gw.internal/v1'; }\n"
                    "  ask() { return this.endpoint; }\n"
                    "}\n"
                    "const c = new Client();\nuse(c.ask());\n"
                )
            }
        )
        call = next(
            e.args[0]
            for e in iter_exprs(graph.by_name["m"].body, into_defs=True)
            if isinstance(e, CallE)
            and isinstance(e.callee, NameE)
            and e.callee.name == "use"
        )
        value = the(resolve_at(resolver, graph, "m", call))
        assert value == Str(s="https://gw.internal/v1")

    def test_constructor_field_via_instance(self) -> None:
        resolver, graph = make_mixed(
            {
                "m.ts": (
                    "class C {\n"
                    "  constructor() { this.endpoint = 'https://gw.internal/v1'; }\n"
                    "}\n"
                    "const c = new C();\nuse(c);\n"
                )
            }
        )
        instance = the(resolve_at(resolver, graph, "m", use_of(graph, "m", "c")))
        assert isinstance(instance, ClassInstance) and instance.class_fq == "m.C"
        # Read c.endpoint → the constructor literal.
        read = AttrE(span=Span("m", 1, 1), base=use_of(graph, "m", "c"), attr="endpoint")
        value = the(resolve_at(resolver, graph, "m", read))
        assert value == Str(s="https://gw.internal/v1")


class TestPushTracking:
    def test_push_grows_message_list(self) -> None:
        value = resolve_use(
            "function f() {\n"
            "  const messages = [{ role: 'system' }];\n"
            "  messages.push({ role: 'user' });\n"
            "  use(messages);\n"
            "  return messages;\n"
            "}\n",
            "messages",
        )
        assert isinstance(value, ListVal)
        assert len(value.elems) == 2  # initial + pushed entry (F4 message state)

    def test_push_dynamic_opens_list(self) -> None:
        value = resolve_use(
            "function f(x) {\n"
            "  const items = [];\n"
            "  items.push(x);\n"
            "  use(items);\n"
            "  return items;\n"
            "}\n",
            "items",
        )
        assert isinstance(value, ListVal) and value.open


class TestBoolOpAndSwitchGetter:
    """SPEC-5 §2.1/§2.3: the DocGen idiom — env-fallback consts behind a switch
    getter with a fallthrough arm — resolves to the exact arm's union."""

    def test_env_or_literal(self) -> None:
        src = "const m = process.env.MODEL_FAST || 'gpt-4o-mini';\nuse(m);\n"
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset(
            {Symbolic(kind="env", key="MODEL_FAST"), Str(s="gpt-4o-mini")}
        )

    def test_nullish_coalescing(self) -> None:
        src = "const m = process.env.A ?? 'fallback';\nuse(m);\n"
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset({Symbolic(kind="env", key="A"), Str(s="fallback")})

    def test_ternary_unions_both_branches(self) -> None:
        # op="either": the truthy consequence must NOT short-circuit the
        # alternative away.
        src = "declare const flag: boolean;\nconst m = flag ? 'a' : 'b';\nuse(m);\n"
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset({Str(s="a"), Str(s="b")})

    def test_fallthrough_switch_getter_selects_arm(self) -> None:
        src = (
            "const MODEL_FAST = process.env.MODEL_FAST || 'gpt-4o-mini';\n"
            "const MODEL_DEFAULT = process.env.MODEL_DEFAULT || 'gpt-4o';\n"
            "function getModelName(type) {\n"
            "  switch (type) {\n"
            "    case 'default':\n"
            "      return MODEL_DEFAULT;\n"
            "    case 'fast':\n"
            "    default:\n"
            "      return MODEL_FAST;\n"
            "  }\n"
            "}\n"
            "const m = getModelName('fast');\n"
            "use(m);\n"
        )
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset(
            {Symbolic(kind="env", key="MODEL_FAST"), Str(s="gpt-4o-mini")}
        )

    def test_switch_getter_other_arm(self) -> None:
        src = (
            "const A = 'model-a';\nconst B = 'model-b';\n"
            "function pick(kind) {\n"
            "  switch (kind) {\n"
            "    case 'a':\n      return A;\n"
            "    default:\n      return B;\n"
            "  }\n"
            "}\n"
            "const m = pick('a');\n"
            "use(m);\n"
        )
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset({Str(s="model-a")})


class TestSpec7Dataflow:
    """SPEC-7 Z2/Z3: spread merge, property access over objects, string +=."""

    def test_spread_merge_resolves_member(self) -> None:
        src = (
            "const defaults = { model: 'gpt-4o', temperature: 0.3 };\n"
            "function go(config) {\n"
            "  const merged = { ...defaults, ...config };\n"
            "  const m = merged.model;\n"
            "  use(m);\n"
            "}\n"
        )
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "m"))
        assert vals == frozenset({Str(s="gpt-4o")})

    def test_augassign_string_folds_to_template_union(self) -> None:
        src = (
            "function build(flag) {\n"
            "  let p = 'base';\n"
            "  if (flag) {\n"
            "    p += ' extra';\n"
            "  }\n"
            "  use(p);\n"
            "}\n"
        )
        resolver, graph = make_mixed({"m.ts": src})
        vals = resolve_at(resolver, graph, "m", use_of(graph, "m", "p"))
        assert vals == frozenset({Str(s="base"), Str(s="base extra")})
