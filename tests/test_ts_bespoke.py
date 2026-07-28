"""SPEC-4 W5: bespoke agent recovery on TS - agent-shape F1-F5 over an async
fetch loop (the DocGen shape), dict-of-callables tool dispatch, wrapper fixed
point over a class SDK, and TS test-path tagging."""

from __future__ import annotations

from typing import ClassVar

from aiscan.context import OrgPack, ResolverBudgets, ResolverStats
from aiscan.facts.models import AgentDefF
from aiscan.frontends.bespoke.agent_shape import ShapeResult, analyze_shapes
from aiscan.frontends.bespoke.wrappers import WrapperAnalyzer, WrapperResult, load_registry
from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import ModuleGraph
from aiscan.modules.ts_graph import TsConfig
from aiscan.parse.registry import make_parser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import Sink, SinkEngine, is_test_path


def _pipeline(
    sources: dict[str, str], org_pack: OrgPack | None = None
) -> tuple[SinkEngine, Resolver, list[Sink]]:
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        parser = make_parser("." + path.rsplit(".", 1)[-1])
        assert parser is not None
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR), f"{path}: {mod}"
        modules[path] = mod
    graph = ModuleGraph(modules, {}, ts_config=TsConfig())
    resolver = Resolver(
        graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats()
    )
    engine = SinkEngine(resolver, org_pack or OrgPack())
    sinks = engine.scan_all().sinks
    return engine, resolver, sinks


def bespoke_agents(
    sources: dict[str, str], org_pack: OrgPack | None = None
) -> tuple[ShapeResult, WrapperResult]:
    engine, resolver, sinks = _pipeline(sources, org_pack)
    # Wrapper fixed point first (mirrors the pipeline), then agent shapes.
    analyzer = WrapperAnalyzer(resolver, engine, org_pack or OrgPack(), load_registry(None))
    wrapper_result = analyzer.fixed_point(sinks)
    all_sinks = [*sinks, *wrapper_result.wrapper_sinks]
    shape = analyze_shapes(
        all_sinks,
        resolver,
        engine,
        org_pack or OrgPack(),
        frozenset(s.span for s in all_sinks),
        excluded_spans=frozenset(wrapper_result.wrapper_def_spans),
    )
    return shape, wrapper_result


DOCGEN_SHAPE = (
    "async function runAgent(question) {\n"
    "  const messages = [{ role: 'system', content: 'You gather evidence.' },\n"
    "                    { role: 'user', content: question }];\n"
    "  while (true) {\n"
    "    const resp = await fetch('https://gw.internal.example/llm/v1/chat', {\n"
    "      method: 'POST',\n"
    "      body: JSON.stringify({ model: 'internal-x1', messages, tools: [] }),\n"
    "    });\n"
    "    const data = await resp.json();\n"
    "    const choice = data.choices[0];\n"
    "    if (choice.finish_reason !== 'tool_calls') {\n"
    "      return choice.message.content;\n"
    "    }\n"
    "    messages.push(choice.message);\n"
    "    for (const call of choice.message.tool_calls) {\n"
    "      const fn = TOOLS[call.function.name];\n"
    "      messages.push({ role: 'tool', content: fn(call) });\n"
    "    }\n"
    "  }\n"
    "}\n"
)


class TestAgentShape:
    def test_docgen_fetch_loop_is_bespoke_agent(self) -> None:
        shape, _ = bespoke_agents({"app.ts": "const TOOLS = {};\n" + DOCGEN_SHAPE})
        agents = [f for f in shape.facts if isinstance(f, AgentDefF)]
        assert agents, "the fetch loop should be recovered as a bespoke agent"
        assert agents[0].name == "runAgent"
        assert agents[0].kind == "bespoke"
        assert agents[0].language == "typescript"

    def test_gateway_model_attributed(self) -> None:
        _engine, _resolver, sinks = _pipeline({"app.ts": "const TOOLS = {};\n" + DOCGEN_SHAPE})
        assert any(
            s.model.model == "internal-x1"
            and s.endpoint == "https://gw.internal.example/llm/v1/chat"
            for s in sinks
        )


