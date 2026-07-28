"""SPEC-4 W0: TS/JS lowering — one check per §2.1 table row, error recovery,
secret redaction, and the DocGen full-clone parse proof."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiscan.ir.nodes import (
    AssignS,
    AttrE,
    BoolOpE,
    CallE,
    CompareE,
    DictE,
    ForS,
    FStrE,
    IfS,
    ListE,
    ModuleIR,
    NameE,
    NumE,
    ReturnS,
    StrE,
    SubscriptE,
    WhileS,
)
from aiscan.parse.base import ParseErrorInfo, SecretFinding
from aiscan.parse.registry import PARSEABLE_EXTS, PIPELINE_EXTS, make_parser
from aiscan.parse.ts_tree_sitter import TsParser

DOCGEN = Path(__file__).resolve().parents[1] / "aiscan-out" / "_clones" / "DocGen"


def parse(source: str, path: str = "m.ts") -> ModuleIR:
    result = TsParser().parse(path, source)
    assert isinstance(result, ModuleIR), result
    return result


class TestImportsExports:
    def test_default_named_namespace(self) -> None:
        mod = parse(
            'import OpenAI from "openai";\n'
            'import { tool as t, Agent } from "@openai/agents";\n'
            'import * as lg from "@langchain/langgraph";\n'
            'import type { Config } from "./types";\n'
        )
        assert [(i.module, i.names) for i in mod.imports] == [
            ("openai", (("default", "OpenAI"),)),
            ("@openai/agents", (("tool", "t"), ("Agent", None))),
            ("@langchain/langgraph", (("@langchain/langgraph", "lg"),)),
        ]  # `import type` stripped

    def test_require_forms(self) -> None:
        mod = parse(
            "const OpenAI = require('openai');\nconst { z } = require('zod');\n",
            path="m.js",
        )
        assert [(i.kind, i.module) for i in mod.imports] == [
            ("import", "openai"),
            ("from", "zod"),
        ]

    def test_exports_and_barrels(self) -> None:
        mod = parse(
            "export function run() { return 1; }\n"
            "export default class Planner {}\n"
            "export { Agent as PublicAgent } from './agent';\n"
            "export const NAME = 'x';\n"
        )
        assert ("run", "run") in mod.exports
        assert ("default", "Planner") in mod.exports
        assert ("PublicAgent", "Agent") in mod.exports  # barrel re-export
        assert ("NAME", "NAME") in mod.exports
        barrel = [i for i in mod.imports if i.module == "./agent"]
        assert barrel and barrel[0].names == (("Agent", None),)


class TestDeclarationsAndFunctions:
    def test_const_assign_and_arrow_named_fn(self) -> None:
        mod = parse("const model = 'gpt-4o';\nconst run = async (q) => { return q; };\n")
        assert isinstance(mod.assigns[0].value, StrE)
        assert mod.defs[0].name == "run" and mod.defs[0].is_async
        assert mod.defs[0].params[0].name == "q"

    def test_arrow_expression_body_implicit_return(self) -> None:
        mod = parse("const f = (x) => x + 1;\n")
        assert isinstance(mod.defs[0].body[0], ReturnS)

    def test_class_with_constructor_and_field_arrow(self) -> None:
        mod = parse(
            "class Client extends Base {\n"
            "  constructor(opts) { this.url = opts.url; }\n"
            "  ask = async (q) => { return q; };\n"
            "  name = 'client';\n"
            "}\n"
        )
        cls = mod.classes[0]
        assert cls.name == "Client"
        assert isinstance(cls.bases[0], NameE)
        assert {m.name for m in cls.methods} == {"constructor", "ask"}
        ctor = next(m for m in cls.methods if m.name == "constructor")
        assign = ctor.body[0]
        assert isinstance(assign, AssignS)
        target = assign.targets[0]
        assert isinstance(target, AttrE) and isinstance(target.base, NameE)
        assert target.base.name == "this" and target.attr == "url"
        assert cls.body_assigns[0].targets[0] == NameE(
            span=cls.body_assigns[0].targets[0].span, name="name"
        )


class TestExpressions:
    def test_new_and_member_chain_and_await(self) -> None:
        mod = parse(
            "const client = new OpenAI({ baseURL: process.env.GW });\n"
            "async function go() {\n"
            "  const r = await client.chat.completions.create({ model: 'gpt-4o' });\n"
            "  return r;\n"
            "}\n"
        )
        ctor = mod.assigns[0].value
        assert isinstance(ctor, CallE) and isinstance(ctor.callee, NameE)
        payload = ctor.args[0]
        assert isinstance(payload, DictE)
        key = payload.entries[0].key
        assert isinstance(key, StrE) and key.value == "baseURL"
        inner = mod.defs[0].body[0]
        assert isinstance(inner, AssignS)
        call = inner.value
        assert isinstance(call, CallE)  # await unwrapped
        chain = call.callee
        assert isinstance(chain, AttrE) and chain.attr == "create"

    def test_object_shorthand_spread_template(self) -> None:
        mod = parse("const body = { model, ...rest, note: `hi ${user}` };\n")
        entries = mod.assigns[0].value
        assert isinstance(entries, DictE)
        key0 = entries.entries[0].key
        assert isinstance(key0, StrE) and key0.value == "model"
        assert isinstance(entries.entries[0].value, NameE)
        assert entries.entries[1].key is None  # spread ⇒ open dict downstream
        assert isinstance(entries.entries[2].value, FStrE)

    def test_ternary_and_nullish_union_branches(self) -> None:
        mod = parse("const m = flag ? 'a' : 'b';\nconst n = x ?? 'fallback';\n")
        tern = mod.assigns[0].value
        assert isinstance(tern, BoolOpE) and len(tern.operands) == 2
        nullish = mod.assigns[1].value
        assert isinstance(nullish, BoolOpE) and nullish.op == "or"

    def test_comparisons_strict_equality(self) -> None:
        mod = parse("const done = reason !== 'tool_calls';\n")
        cmp_expr = mod.assigns[0].value
        assert isinstance(cmp_expr, CompareE) and cmp_expr.ops == ("!=",)

    def test_jsx_is_opaque_but_children_lower(self) -> None:
        mod = parse("export const App = () => <div>{run()}</div>;\n", path="app.tsx")
        # The component is a def; its JSX body is opaque — no crash, no loss of file.
        assert mod.defs or mod.assigns


class TestControlFlow:
    def test_while_loop_destructuring_dispatch(self) -> None:
        mod = parse(
            "async function agent() {\n"
            "  while (true) {\n"
            "    const resp = await call();\n"
            "    const { choices } = resp;\n"
            "    if (choices[0].finish_reason !== 'tool_calls') { return choices[0]; }\n"
            "    messages.push(choices[0].message);\n"
            "  }\n"
            "}\n"
        )
        loop = mod.defs[0].body[0]
        assert isinstance(loop, WhileS)
        destructure = loop.body[1]
        assert isinstance(destructure, AssignS)
        value = destructure.value
        assert isinstance(value, AttrE) and value.attr == "choices"  # resp.choices
        branch = loop.body[2]
        assert isinstance(branch, IfS)

    def test_for_of_and_c_style_for(self) -> None:
        mod = parse(
            "function f(xs) {\n"
            "  for (const x of xs) { use(x); }\n"
            "  for (let i = 0; i < 3; i++) { use(i); }\n"
            "}\n"
        )
        body = mod.defs[0].body
        assert isinstance(body[0], ForS)
        assert isinstance(body[1], AssignS)  # i = 0 initialiser
        assert isinstance(body[2], WhileS)  # c-style for → while

    def test_switch_lowers_to_if_chain(self) -> None:
        mod = parse(
            "function route(name) {\n"
            "  switch (name) {\n"
            "    case 'lookup': return doLookup();\n"
            "    case 'send': return doSend();\n"
            "    default: return null;\n"
            "  }\n"
            "}\n"
        )
        chain = mod.defs[0].body[0]
        assert isinstance(chain, IfS)
        assert isinstance(chain.test, CompareE)
        assert isinstance(chain.orelse[0], IfS)

    def test_array_index_and_subscript(self) -> None:
        mod = parse("const first = choices[0];\nconst tools = [a, b];\n")
        sub = mod.assigns[0].value
        assert isinstance(sub, SubscriptE)
        index = sub.index
        assert isinstance(index, NumE) and index.value == 0
        assert isinstance(mod.assigns[1].value, ListE)


class TestRobustness:
    def test_syntax_errors_recover(self) -> None:
        mod = parse("const ok = 1;\nfunction broken( {{{\nconst after = 2;\n")
        # tree-sitter recovers: the good statements still lower.
        names = [
            t.name
            for s in mod.body
            if isinstance(s, AssignS)
            for t in s.targets
            if isinstance(t, NameE)
        ]
        assert "ok" in names

    def test_secret_redaction_in_ts(self) -> None:
        found: list[SecretFinding] = []
        parser = TsParser(on_secret=found.append)
        mod = parser.parse(
            "m.ts", 'const KEY = "sk-test00000000000000000000000000000000";\n'
        )
        assert isinstance(mod, ModuleIR)
        value = mod.assigns[0].value
        assert isinstance(value, StrE) and value.redacted
        assert "sk-test" not in value.value
        assert found and found[0].secret_kind

    def test_unknown_statements_opaque_never_crash(self) -> None:
        mod = parse(
            "type X = { a: string };\ninterface Y { b: number }\n"
            "enum Z { A, B }\ndeclare const w: number;\n"
            "label: for (;;) { break label; }\n"
        )
        assert isinstance(mod, ModuleIR)


class TestRegistry:
    def test_extension_routing(self) -> None:
        assert isinstance(make_parser(".ts"), TsParser)
        assert isinstance(make_parser(".tsx"), TsParser)
        assert isinstance(make_parser(".mjs"), TsParser)
        assert make_parser(".java") is None
        assert ".py" in PIPELINE_EXTS and ".ts" in PARSEABLE_EXTS
        # W6: the pipeline now analyses TS/JS end-to-end.
        assert ".ts" in PIPELINE_EXTS and ".tsx" in PIPELINE_EXTS


@pytest.mark.skipif(not DOCGEN.is_dir(), reason="DocGen clone not present")
class TestDocGenParseProof:
    def test_parses_entire_clone_without_crashing(self) -> None:
        parser = TsParser()
        parsed, errors = 0, []
        for path in sorted(DOCGEN.rglob("*")):
            if path.suffix.lower() not in (".ts", ".tsx", ".js") or ".git" in path.parts:
                continue
            if "node_modules" in path.parts:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            result = parser.parse(path.relative_to(DOCGEN).as_posix(), source)
            if isinstance(result, ParseErrorInfo):
                errors.append(result.path)
            else:
                parsed += 1
        assert parsed >= 80, f"parsed only {parsed}"
        assert not errors, errors

    def test_docgen_agent_loop_shape_survives(self) -> None:
        source = (DOCGEN / "apps/web/src/lib/evidence-agent.ts").read_text(
            encoding="utf-8", errors="replace"
        )
        result = TsParser().parse("apps/web/src/lib/evidence-agent.ts", source)
        assert isinstance(result, ModuleIR)
        # The construct F2 will anchor on later: functions with loops + calls.
        assert result.defs or result.classes
