"""SPEC-6 §3: inventory derivations — canonical model display, usage
grouping, system type, data signals."""

from __future__ import annotations

from aiscan.derive.inventory import (
    EnvDefaults,
    build_models_used,
    canonical_model,
    data_signals_of,
    group_usages,
    system_type_of,
)
from aiscan.ingest.env_defaults import EnvDefault
from aiscan.inventory.schema import (
    AgentRecord,
    DerivedValue,
    DetectedValue,
    DetectionInfo,
    ModelUsage,
    StateEntry,
    ToolRecord,
)
from aiscan.ir.values import JsonRepr

_ENV: EnvDefaults = {
    "MODEL_FAST": (
        EnvDefault(value="gpt-4o-mini", source="infra/env.example", form="example"),
    )
}


def _usage(
    model: JsonRepr, task: str = "chat", ev: str = "a.ts:1-2", in_agent: bool = False
) -> ModelUsage:
    return ModelUsage(
        model=DetectedValue(value=model, evidence=(ev,)),
        task=task,  # type: ignore[arg-type]
        evidence=(ev,),
        in_agent=in_agent,
    )


class TestCanonicalModel:
    def test_plain_literal(self) -> None:
        assert canonical_model("gpt-4o", {}) == ("gpt-4o", ())

    def test_env_fallback_union(self) -> None:
        value: JsonRepr = {
            "union": [
                "gpt-4o-mini",
                {"symbolic": "env:MODEL_FAST"},
                {"unresolved": "unbound"},
            ]
        }
        display, quals = canonical_model(value, _ENV)
        assert display == "gpt-4o-mini"
        assert "code default" in quals
        assert "env-configurable: MODEL_FAST" in quals
        assert "declared gpt-4o-mini — infra/env.example" in quals
        assert "+unknown" in quals

    def test_all_symbolic_union(self) -> None:
        value: JsonRepr = {"union": [{"symbolic": "env:A"}, {"symbolic": "env:B"}]}
        display, _ = canonical_model(value, {})
        assert display == "set via env A, B"

    def test_wrapper_symbolic(self) -> None:
        display, _ = canonical_model(
            {"symbolic": "wrapper_default:apps/lib/x.getQueryEmbedding"}, {}
        )
        assert display == "via getQueryEmbedding (wrapper)"

    def test_unresolved(self) -> None:
        # SPEC-7 Z10: unknowns render in plain language, never scanner jargon.
        assert canonical_model({"unresolved": "dynamic"}, {}) == (
            "not determined",
            ("dynamic",),
        )


class TestModelsUsedAndGroups:
    def test_dedupe_by_display_and_task(self) -> None:
        union: JsonRepr = {"union": ["gpt-4o-mini", {"symbolic": "env:MODEL_FAST"}]}
        rows = build_models_used(
            [
                (union, None, "openai", "vendor_external", "chat", "production"),
                (union, None, "unknown", "vendor_external", "chat", "test"),
                ("gpt-4o-mini", None, "unknown", "vendor_external", "embedding", "test"),
            ],
            _ENV,
        )
        assert len(rows) == 2  # chat union merged; embedding row separate
        chat = next(r for r in rows if r["task"] == "chat")
        assert chat["display"] == "gpt-4o-mini"
        assert chat["api_style"] == "openai"  # non-unknown wins
        assert chat["locations"] == ["production", "test"]

    def test_empty_literal_never_wins_display(self) -> None:
        value: JsonRepr = {"union": ["", {"symbolic": "env:EXAMPLE_MODEL"}]}
        display, _quals = canonical_model(value, {})
        assert display == "set via env EXAMPLE_MODEL"

    def test_class_instance_short_name_dedupes(self) -> None:
        rows = build_models_used(
            [
                (
                    {"instance": "fake_model.FakeModel"},
                    None, "unknown", "vendor_external", "chat", "test",
                ),
                (
                    {"instance": "tests.fake_model.FakeModel"},
                    None, "unknown", "vendor_external", "chat", "test",
                ),
            ],
            {},
        )
        assert len(rows) == 1
        assert rows[0]["display"] == "FakeModel"

    def test_group_usages(self) -> None:
        usages = [
            _usage("gpt-4o", ev="a.ts:1-2"),
            _usage("gpt-4o", ev="a.ts:9-12", in_agent=True),
            _usage("gpt-4o", ev="b.ts:3-4"),
        ]
        groups = group_usages(usages, {})
        assert [(g["file"], g["call_sites"], g["in_agent"]) for g in groups] == [
            ("a.ts", 2, True),
            ("b.ts", 1, False),
        ]


def _agent(states: tuple[str, ...] = ()) -> AgentRecord:
    return AgentRecord(
        agent_id="a1",
        detection=DetectionInfo(method="bespoke", confidence="medium", evidence=("m.ts:1-2",)),
        state=tuple(StateEntry(kind=k) for k in states),  # type: ignore[arg-type]
    )


class TestSystemTypeAndSignals:
    def test_agentic_rag_batch(self) -> None:
        agents = [_agent(("vectorstore",)), _agent()]
        usages = [
            _usage("m", task="embedding"),
            _usage("m", ev="services/worker/src/jobs/gen.ts:1-2"),
        ]
        st = system_type_of(agents, usages)
        assert st["value"] == "agentic"
        assert st["components"] == ["multi-agent", "rag", "batch-llm"]

    def test_genai_only(self) -> None:
        st = system_type_of([], [_usage("m")])
        assert st["value"] == "genai"
        assert system_type_of([], [])["value"] == "none"

    def test_data_signals(self) -> None:
        tool = ToolRecord(
            tool_id="t1",
            kind="function",
            side_effects=("external_send",),
            external_target="sandbox.internal",
            evidence=("x.ts:1-2",),
            is_sensitive=DerivedValue(value=False),
        )
        signals = data_signals_of(
            [tool], [_usage("m", task="embedding")], [_agent(("vectorstore",))]
        )
        assert "sends data to sandbox.internal" in signals
        assert "indexes documents for semantic search (RAG)" in signals
