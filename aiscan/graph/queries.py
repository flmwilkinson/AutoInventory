"""BOM queries over the ADG (SPEC §5.3): the per-agent BOM is a query."""

from __future__ import annotations

from dataclasses import dataclass

from aiscan.facts.models import MCPDefF, ToolDefF
from aiscan.graph.model import Edge, Graph


@dataclass(frozen=True, slots=True)
class AgentBom:
    agent_id: str
    model_ids: tuple[str, ...]
    prompt_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    mcp_ids: tuple[str, ...]
    state_ids: tuple[str, ...]
    policy_ids: tuple[str, ...]
    transfers_out: tuple[Edge, ...]


@dataclass(frozen=True, slots=True)
class BundleBom:
    agents: tuple[AgentBom, ...]
    orphan_call_site_ids: tuple[str, ...]
    unbound_mcp_ids: tuple[str, ...]
    unbound_prompt_ids: tuple[str, ...]
    unbound_tool_ids: tuple[str, ...] = ()


def agent_bom(graph: Graph, agent_id: str) -> AgentBom:
    """ACDG neighbours of one agent, grouped by node type."""
    tools: list[str] = []
    mcps: list[str] = []
    for nid in graph.neighbors(agent_id, family="ACDG", type="bind_tool", direction="out"):
        (tools if isinstance(graph.nodes[nid], ToolDefF) else mcps).append(nid)
    for nid in graph.neighbors(agent_id, family="ACDG", type="bind_mcp", direction="out"):
        if nid not in mcps:
            mcps.append(nid)
    policies = graph.neighbors(agent_id, family="ACDG", type="attach_policy", direction="in")
    transfers = tuple(
        e
        for e in sorted(
            graph.edges_from(agent_id, family="ACFG"),
            key=lambda e: (e.type, e.dst),
        )
        if e.type.startswith("transfer:")
    )
    return AgentBom(
        agent_id=agent_id,
        model_ids=tuple(
            graph.neighbors(agent_id, family="ACDG", type="bind_model", direction="out")
        ),
        prompt_ids=tuple(
            graph.neighbors(agent_id, family="ACDG", type="bind_prompt", direction="out")
        ),
        tool_ids=tuple(sorted(tools)),
        mcp_ids=tuple(sorted(mcps)),
        state_ids=tuple(
            graph.neighbors(agent_id, family="ACDG", type="bind_state", direction="out")
        ),
        policy_ids=tuple(sorted(policies)),
        transfers_out=transfers,
    )


def bundle_bom(graph: Graph) -> BundleBom:
    """Every agent's BOM plus orphan call sites and unbound artefacts."""
    agents = tuple(agent_bom(graph, aid) for aid in graph.nodes_of_type("A"))
    bound: set[str] = set()
    for bom in agents:
        bound.update(bom.model_ids)
        bound.update(bom.prompt_ids)
        bound.update(bom.tool_ids)
        bound.update(bom.mcp_ids)
        bound.update(bom.state_ids)
        bound.update(bom.policy_ids)
    call_sites = graph.nodes_of_type("L")
    for cid in call_sites:
        bound.update(graph.neighbors(cid, family="ACDG", direction="out"))
    unbound_mcp = [
        nid
        for nid in graph.nodes_of_type("C")
        if isinstance(graph.nodes[nid], MCPDefF) and nid not in bound
    ]
    unbound_prompts = [nid for nid in graph.nodes_of_type("I") if nid not in bound]
    unbound_tools = [
        nid
        for nid in graph.nodes_of_type("C")
        if isinstance(graph.nodes[nid], ToolDefF) and nid not in bound
    ]
    return BundleBom(
        agents=agents,
        orphan_call_site_ids=tuple(call_sites),
        unbound_mcp_ids=tuple(sorted(unbound_mcp)),
        unbound_prompt_ids=tuple(sorted(unbound_prompts)),
        unbound_tool_ids=tuple(sorted(unbound_tools)),
    )
