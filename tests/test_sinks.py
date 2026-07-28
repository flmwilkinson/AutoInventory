"""Sink engine suite (P2 exit gate).

Gates: `bespoke_gateway_loop` and `azure_deployment` yield correct sinks and
attribution, asserted at fact level. Plus scorer threshold edges, ladder rung
order, prompt binding, and side-effect classification units.
"""

from __future__ import annotations

from pathlib import Path

from aiscan.cli import discover_python_files
from aiscan.context import OrgPack, ResolverBudgets, ResolverStats
from aiscan.ir.nodes import ModuleIR, Span, StrE
from aiscan.ir.values import DictVal, Hole, Str, Symbolic, Template, Top
from aiscan.modules.graph import ModuleGraph
from aiscan.parse.py_ast import AstParser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.attribution import (
    attribute_model,
    deployment_from_url,
    host_of,
)
from aiscan.sinks.engine import Sink, SinkEngine, SinkScanResult
from aiscan.sinks.shape import ShapeInputs, score_shape
from aiscan.sinks.side_effects import SideEffectReport, classify_body, extract_credential
from tests.conftest import fixture_repo


def engine_for_repo(
    repo: Path, org_pack: OrgPack | None = None
) -> tuple[SinkEngine, SinkScanResult]:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for rel in discover_python_files(repo):
        mod = parser.parse(rel, (repo / rel).read_text(encoding="utf-8"))
        assert isinstance(mod, ModuleIR)
        modules[rel] = mod
    graph = ModuleGraph(modules)
    resolver = Resolver(graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats())
    engine = SinkEngine(resolver, org_pack or OrgPack(), repo_root=repo)
    return engine, engine.scan_all()


def engine_for_sources(
    sources: dict[str, str], org_pack: OrgPack | None = None
) -> tuple[SinkEngine, SinkScanResult]:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for path, src in sources.items():
        mod = parser.parse(path, src)
        assert isinstance(mod, ModuleIR)
        modules[path] = mod
    graph = ModuleGraph(modules)
    resolver = Resolver(graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats())
    engine = SinkEngine(resolver, org_pack or OrgPack(), repo_root=None)
    return engine, engine.scan_all()


def sink_in(result: SinkScanResult, path_fragment: str) -> Sink:
    hits = [s for s in result.sinks if path_fragment in s.site.path]
    assert hits, f"no sink in {path_fragment}: {[s.site.path for s in result.sinks]}"
    return hits[0]


