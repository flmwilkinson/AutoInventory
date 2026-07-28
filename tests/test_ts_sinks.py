"""SPEC-4 W3: TS sink detection — SDK sinks (openai/anthropic/vercel-ai),
endpoint-agnostic fetch/axios shape scoring, gateway attribution, azure
deployment. Asserted at the Sink fact level."""

from __future__ import annotations

from aiscan.context import OrgPack, ResolverBudgets, ResolverStats
from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import ModuleGraph
from aiscan.modules.ts_graph import TsConfig
from aiscan.parse.registry import make_parser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import Sink, SinkEngine


def scan_sinks(
    source: str, path: str = "app.ts", org_pack: OrgPack | None = None
) -> list[Sink]:
    parser = make_parser("." + path.rsplit(".", 1)[-1])
    assert parser is not None
    mod = parser.parse(path, source)
    assert isinstance(mod, ModuleIR), mod
    graph = ModuleGraph({path: mod}, {}, ts_config=TsConfig())
    resolver = Resolver(
        graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats()
    )
    engine = SinkEngine(resolver, org_pack or OrgPack())
    return engine.scan_all().sinks


def only(sinks: list[Sink]) -> Sink:
    assert len(sinks) == 1, [s.signals for s in sinks]
    return sinks[0]


class TestSdkSinks:
    def test_openai_default_import(self) -> None:
        sink = only(
            scan_sinks(
                "import OpenAI from 'openai';\n"
                "const client = new OpenAI();\n"
                "async function go() {\n"
                "  return await client.chat.completions.create({\n"
                "    model: 'gpt-4o', messages: [] });\n"
                "}\n"
            )
        )
        assert sink.kind == "sdk"
        assert sink.api_style == "openai"
        assert sink.model.model == "gpt-4o"

    def test_openai_ctor_baseurl_endpoint(self) -> None:
        sink = only(
            scan_sinks(
                "import OpenAI from 'openai';\n"
                "const client = new OpenAI({ baseURL: 'https://gw.internal.example/v1' });\n"
                "async function go() {\n"
                "  return await client.chat.completions.create({ model: 'x', messages: [] });\n"
                "}\n"
            )
        )
        assert sink.endpoint == "https://gw.internal.example/v1"

    def test_anthropic_messages(self) -> None:
        sink = only(
            scan_sinks(
                "import Anthropic from '@anthropic-ai/sdk';\n"
                "const a = new Anthropic();\n"
                "async function go() {\n"
                "  return await a.messages.create({ model: 'claude-3', messages: [] });\n"
                "}\n"
            )
        )
        assert sink.api_style == "anthropic"
        assert sink.model.model == "claude-3"

    def test_vercel_ai_generate_text(self) -> None:
        sinks = scan_sinks(
            "import { generateText } from 'ai';\n"
            "async function go() {\n"
            "  return await generateText({ model: 'gpt-4o', prompt: 'hi' });\n"
            "}\n"
        )
        assert any(s.kind == "sdk" and "vercel-ai" in " ".join(s.signals) for s in sinks)


