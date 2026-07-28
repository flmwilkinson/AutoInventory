"""SPEC-9 A2 — the agnostic resolved-model fallback.

Detects a framework agent when a model *resolves* and an agent-shaped companion
(tools/instructions) is present, on a call site no pack claimed. The class name
is never a gate: this both survives framework renames/moves/new-frameworks and
keeps non-AI ``*Agent`` classes out.
"""

from __future__ import annotations

from pathlib import Path

from aiscan.facts.models import AgentDefF, BindModelF, ModelRefF
from tests.test_f1_framework import run_f1


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return repo


def _agents(result) -> dict[str, AgentDefF]:
    return {f.name: f for f in result.facts if isinstance(f, AgentDefF)}


def _model_of(result, agent: AgentDefF):
    models = {f.id: f for f in result.facts if isinstance(f, ModelRefF)}
    for b in result.facts:
        if isinstance(b, BindModelF) and b.agent_id == agent.id:
            return models[b.model_id].model
    return None


# --- resilience: the whole point ---


def test_renamed_and_moved_framework_still_detected(tmp_path: Path) -> None:
    """Simulate a framework update that renamed the agent class, moved its
    module, AND renamed the model kwarg to a novel name — with NO pack. A2 must
    still detect the agent (model found by value) at medium confidence."""
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\nname='demo'\nversion='0.1'\ndependencies=['someframework']\n"
                "# openai import gives triage an AI signal:\n"
            ),
            "app.py": (
                "from openai import OpenAI  # triage signal\n"
                "from someframework.brandnew import ChatWorker, LlmClient\n\n"
                "worker = ChatWorker(\n"
                "    name='Researcher',\n"
                "    engine=LlmClient(model='gpt-4o'),\n"  # novel model kwarg name
                "    instructions='Research things.',\n"
                ")\n"
            ),
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "worker" in agents or "Researcher" in agents
    agent = agents.get("worker") or agents["Researcher"]
    assert agent.confidence == "medium"
    assert agent.method == "agent_shape:ctor"
    assert _model_of(result, agent) == "gpt-4o"


def test_unknown_framework_with_model_named_kwarg(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='d'\nversion='0.1'\ndependencies=['x']\n",
            "app.py": (
                "from openai import OpenAI\n"
                "from novelagents import Assistant\n\n"
                "a = Assistant(model='anthropic:claude-sonnet', instructions='Help.')\n"
            ),
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "a" in agents
    assert agents["a"].confidence == "medium"


# --- adversarial: must NOT detect ---


def test_local_agent_class_not_detected(tmp_path: Path) -> None:
    """A local class named Agent with no resolvable model — the adversarial
    guard. Companion kwarg (schedule) is not agent-shaped; no model resolves."""
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='d'\nversion='0.1'\ndependencies=['openai']\n",
            "sched.py": (
                "from openai import OpenAI  # triage signal only\n\n"
                "class Agent:\n"
                "    def __init__(self, name, schedule=None):\n"
                "        self.name = name\n\n"
                "def nightly():\n    return 'ok'\n\n"
                "agent = Agent('batch', schedule=[nightly])\n"
            ),
        },
    )
    assert _agents(run_f1(repo)) == {}


def test_non_ai_agent_named_class_with_model_kwarg_not_detected(tmp_path: Path) -> None:
    """The false-positive trap: a `*Agent` name + a kwarg literally named
    `model` (a schema class, not an LLM) + a `*Client` instance + a tools list.
    Name + companion present, but the model does NOT resolve → no agent."""
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='d'\nversion='0.1'\ndependencies=['openai']\n",
            "report.py": (
                "from openai import OpenAI  # triage signal only\n\n"
                "class SchemaV2:\n    pass\n\n"
                "class S3Client:\n    pass\n\n"
                "def handler():\n    return 1\n\n"
                "class ReportAgent:\n"
                "    def __init__(self, name, model=None, tools=None, client=None):\n"
                "        self.name = name\n\n"
                "report = ReportAgent(\n"
                "    name='R', model=SchemaV2, tools=[handler], client=S3Client()\n"
                ")\n"
            ),
        },
    )
    assert _agents(run_f1(repo)) == {}


def test_bare_model_call_is_not_an_agent(tmp_path: Path) -> None:
    """A model construction with no agent-shaped companion is a model/sink, not
    an agent — the second gate."""
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='d'\nversion='0.1'\ndependencies=['x']\n",
            "app.py": (
                "from openai import OpenAI\n"
                "from someframework import ModelHolder, LlmClient\n\n"
                "h = ModelHolder(model_client=LlmClient(model='gpt-4o'))\n"  # no tools/instructions
            ),
        },
    )
    assert _agents(run_f1(repo)) == {}


def test_pack_wins_over_fallback(tmp_path: Path) -> None:
    """A pack-detected agent is high confidence; the fallback must not also fire
    on the same site (double-emit)."""
    repo = _write(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='d'\nversion='0.1'\ndependencies=['pydantic-ai']\n",
            "app.py": (
                "from pydantic_ai import Agent\n\n"
                "agent = Agent('openai:gpt-4o', system_prompt='Be helpful.')\n"
            ),
        },
    )
    agents = _agents(run_f1(repo))
    assert "agent" in agents
    assert agents["agent"].confidence == "high"  # pack, not the medium fallback
    assert agents["agent"].method != "agent_shape:ctor"