class TestWrapperFixedPoint:
    def test_class_sdk_wrapper_recovered(self) -> None:
        sources = {
            "bank_ai/client.ts": (
                "export class LLMClient {\n"
                "  async chat(messages) {\n"
                "    const r = await fetch('https://llm.bank.internal/v1/chat', {\n"
                "      body: JSON.stringify({ model: 'bank-gpt', messages }) });\n"
                "    const d = await r.json();\n"
                "    return d.choices[0].message;\n"
                "  }\n"
                "}\n"
            ),
            "app.ts": (
                "import { LLMClient } from './bank_ai/client';\n"
                "const client = new LLMClient();\n"
                "async function assistant(q) {\n"
                "  const msgs = [{ role: 'user', content: q }];\n"
                "  while (true) {\n"
                "    const out = await client.chat(msgs);\n"
                "    if (!out.tool_calls) { return out.content; }\n"
                "    msgs.push(out);\n"
                "  }\n"
                "}\n"
            ),
        }
        _, wrapper_result = bespoke_agents(sources)
        # LLMClient classified as a wrapper by the fixed point (no signature).
        assert any("LLMClient" in fq for fq in wrapper_result.classified)


class TestTsTestPaths:
    def test_ts_test_conventions(self) -> None:
        assert is_test_path("src/agent.test.ts")
        assert is_test_path("src/agent.spec.ts")
        assert is_test_path("__tests__/agent.ts")
        assert is_test_path("src/Button.stories.tsx")
        assert is_test_path("packages/x/__mocks__/openai.ts")
        assert not is_test_path("src/lib/evidence-agent.ts")
        assert not is_test_path("app/main.ts")

    def test_test_file_agent_tagged_not_dropped(self) -> None:
        shape, _ = bespoke_agents(
            {"agent.test.ts": "const TOOLS = {};\n" + DOCGEN_SHAPE}
        )
        agents = [f for f in shape.facts if isinstance(f, AgentDefF)]
        assert agents and agents[0].location == "test"  # kept, tagged


class TestBespokeToolExtraction:
    """SPEC-5 §4: OpenAI tool-schema literals reached through a getter, linked
    to implementations via a string-keyed dispatcher, side effects classified
    transitively through internal helpers (the DocGen llm-tools shape)."""

    SOURCES: ClassVar[dict[str, str]] = {
        "tools.ts": (
            "export const AVAILABLE_TOOLS = [\n"
            "  { type: 'function', function: { name: 'generate_chart',\n"
            "      description: 'Render a chart' } },\n"
            "  { type: 'function', function: { name: 'create_data_table',\n"
            "      description: 'Format a table' } },\n"
            "];\n"
            "export function getAvailableTools() {\n"
            "  return AVAILABLE_TOOLS;\n"
            "}\n"
            "async function runChart(code) {\n"
            "  const r = await fetch('https://sandbox.internal/execute', {\n"
            "    method: 'POST', body: JSON.stringify({ code }) });\n"
            "  return await r.json();\n"
            "}\n"
            "function formatTable(rows) {\n"
            "  return rows.join('\n');\n"
            "}\n"
            "export async function executeTool(toolName, args) {\n"
            "  switch (toolName) {\n"
            "    case 'generate_chart':\n"
            "      return await runChart(args.code);\n"
            "    case 'create_data_table':\n"
            "      return formatTable(args.rows);\n"
            "    default:\n"
            "      return null;\n"
            "  }\n"
            "}\n"
        ),
        "agent.ts": (
            "import { getAvailableTools, executeTool } from './tools';\n"
            "async function runAgent(ctx, question) {\n"
            "  const tools = getAvailableTools();\n"
            "  const messages = [{ role: 'system', content: 'sys' },\n"
            "                    { role: 'user', content: question }];\n"
            "  while (true) {\n"
            "    const resp = await ctx.openai.chat.completions.create({\n"
            "      model: 'gpt-4o-mini', messages: messages,\n"
            "      tools: tools.length > 0 ? tools : undefined });\n"
            "    const choice = resp.choices[0];\n"
            "    if (choice.finish_reason === 'tool_calls') {\n"
            "      const result = await executeTool(choice.name, choice);\n"
            "      messages.push({ role: 'tool', content: result });\n"
            "      continue;\n"
            "    }\n"
            "    return choice.message;\n"
            "  }\n"
            "}\n"
        ),
    }

    def test_schema_tools_extracted_with_effects(self) -> None:
        shape, _ = bespoke_agents(dict(self.SOURCES))
        agents = [f for f in shape.facts if isinstance(f, AgentDefF)]
        assert len(agents) == 1
        from aiscan.facts.models import BindToolF, ToolDefF

        tools = {f.name: f for f in shape.facts if isinstance(f, ToolDefF)}
        assert set(tools) == {"generate_chart", "create_data_table"}
        chart = tools["generate_chart"]
        assert chart.kind == "function"
        assert "external_send" in chart.side_effects
        assert chart.external_target == "sandbox.internal"
        table = tools["create_data_table"]
        assert table.kind == "function"
        assert table.side_effects == ()
        binds = [f for f in shape.facts if isinstance(f, BindToolF)]
        assert len(binds) == 2


