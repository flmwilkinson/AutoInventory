"""Framework-pack detection for the added ecosystems (SPEC-8 breadth):
PydanticAI, LlamaIndex, AutoGen (v0.4 + v0.2), Semantic Kernel.

Each case is consumer-style (the framework is third-party, so its public import
path matches the rule directly). We assert the agent, its model, and — where the
framework exposes them at the call site — its tools/handoffs are detected. These
packs are an additive precision layer over the framework-agnostic bespoke
frontend; here we check the precise path.
"""

from __future__ import annotations

from pathlib import Path

from aiscan.facts.models import AgentDefF, BindModelF, ModelRefF, ToolDefF
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


def test_pydantic_ai(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from pydantic_ai import Agent\n\n"
                "agent = Agent('openai:gpt-4o', system_prompt='Be helpful.')\n\n\n"
                "@agent.tool_plain\n"
                "def roll() -> int:\n"
                "    return 4\n"
            )
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "agent" in agents
    assert _model_of(result, agents["agent"]) == "openai:gpt-4o"
    assert any(isinstance(f, ToolDefF) and f.name == "roll" for f in result.facts)


def test_llama_index(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from llama_index.core.agent.workflow import FunctionAgent\n"
                "from llama_index.llms.openai import OpenAI\n\n"
                "def search(q: str) -> str:\n"
                "    return q\n\n"
                "planner = FunctionAgent(\n"
                "    llm=OpenAI(model='gpt-4o-mini'),\n"
                "    tools=[search],\n"
                "    system_prompt='Plan.',\n"
                ")\n"
            )
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "planner" in agents
    assert _model_of(result, agents["planner"]) == "gpt-4o-mini"


def test_autogen_v04(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from autogen_agentchat.agents import AssistantAgent\n"
                "from autogen_ext.models.openai import OpenAIChatCompletionClient\n\n"
                "def get_time() -> str:\n"
                "    return 'now'\n\n"
                "client = OpenAIChatCompletionClient(model='gpt-4o')\n"
                "weather = AssistantAgent(\n"
                "    name='weather_agent',\n"
                "    model_client=client,\n"
                "    tools=[get_time],\n"
                "    system_message='Report the weather.',\n"
                ")\n"
            )
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "weather_agent" in agents
    assert _model_of(result, agents["weather_agent"]) == "gpt-4o"


def test_autogen_v04_positional(tmp_path: Path) -> None:
    # Quickstart style: name and model_client both positional.
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from autogen_agentchat.agents import AssistantAgent\n"
                "from autogen_ext.models.openai import OpenAIChatCompletionClient\n\n"
                "agent = AssistantAgent('assistant', OpenAIChatCompletionClient(model='gpt-4o'))\n"
            )
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "assistant" in agents
    assert _model_of(result, agents["assistant"]) == "gpt-4o"


def test_autogen_v02_legacy(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from autogen import AssistantAgent\n\n"
                "assistant = AssistantAgent(\n"
                "    name='helper',\n"
                "    system_message='You help.',\n"
                "    llm_config={'model': 'gpt-4'},\n"
                ")\n"
            )
        },
    )
    result = run_f1(repo)
    assert "helper" in _agents(result)


def test_semantic_kernel(tmp_path: Path) -> None:
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from semantic_kernel.agents import ChatCompletionAgent\n"
                "from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion\n\n"
                "agent = ChatCompletionAgent(\n"
                "    service=OpenAIChatCompletion(ai_model_id='gpt-4o'),\n"
                "    name='Triage',\n"
                "    instructions='Route requests.',\n"
                ")\n"
            )
        },
    )
    result = run_f1(repo)
    agents = _agents(result)
    assert "Triage" in agents
    assert _model_of(result, agents["Triage"]) == "gpt-4o"


def test_autogen_multi_agent(tmp_path: Path) -> None:
    # A Swarm-style pair with name-based handoffs. Both agents are inventoried;
    # instance-based handoffs emit a Transfer, name-string handoffs (this case)
    # detect the agents but not yet the edge — a documented follow-up.
    repo = _write(
        tmp_path,
        {
            "app.py": (
                "from autogen_agentchat.agents import AssistantAgent\n"
                "from autogen_ext.models.openai import OpenAIChatCompletionClient\n\n"
                "client = OpenAIChatCompletionClient(model='gpt-4o')\n"
                "a = AssistantAgent(name='alpha', model_client=client, handoffs=['beta'])\n"
                "b = AssistantAgent(name='beta', model_client=client)\n"
            )
        },
    )
    agents = _agents(run_f1(repo))
    assert {"alpha", "beta"} <= set(agents)
