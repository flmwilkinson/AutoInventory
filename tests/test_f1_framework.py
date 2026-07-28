"""F1 framework-frontend units (P4): promotion behaviour, adversarial
non-match, and config-file rules."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.cli import discover_python_files
from aiscan.context import OrgPack, ResolverBudgets, ResolverStats
from aiscan.facts.models import (
    AgentDefF,
    BindModelF,
    MCPDefF,
    ModelRefF,
    PromptDefF,
    ToolDefF,
)
from aiscan.frontends.framework.engine import F1Result, FrameworkEngine, load_packs
from aiscan.ir.nodes import ModuleIR
from aiscan.modules.graph import ModuleGraph
from aiscan.parse.py_ast import AstParser
from aiscan.resolve.engine import Resolver
from aiscan.sinks.engine import SinkEngine
from tests.conftest import fixture_repo


def run_f1(repo: Path) -> F1Result:
    parser = AstParser()
    modules: dict[str, ModuleIR] = {}
    for rel in discover_python_files(repo):
        mod = parser.parse(rel, (repo / rel).read_text(encoding="utf-8"))
        assert isinstance(mod, ModuleIR)
        modules[rel] = mod
    graph = ModuleGraph(modules)
    resolver = Resolver(graph, graph.build_symbol_tables(), ResolverBudgets(), ResolverStats())
    sink_engine = SinkEngine(resolver, OrgPack(), repo_root=repo)
    sink_result = sink_engine.scan_all()
    engine = FrameworkEngine(
        resolver,
        sink_engine,
        load_packs(),
        llm_sink_spans=frozenset(s.span for s in sink_result.sinks),
        sinks=tuple(sink_result.sinks),
    )
    return engine.run()


class TestMonorepoReexport:
    """Scanning a framework's OWN source (openai-agents-python etc.): the
    public name resolves through the package re-export to its definition
    module, which the rule must still match (SPEC-1 §7.2 vendored/monorepo)."""

    def _write_monorepo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / "src" / "agents").mkdir(parents=True)
        (repo / "src" / "agents" / "__init__.py").write_text(
            "from agents.agent import Agent\n\n__all__ = ['Agent']\n", encoding="utf-8"
        )
        (repo / "src" / "agents" / "agent.py").write_text(
            "class Agent:\n"
            "    def __init__(self, name, instructions=None, tools=None):\n"
            "        self.name = name\n",
            encoding="utf-8",
        )
        (repo / "examples").mkdir()
        (repo / "examples" / "hello.py").write_text(
            "from agents import Agent\n\n"
            "agent = Agent(name='Assistant', instructions='You only respond in haikus.')\n",
            encoding="utf-8",
        )
        return repo

    def test_agent_detected_through_local_reexport(self, tmp_path: Path) -> None:
        result = run_f1(self._write_monorepo(tmp_path))
        agents = [f for f in result.facts if isinstance(f, AgentDefF)]
        assert [a.name for a in agents] == ["Assistant"]
        assert agents[0].framework == "openai-agents"

    def test_agents_tagged_by_location(self, tmp_path: Path) -> None:
        repo = self._write_monorepo(tmp_path)  # example agent under examples/
        (repo / "tests").mkdir()
        (repo / "tests" / "test_something.py").write_text(
            "from agents import Agent\n\n"
            "def test_it():\n    a = Agent(name='TestFixture')\n    assert a\n",
            encoding="utf-8",
        )
        (repo / "app.py").write_text(
            "from agents import Agent\n\nprod = Agent(name='Prod')\n", encoding="utf-8"
        )
        result = run_f1(repo)
        loc = {f.name: f.location for f in result.facts if isinstance(f, AgentDefF)}
        # Nothing dropped — every agent kept, tagged by where it lives.
        assert loc == {"Assistant": "example", "TestFixture": "test", "Prod": "production"}

    def test_mcp_detected_through_lazy_reexport(self, tmp_path: Path) -> None:
        """openai-agents-python exposes MCP classes via a PEP 562 lazy
        ``__getattr__`` with the real imports under ``if TYPE_CHECKING:`` —
        the call site resolves to the definition fq, which must match the
        rule like the public path does (the _MCP_CLASSES gate regression)."""
        repo = self._write_monorepo(tmp_path)
        mcp_dir = repo / "src" / "agents" / "mcp"
        mcp_dir.mkdir()
        (mcp_dir / "__init__.py").write_text(
            "from typing import TYPE_CHECKING, Any\n"
            "from importlib import import_module\n\n"
            "if TYPE_CHECKING:\n"
            "    from .server import MCPServerStdio\n\n"
            '_LAZY_EXPORTS = {"MCPServerStdio": ".server"}\n\n\n'
            "def __getattr__(name: str) -> Any:\n"
            "    return getattr(import_module(_LAZY_EXPORTS[name], __name__), name)\n",
            encoding="utf-8",
        )
        (mcp_dir / "server.py").write_text(
            "class MCPServerStdio:\n"
            "    def __init__(self, params, name=None):\n"
            "        self.params = params\n",
            encoding="utf-8",
        )
        (repo / "examples" / "use_mcp.py").write_text(
            "from agents.mcp import MCPServerStdio\n\n"
            'server = MCPServerStdio(params={"command": "npx"})\n',
            encoding="utf-8",
        )
        result = run_f1(repo)
        mcps = [f for f in result.facts if isinstance(f, MCPDefF)]
        assert [(m.server, m.transport) for m in mcps] == [("npx", "stdio")]

    def test_realtime_agent_with_constant_default_model(self, tmp_path: Path) -> None:
        """RealtimeAgent counts as an agent (the Z9 pack gap), and a
        ``default_ref`` naming a module-level constant resolves like a
        zero-arg function default."""
        repo = self._write_monorepo(tmp_path)
        rt_dir = repo / "src" / "agents" / "realtime"
        rt_dir.mkdir()
        (rt_dir / "__init__.py").write_text(
            "from .agent import RealtimeAgent\n", encoding="utf-8"
        )
        (rt_dir / "agent.py").write_text(
            "class RealtimeAgent:\n"
            "    def __init__(self, name, instructions=None, tools=None,\n"
            "                 handoffs=None, mcp_servers=None):\n"
            "        self.name = name\n",
            encoding="utf-8",
        )
        (rt_dir / "openai_realtime.py").write_text(
            'DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"\n', encoding="utf-8"
        )
        (repo / "examples" / "voice_demo.py").write_text(
            "from agents.realtime import RealtimeAgent\n\n"
            "agent = RealtimeAgent(name='Voice', instructions='Talk to the caller.')\n",
            encoding="utf-8",
        )
        result = run_f1(repo)
        agents = {f.name: f for f in result.facts if isinstance(f, AgentDefF)}
        assert "Voice" in agents
        assert agents["Voice"].framework == "openai-agents"
        models = {f.id: f for f in result.facts if isinstance(f, ModelRefF)}
        bound = [
            models[b.model_id].model
            for b in result.facts
            if isinstance(b, BindModelF) and b.agent_id == agents["Voice"].id
        ]
        assert bound == ["gpt-realtime-2.1"]


class TestPromotion:
    def test_only_sink_bearing_node_promoted(self) -> None:
        result = run_f1(fixture_repo("fw_langgraph_nodes"))
        agents = [f for f in result.facts if isinstance(f, AgentDefF)]
        assert [a.name for a in agents] == ["plan"]
        assert "promotion:sink" in agents[0].method
        assert result.unpromoted_candidates == 2


class TestAdversarialAgentName:
    def test_user_agent_class_never_matches(self) -> None:
        result = run_f1(fixture_repo("adversarial_agent_name"))
        assert not [f for f in result.facts if isinstance(f, AgentDefF)]
        assert result.unpromoted_candidates == 0


class TestConfigFileRules:
    def _run_on_tree(self, tmp_path: Path, files: dict[str, str]) -> F1Result:
        for rel, content in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return run_f1(tmp_path)

    def test_mcp_json(self, tmp_path: Path) -> None:
        cfg = {
            "mcpServers": {
                "docs": {"url": "https://mcp.example.com/sse"},
                "runner": {"command": "uvx", "args": ["mcp-runner"], "tools": ["run", "stop"]},
            }
        }
        result = self._run_on_tree(
            tmp_path, {"app.py": "x = 1\n", ".mcp.json": json.dumps(cfg)}
        )
        mcps = {f.id: f for f in result.facts if isinstance(f, MCPDefF)}
        assert mcps["mcp:docs"].transport == "sse"
        assert mcps["mcp:docs"].server == "https://mcp.example.com/sse"
        assert mcps["mcp:runner"].transport == "stdio"
        assert mcps["mcp:runner"].server == "uvx mcp-runner"
        assert mcps["mcp:runner"].declared_tools == ("run", "stop")

    def test_prompt_file_candidate(self, tmp_path: Path) -> None:
        result = self._run_on_tree(
            tmp_path,
            {"app.py": "x = 1\n", "prompts/system.md": "You are a careful assistant.\n"},
        )
        prompts = [f for f in result.facts if isinstance(f, PromptDefF)]
        assert len(prompts) == 1
        assert prompts[0].origin == "file"
        assert prompts[0].file_ref == "prompts/system.md"
        assert prompts[0].content_hash is not None

    def test_skill_md(self, tmp_path: Path) -> None:
        skill = "---\nname: deploy-service\ndescription: Deploys the service\n---\n\nBody.\n"
        result = self._run_on_tree(tmp_path, {"app.py": "x = 1\n", "SKILL.md": skill})
        tools = [f for f in result.facts if isinstance(f, ToolDefF)]
        assert len(tools) == 1
        assert tools[0].kind == "schema_declared"
        assert tools[0].name == "deploy-service"
