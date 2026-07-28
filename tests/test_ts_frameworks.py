"""SPEC-4 W4: TS F1 framework packs — @openai/agents (incl. barrel + handoff),
LangGraph.js node promotion + routes, Vercel AI generateText-with-tools.
Adversarial non-match (a user class named Agent, no AI imports)."""

from __future__ import annotations

from aiscan.context import OrgPack, ResolverBudgets, ResolverStats
from aiscan.facts.models import AgentDefF, ToolDefF, TransferF
from aiscan.frontends.framework.engine import F1Result, FrameworkEngine, load_packs
from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import ModuleGraph
from aiscan.modules.ts_graph import TsConfig
from aiscan.parse.registry import make_parser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import SinkEngine


def run_f1_ts(sources: dict[str, str], ts_config: TsConfig | None = None) -> F1Result:
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        parser = make_parser("." + path.rsplit(".", 1)[-1])
        assert parser is not None, path
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR), f"{path}: {mod}"
        modules[path] = mod
    graph = ModuleGraph(modules, {}, ts_config=ts_config or TsConfig())
    resolver = Resolver(
        graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats()
    )
    sink_engine = SinkEngine(resolver, OrgPack())
    sink_result = sink_engine.scan_all()
    engine = FrameworkEngine(
        resolver,
        sink_engine,
        load_packs(),
        llm_sink_spans=frozenset(s.span for s in sink_result.sinks),
        sinks=tuple(sink_result.sinks),
    )
    return engine.run()


def agents(result: F1Result) -> list[AgentDefF]:
    return [f for f in result.facts if isinstance(f, AgentDefF)]


class TestOpenAiAgentsJs:
    def test_agent_ctor_with_model_and_tools(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "import { Agent } from '@openai/agents';\n"
                    "const assistant = new Agent({\n"
                    "  name: 'Assistant',\n"
                    "  instructions: 'You only respond in haikus.',\n"
                    "  model: 'gpt-4o',\n"
                    "});\n"
                )
            }
        )
        found = agents(result)
        assert [a.name for a in found] == ["Assistant"]
        assert found[0].framework == "openai-agents-js"
        assert found[0].language == "typescript"

    def test_handoff_between_two_agents(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "import { Agent } from '@openai/agents';\n"
                    "const spanish = new Agent({ name: 'Spanish', instructions: 'es' });\n"
                    "const triage = new Agent({ name: 'Triage', instructions: 'route',\n"
                    "  handoffs: [spanish] });\n"
                )
            }
        )
        assert {a.name for a in agents(result)} == {"Spanish", "Triage"}
        transfers = [f for f in result.facts if isinstance(f, TransferF)]
        assert any(t.kind == "handoff" for t in transfers)

    def test_barrel_reexport_still_matches(self) -> None:
        # The TS twin of the Python re-export canonicalisation: Agent imported
        # through a local barrel must still fire the rule.
        result = run_f1_ts(
            {
                "src/index.ts": "export { Agent } from '@openai/agents';\n",
                "app.ts": (
                    "import { Agent } from './src';\n"
                    "const a = new Agent({ name: 'Barreled', instructions: 'x' });\n"
                ),
            }
        )
        assert [a.name for a in agents(result)] == ["Barreled"]

    def test_tool_defined_inline(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "import { Agent, tool } from '@openai/agents';\n"
                    "function lookupImpl(id) { return fetch('https://db/' + id); }\n"
                    "const lookup = tool({ name: 'lookup', execute: lookupImpl });\n"
                    "const a = new Agent({ name: 'A', instructions: 'x', tools: [lookup] });\n"
                )
            }
        )
        tools = [f for f in result.facts if isinstance(f, ToolDefF)]
        assert tools  # execute function became a ToolDef


class TestLangGraphJs:
    def test_add_node_promotes_only_model_node(self) -> None:
        result = run_f1_ts(
            {
                "graph.ts": (
                    "import { StateGraph } from '@langchain/langgraph';\n"
                    "import OpenAI from 'openai';\n"
                    "const client = new OpenAI();\n"
                    "async function planner(s) {\n"
                    "  return await client.chat.completions.create(\n"
                    "    { model: 'gpt-4o', messages: [] });\n"
                    "}\n"
                    "function passthrough(s) { return s; }\n"
                    "const g = new StateGraph({});\n"
                    "g.addNode('planner', planner);\n"
                    "g.addNode('passthrough', passthrough);\n"
                    "g.addEdge('planner', 'passthrough');\n"
                )
            }
        )
        # Only the node whose function contains a sink is promoted.
        names = {a.name for a in agents(result)}
        assert "planner" in names
        assert "passthrough" not in names


class TestVercelAi:
    def test_generate_text_with_tools_is_agent(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "import { generateText, tool } from 'ai';\n"
                    "function weatherImpl(loc) { return fetch('https://wx/' + loc); }\n"
                    "const weather = tool({ description: 'wx', execute: weatherImpl });\n"
                    "async function go() {\n"
                    "  return await generateText({ model: 'gpt-4o', prompt: 'hi',\n"
                    "    tools: { weather } });\n"
                    "}\n"
                )
            }
        )
        assert agents(result), "generateText with tools should be an agent"
        assert agents(result)[0].framework == "vercel-ai"

    def test_generate_text_without_tools_is_not_agent(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "import { generateText } from 'ai';\n"
                    "async function go() {\n"
                    "  return await generateText({ model: 'gpt-4o', prompt: 'hi' });\n"
                    "}\n"
                )
            }
        )
        assert not agents(result)  # plain call → sink/LLMCallSite, not an agent


class TestAdversarial:
    def test_user_class_named_agent_no_ai(self) -> None:
        result = run_f1_ts(
            {
                "app.ts": (
                    "class Agent {\n"
                    "  constructor(public name: string) {}\n"
                    "}\n"
                    "const a = new Agent('not-ai');\n"
                )
            }
        )
        assert not agents(result)  # local Agent, no @openai/agents import