class TestGatewayLoopSinks:
    """P2 exit: the headline fixture, no known host anywhere."""

    def test_gateway_sink_found_and_attributed(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_gateway_loop"))
        sink = sink_in(result, "app/loop.py")
        assert sink.kind == "shape"
        assert sink.confidence == "high"
        assert sink.score is not None and sink.score >= 7
        assert sink.model.model == "internal-x1"
        assert sink.model.method == "attribution:literal"
        assert sink.model.endpoint == "https://gw.internal.example/llm/v1/chat"
        assert sink.model.api_style == "openai"
        assert sink.model.provider is None  # gateway host is in no registry
        assert sink.model.evidence and "app/loop.py" in sink.model.evidence[0]

    def test_gateway_prompt_bound_from_messages(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_gateway_loop"))
        sink = sink_in(result, "app/loop.py")
        assert sink.prompt is not None
        assert sink.prompt.content == "You are the ops assistant for the payments desk."
        assert sink.prompt.origin == "literal"
        assert not sink.prompt.dynamic

    def test_embedding_usage_is_sdk_sink(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_gateway_loop"))
        sink = sink_in(result, "app/index.py")
        assert sink.kind == "sdk"
        assert sink.task == "embedding"
        assert sink.model.model == "text-embedding-3-small"
        assert sink.model.provider == "openai"

    def test_tool_posts_are_not_llm_sinks(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_gateway_loop"))
        loop_sinks = [s for s in result.sinks if "app/loop.py" in s.site.path]
        assert len(loop_sinks) == 1  # only the gateway call, not send_payment


class TestAudioSinks:
    """OpenAI audio chains are model-carrying sinks with stt/tts tasks — a
    voice pipeline must not be invisible in the models table."""

    def test_transcription_is_stt_sink(self) -> None:
        _, result = engine_for_sources(
            {
                "voice.py": (
                    "from openai import AsyncOpenAI\n\n"
                    "client = AsyncOpenAI()\n\n\n"
                    "async def hear(f):\n"
                    "    return await client.audio.transcriptions.create(\n"
                    "        model='gpt-4o-transcribe', file=f\n"
                    "    )\n"
                )
            }
        )
        sink = sink_in(result, "voice.py")
        assert sink.kind == "sdk"
        assert sink.task == "stt"
        assert sink.model.model == "gpt-4o-transcribe"

    def test_streaming_speech_is_tts_sink(self) -> None:
        _, result = engine_for_sources(
            {
                "speak.py": (
                    "from openai import AsyncOpenAI\n\n"
                    "client = AsyncOpenAI()\n\n\n"
                    "def say(text):\n"
                    "    return client.audio.speech.with_streaming_response.create(\n"
                    "        model='gpt-4o-mini-tts', voice='ash', input=text\n"
                    "    )\n"
                )
            }
        )
        sink = sink_in(result, "speak.py")
        assert sink.kind == "sdk"
        assert sink.task == "tts"
        assert sink.model.model == "gpt-4o-mini-tts"

    def test_di_transcription_caught_by_shape(self) -> None:
        """The SDK's own voice adapters call `self._client.audio...` on an
        injected client — an opaque root the SDK-root match misses. The
        shape-suffix fallback (as for embeddings) must still catch it as stt."""
        _, result = engine_for_sources(
            {
                "stt_model.py": (
                    "class OpenAISTTModel:\n"
                    "    def __init__(self, model, openai_client):\n"
                    "        self.model = model\n"
                    "        self._client = openai_client\n\n"
                    "    async def transcribe(self, audio_file):\n"
                    "        return await self._client.audio.transcriptions.create(\n"
                    "            model=self.model, file=audio_file, temperature=0.0\n"
                    "        )\n"
                )
            }
        )
        sink = sink_in(result, "stt_model.py")
        assert sink.kind == "shape"
        assert sink.task == "stt"


class TestAzureDeployment:
    """P2 exit: deployment split — the foundation model is not in the code."""

    def test_azure_sink(self) -> None:
        _, result = engine_for_repo(fixture_repo("azure_deployment"))
        assert len(result.sinks) == 1
        sink = result.sinks[0]
        assert sink.kind == "shape"
        assert sink.confidence == "high"
        assert sink.model.deployment == "prod-gpt4"
        assert sink.model.model == {"symbolic": "external:azure:deployment:prod-gpt4"}
        assert sink.model.method == "attribution:external"
        endpoint = sink.model.endpoint
        assert isinstance(endpoint, str)
        assert endpoint.startswith("https://bankresource.openai.azure.com/openai/deployments/")


class TestOtherFixtureSinks:
    def test_raw_openai_loop_scores_high_with_host_corroboration(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_raw_openai_loop"))
        sink = sink_in(result, "agent.py")
        assert sink.kind == "shape"
        assert sink.score is not None and sink.score >= 8
        assert "host:api.openai.com" in sink.signals
        assert sink.model.model == "gpt-4o"

    def test_llm_call_only_sdk_sink(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_llm_call_only"))
        sink = sink_in(result, "summarise.py")
        assert sink.kind == "sdk"
        assert sink.model.model == "gpt-4o-mini"
        assert sink.prompt is not None
        assert sink.prompt.content == "Summarise in one paragraph."

    def test_wrapper_client_shape_sink_inside_method(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_wrapper"))
        sink = sink_in(result, "bank_ai/client.py")
        assert sink.kind == "shape"
        assert sink.site.owner is not None and sink.site.owner.name == "LLMClient"
        # SPEC-7 Z3: def-site `self` resolves through the class's own ctor —
        # the ctor default is a detected fact (call sites may still override,
        # which the wrapper fixed point handles per call site).
        assert sink.model.method == "attribution:constant"
        assert sink.model.model == "bank-small-1"

    def test_org_gateway_host_sink_for_dynamic_payload(self) -> None:
        org = OrgPack(gateway_hosts=("llm.bank.internal",))
        _, result = engine_for_repo(fixture_repo("bespoke_wrapper"), org_pack=org)
        sink = sink_in(result, "batch.py")
        assert sink.kind == "host"
        assert sink.model.provider == "org-gateway"

    def test_dynamic_prompt_origins(self) -> None:
        _, result = engine_for_repo(fixture_repo("dynamic_prompt"))
        sinks = [s for s in result.sinks if "replies.py" in s.site.path]
        assert len(sinks) == 2
        by_line = sorted(sinks, key=lambda s: s.span.line_start)
        file_prompt = by_line[0].prompt
        assert file_prompt is not None
        assert file_prompt.origin == "file"
        assert file_prompt.file_ref == "prompts/system.md"
        assert file_prompt.content_hash is not None
        assert isinstance(file_prompt.content, str)
        assert "escalation assistant" in file_prompt.content
        dyn_prompt = by_line[1].prompt
        assert dyn_prompt is not None
        assert dyn_prompt.dynamic
        assert dyn_prompt.content == {
            "template": "You greet {customer_name} warmly and briefly.",
            "dynamic": True,
        }

    def test_test_and_main_sinks_flagged(self) -> None:
        _, result = engine_for_repo(fixture_repo("adversarial_test_sink"))
        assert len(result.sinks) == 2
        flags = {(s.site.in_tests, s.site.under_main) for s in result.sinks}
        assert (True, False) in flags  # tests/test_client.py
        assert (False, True) in flags  # tool.py under __main__

    def test_agent_name_fixture_has_no_sinks(self) -> None:
        _, result = engine_for_repo(fixture_repo("adversarial_agent_name"))
        assert result.sinks == []
        assert result.suspected == []


class TestShapeScorerThresholds:
    def _inputs(self, **overrides: object) -> ShapeInputs:
        base: dict[str, object] = {
            "url_text": "https://internal.example/api",
            "payload": None,
            "payload_unresolved": False,
            "headers": None,
            "response_fields": frozenset(),
            "stream_hint": False,
        }
        base.update(overrides)
        return ShapeInputs(**base)  # type: ignore[arg-type]

    def _payload(self, *keys: str) -> DictVal:
        return DictVal(entries=tuple((k, frozenset({Str(s="x")})) for k in keys))

    def test_score_four_is_suspect_not_sink(self) -> None:
        headers = DictVal(entries=(("Authorization", frozenset({Str(s="Bearer t")})),))
        score = score_shape(
            self._inputs(payload=self._payload("model", "messages"), headers=headers)
        )
        assert score.score == 4
        assert score.is_suspect and not score.is_sink

    def test_score_five_is_medium_sink(self) -> None:
        headers = DictVal(entries=(("Authorization", frozenset({Str(s="Bearer t")})),))
        score = score_shape(
            self._inputs(
                payload=self._payload("model", "messages", "temperature"), headers=headers
            )
        )
        assert score.score == 5
        assert score.is_sink
        assert score.confidence() == "medium"

    def test_score_seven_is_high_sink(self) -> None:
        score = score_shape(
            self._inputs(
                url_text="https://gw.internal/llm/v1/chat/completions",
                payload=self._payload("model", "messages", "temperature"),
            )
        )
        assert score.score == 7
        assert score.confidence() == "high"

    def test_open_payload_lowers_confidence_not_score(self) -> None:
        open_payload = DictVal(
            entries=tuple(
                (k, frozenset({Str(s="x")})) for k in ("model", "messages", "temperature")
            ),
            open=True,
        )
        headers = DictVal(entries=(("x-api-key", frozenset({Str(s="k")})),))
        score = score_shape(self._inputs(payload=open_payload, headers=headers))
        assert score.score == 5
        assert score.confidence() == "low"

    def test_sampling_cap(self) -> None:
        payload = self._payload("model", "messages", "temperature", "top_p", "max_tokens")
        score = score_shape(self._inputs(payload=payload))
        assert score.score == 5  # 3 + capped 2

    def test_anthropic_style_inference(self) -> None:
        score = score_shape(
            self._inputs(
                url_text="https://gw.internal/v1/messages",
                payload=self._payload("model", "messages"),
            )
        )
        assert score.api_style == "anthropic"


class TestAttributionLadder:
    def test_literal_rung_wins(self) -> None:
        expr = StrE(span=_span(), value="gpt-4o")
        att = attribute_model(expr, frozenset({Str(s="gpt-4o")}))
        assert att.method == "attribution:literal"
        assert att.model == "gpt-4o"

    def test_constant_rung(self) -> None:
        att = attribute_model(None, frozenset({Str(s="bank-gpt4-prod")}))
        assert att.method == "attribution:constant"
        assert att.model == "bank-gpt4-prod"

    def test_symbolic_rung(self) -> None:
        att = attribute_model(None, frozenset({Symbolic(kind="env", key="LLM_MODEL")}))
        assert att.method == "attribution:config_symbolic"
        assert att.model == {"symbolic": "env:LLM_MODEL"}

    def test_wrapper_default_rung(self) -> None:
        att = attribute_model(
            None, frozenset({Symbolic(kind="wrapper_default", key="bank_ai.LLMClient")})
        )
        assert att.method == "attribution:wrapper_default"

    def test_unresolved_rung(self) -> None:
        att = attribute_model(None, frozenset({Top(reason="dynamic")}))
        assert att.method == "attribution:unresolved"
        assert att.model == {"unresolved": "dynamic"}
        assert not att.resolved

    def test_deployment_extraction(self) -> None:
        url = "https://r.openai.azure.com/openai/deployments/prod-gpt4/chat/completions?x=1"
        assert deployment_from_url(url) == "prod-gpt4"

    def test_host_of_template_needs_complete_host(self) -> None:
        complete = Template(parts=("https://gw.bank.internal/llm/",))
        assert host_of(frozenset({complete})) == "gw.bank.internal"
        truncated = Template(parts=("https://gw.bank",))
        assert host_of(frozenset({truncated})) is None


class TestSideEffects:
    def _classify(
        self, fixture: str, module_path: str, func: str, org: OrgPack | None = None
    ) -> SideEffectReport:
        repo = fixture_repo(fixture)
        parser = AstParser()
        modules: dict[str, ModuleIR] = {}
        for rel in discover_python_files(repo):
            mod = parser.parse(rel, (repo / rel).read_text(encoding="utf-8"))
            assert isinstance(mod, ModuleIR)
            modules[rel] = mod
        graph = ModuleGraph(modules)
        resolver = Resolver(
            graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats()
        )
        module_name = graph.name_by_path[module_path]
        fn = next(f for f in graph.by_name[module_name].defs if f.name == func)
        return classify_body(fn, module_name, resolver, org or OrgPack())

    def test_send_payment_is_external_send_with_credential(self) -> None:
        report = self._classify("bespoke_gateway_loop", "app/loop.py", "send_payment")
        assert report.effects == ("external_send",)
        assert report.external_target == "payments-core.internal"
        assert report.credential_ref == {"symbolic": "env:PAYMENTS_TOKEN"}
        assert report.http_verb == "POST"

    def test_lookup_account_is_read(self) -> None:
        report = self._classify("bespoke_gateway_loop", "app/loop.py", "lookup_account")
        assert report.effects == ("read",)

    def test_save_note_is_write(self) -> None:
        report = self._classify("bespoke_raw_openai_loop", "agent.py", "save_note")
        assert "write" in report.effects

    def test_restart_host_is_code_exec(self) -> None:
        report = self._classify("bespoke_wrapper", "app.py", "restart_host")
        assert "code_exec" in report.effects

    def test_open_ticket_is_external_send(self) -> None:
        report = self._classify("bespoke_wrapper", "app.py", "open_ticket")
        assert "external_send" in report.effects
        assert report.external_target == "itsm.internal.example"

    def test_extract_credential_from_template_hole(self) -> None:
        template = Template(parts=("Bearer ", Hole(expr="env:PAYMENTS_TOKEN")))
        headers = DictVal(entries=(("Authorization", frozenset({template})),))
        assert extract_credential(headers) == {"symbolic": "env:PAYMENTS_TOKEN"}


class TestPromptFromOpenMessages:
    def test_open_messages_list_still_binds_system(self) -> None:
        _, result = engine_for_repo(fixture_repo("bespoke_raw_openai_loop"))
        sink = sink_in(result, "agent.py")
        assert sink.prompt is not None
        assert sink.prompt.content == "You are a helpful weather assistant."


def _span() -> Span:
    return Span(file="x.py", line_start=1, line_end=1)