class TestClosureAttribution:
    """SPEC-5 §5: a helper the agent calls, holding its own LLM call, is
    attributed to the agent (in_agent) instead of orphaning."""

    def test_helper_sink_in_member_spans(self) -> None:
        sources = {
            "m.ts": (
                # The helper stores its result and returns a status literal —
                # deliberately NOT wrapper-shaped (no passthrough params, does
                # not return the sink result); passthrough wrappers are the
                # wrapper fixed point's job.
                "async function summarise(ctx) {\n"
                "  const r = await ctx.openai.chat.completions.create({\n"
                "    model: 'gpt-4o-mini',\n"
                "    messages: [{ role: 'user', content: 'summarise the notes' }] });\n"
                "  ctx.results.push(r.choices[0].message);\n"
                "  return 'ok';\n"
                "}\n"
                "async function runAgent(ctx, question) {\n"
                "  const messages = [{ role: 'system', content: 'sys' },\n"
                "                    { role: 'user', content: question }];\n"
                "  while (true) {\n"
                "    const resp = await ctx.openai.chat.completions.create({\n"
                "      model: 'gpt-4o-mini', messages: messages, tools: [] });\n"
                "    const choice = resp.choices[0];\n"
                "    if (choice.finish_reason === 'tool_calls') {\n"
                "      const result = await summarise(ctx);\n"
                "      messages.push({ role: 'tool', content: result });\n"
                "      continue;\n"
                "    }\n"
                "    return choice.message;\n"
                "  }\n"
                "}\n"
            )
        }
        shape, _ = bespoke_agents(sources)
        agents = [f for f in shape.facts if isinstance(f, AgentDefF)]
        assert len(agents) == 1
        # The helper's sink is a member (attributed), not consumed, not orphan.
        member_files = {s.file for s in shape.member_spans}
        assert member_files == {"m.ts"}
        assert len(shape.member_spans) == 1
        (member,) = shape.member_spans
        assert member not in shape.consumed_spans


class TestCallerArgumentBinding:
    """SPEC-7 Z1: prompt/tools passed as anchor parameters recover from the
    anchor's call sites (the orchestrator(prompt, tools) idiom)."""

    def test_prompt_and_tools_from_caller(self) -> None:
        sources = {
            "m.ts": (
                "async function generate(ctx, systemPrompt, tools) {\n"
                "  const messages = [{ role: 'system', content: systemPrompt },\n"
                "                    { role: 'user', content: 'go' }];\n"
                "  while (true) {\n"
                "    const resp = await ctx.openai.chat.completions.create({\n"
                "      model: 'gpt-4o-mini', messages: messages,\n"
                "      tools: tools.length > 0 ? tools : undefined });\n"
                "    const choice = resp.choices[0];\n"
                "    if (choice.finish_reason === 'tool_calls') {\n"
                "      messages.push({ role: 'tool', content: choice.content });\n"
                "      continue;\n"
                "    }\n"
                "    return choice.message;\n"
                "  }\n"
                "}\n"
                "export async function run(ctx) {\n"
                "  const tools = [{ type: 'function',\n"
                "    function: { name: 'lookup_rates', description: 'Rates' } }];\n"
                "  const sp = 'You are the FX assistant.';\n"
                "  return await generate(ctx, sp, tools);\n"
                "}\n"
            )
        }
        shape, _ = bespoke_agents(sources)
        from aiscan.facts.models import BindPromptF, PromptDefF, ToolDefF

        agents = [f for f in shape.facts if isinstance(f, AgentDefF)]
        assert [a.name for a in agents] == ["generate"]
        prompts = [f for f in shape.facts if isinstance(f, PromptDefF)]
        assert any(f.content == "You are the FX assistant." for f in prompts)
        assert any(isinstance(f, BindPromptF) for f in shape.facts)
        tools = [f for f in shape.facts if isinstance(f, ToolDefF)]
        assert [t.name for t in tools] == ["lookup_rates"]