class TestHttpShapeSinks:
    def test_fetch_gateway_endpoint_agnostic(self) -> None:
        # The headline: a fetch loop against an internal gateway, no known host.
        sink = only(
            scan_sinks(
                "async function agent(msgs) {\n"
                "  const resp = await fetch('https://gw.internal.example/llm/v1/chat', {\n"
                "    method: 'POST',\n"
                "    headers: { Authorization: 'Bearer x' },\n"
                "    body: JSON.stringify({ model: 'internal-x1', messages: msgs }),\n"
                "  });\n"
                "  const data = await resp.json();\n"
                "  return data.choices[0].message;\n"
                "}\n"
            )
        )
        assert sink.kind == "shape"
        assert sink.model.model == "internal-x1"
        assert sink.endpoint == "https://gw.internal.example/llm/v1/chat"

    def test_axios_post_shape(self) -> None:
        sink = only(
            scan_sinks(
                "import axios from 'axios';\n"
                "async function call(msgs) {\n"
                "  const resp = await axios.post('https://api.internal/chat', {\n"
                "    model: 'gpt-4o', messages: msgs, temperature: 0,\n"
                "  });\n"
                "  return resp.data.choices[0];\n"
                "}\n"
            )
        )
        assert sink.kind == "shape"
        assert sink.model.model == "gpt-4o"

    def test_axios_config_object(self) -> None:
        sink = only(
            scan_sinks(
                "import axios from 'axios';\n"
                "async function call(msgs) {\n"
                "  const resp = await axios({ url: 'https://api.internal/v1/chat',\n"
                "    method: 'post', data: { model: 'm1', messages: msgs } });\n"
                "  return resp.data.choices[0].message;\n"
                "}\n"
            )
        )
        assert sink.kind == "shape"
        assert sink.model.model == "m1"

    def test_known_host_via_fetch(self) -> None:
        sink = only(
            scan_sinks(
                "async function go() {\n"
                "  const r = await fetch('https://api.openai.com/v1/chat/completions', {\n"
                "    body: JSON.stringify({ model: 'gpt-4o', messages: [] }) });\n"
                "  return r.json();\n"
                "}\n"
            )
        )
        assert sink.kind in ("shape", "host")
        assert "host:api.openai.com" in " ".join(sink.signals)

    def test_non_llm_fetch_is_not_a_sink(self) -> None:
        sinks = scan_sinks(
            "async function load() {\n"
            "  const r = await fetch('https://api.internal/users', {\n"
            "    body: JSON.stringify({ page: 1 }) });\n"
            "  return r.json();\n"
            "}\n"
        )
        assert sinks == []


class TestSecretRedactionInSink:
    def test_api_key_header_redacted(self) -> None:
        sinks = scan_sinks(
            "async function go() {\n"
            "  return await fetch('https://api.openai.com/v1/chat/completions', {\n"
            "    headers: { Authorization: 'Bearer sk-test00000000000000000000000000' },\n"
            "    body: JSON.stringify({ model: 'gpt-4o', messages: [] }) });\n"
            "}\n"
        )
        assert sinks  # still detected
        # Secret never appears in any emitted fact.
        blob = repr([s.model for s in sinks] + [s.endpoint for s in sinks])
        assert "sk-test" not in blob


class TestChainShapeEmbeddings:
    """SPEC-5 §3: ``embeddings.create`` behind indirection (typed DI param,
    factory-built client) is a sink via chain suffix + payload shape — the
    DocGen ``apps/api`` blind spot."""

    def test_di_param_embeddings(self) -> None:
        sink = only(
            scan_sinks(
                "import OpenAI from 'openai';\n"
                "const EMBEDDING_MODEL = process.env.MODEL_EMBEDDING"
                " || 'text-embedding-3-small';\n"
                "async function embed(openai: OpenAI, text: string) {\n"
                "  const response = await openai.embeddings.create({\n"
                "    model: EMBEDDING_MODEL, input: text });\n"
                "  return response.data[0].embedding;\n"
                "}\n"
            )
        )
        assert sink.kind == "shape"
        assert sink.task == "embedding"
        assert "chain:sdk-suffix" in sink.signals
        assert sink.model.method == "attribution:config_symbolic"

    def test_embeddings_without_model_not_matched(self) -> None:
        # No model key → the chain extractor abstains; nothing else scores.
        sinks = scan_sinks(
            "async function embed(client: unknown, text: string) {\n"
            "  return await (client as any).embeddings.create({ input: text });\n"
            "}\n"
        )
        assert sinks == []

    def test_chain_hint_scores_di_chat(self) -> None:
        sink = only(
            scan_sinks(
                "async function go(ctx: any) {\n"
                "  const r = await ctx.openai.chat.completions.create({\n"
                "    model: 'gpt-4o-mini', messages: [] });\n"
                "  return r.choices[0].message;\n"
                "}\n"
            )
        )
        assert sink.kind == "shape"
        assert sink.task == "chat"
        assert "chain:sdk-suffix" in sink.signals
