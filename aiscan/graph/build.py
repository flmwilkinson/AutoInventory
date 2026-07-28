"""Facts → ADG assembly with merge/dedup (SPEC §6.8).

Two frontends finding the same entity produce one node: evidence unions,
confidence maxes, ``method`` lists both. Downstream never knows which frontend
found what.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiscan.facts.models import (
    AnyFact,
    AttachPolicyF,
    BindMCPF,
    BindModelF,
    BindPromptF,
    BindStateF,
    BindToolF,
    InterAgentMsgF,
    LLMCallSiteF,
    TransferF,
    max_confidence,
)
from aiscan.graph.model import Edge, Graph, node_type_of


@dataclass(slots=True)
class BuildStats:
    merged_nodes: int = 0
    dropped_edges: int = 0
    merged_edges: int = 0


@dataclass(slots=True)
class BuildResult:
    graph: Graph
    stats: BuildStats = field(default_factory=BuildStats)


def _merge_fact(existing: AnyFact, new: AnyFact) -> AnyFact:
    methods = sorted(set(existing.method.split("+")) | set(new.method.split("+")))
    return existing.model_copy(
        update={
            "evidence": tuple(sorted(set(existing.evidence) | set(new.evidence))),
            "confidence": max_confidence(existing.confidence, new.confidence),
            "method": "+".join(methods),
            "source_files": tuple(sorted(set(existing.source_files) | set(new.source_files))),
        }
    )


def build_graph(facts: list[AnyFact]) -> BuildResult:
    """Assemble the typed graph; deterministic given fact list content."""
    result = BuildResult(graph=Graph())
    graph = result.graph

    entity_facts = [f for f in facts if node_type_of(f) is not None]
    for fact in sorted(entity_facts, key=lambda f: (f.id, f.method)):
        node_type = node_type_of(fact)
        assert node_type is not None
        if fact.id in graph.nodes:
            graph.nodes[fact.id] = _merge_fact(graph.nodes[fact.id], fact)
            result.stats.merged_nodes += 1
        else:
            graph.nodes[fact.id] = fact
            graph.node_types[fact.id] = node_type

    edge_index: dict[tuple[str, str, str, str, tuple[tuple[str, str], ...]], Edge] = {}

    def add_edge(edge: Edge) -> None:
        if edge.src not in graph.nodes or edge.dst not in graph.nodes:
            result.stats.dropped_edges += 1
            return
        key = (edge.family, edge.type, edge.src, edge.dst, edge.attrs)
        if key in edge_index:
            merged = Edge(
                family=edge.family,
                type=edge.type,
                src=edge.src,
                dst=edge.dst,
                attrs=edge.attrs,
                evidence=tuple(sorted(set(edge_index[key].evidence) | set(edge.evidence))),
            )
            edge_index[key] = merged
            result.stats.merged_edges += 1
        else:
            edge_index[key] = edge

    binding_facts = [f for f in facts if node_type_of(f) is None]
    for fact in sorted(binding_facts, key=lambda f: (f.id, f.method)):
        match fact:
            case BindModelF(agent_id=a, model_id=m):
                add_edge(Edge("ACDG", "bind_model", a, m, evidence=fact.evidence))
            case BindPromptF(agent_id=a, prompt_id=p):
                add_edge(Edge("ACDG", "bind_prompt", a, p, evidence=fact.evidence))
            case BindToolF(agent_id=a, tool_id=t):
                add_edge(Edge("ACDG", "bind_tool", a, t, evidence=fact.evidence))
            case BindMCPF(agent_id=a, mcp_id=m):
                add_edge(Edge("ACDG", "bind_mcp", a, m, evidence=fact.evidence))
            case BindStateF(agent_id=a, state_id=s):
                add_edge(Edge("ACDG", "bind_state", a, s, evidence=fact.evidence))
            case AttachPolicyF(policy_id=p, target_id=t):
                add_edge(Edge("ACDG", "attach_policy", p, t, evidence=fact.evidence))
            case TransferF(from_id=f_, to_id=t, kind=kind, guard=guard):
                attrs = (("guard", guard),) if guard else ()
                add_edge(
                    Edge("ACFG", f"transfer:{kind}", f_, t, attrs=attrs, evidence=fact.evidence)
                )
            case InterAgentMsgF(from_id=f_, to_id=t, via=via):
                attrs = (("via", via),) if via else ()
                add_edge(
                    Edge("ADFG", "inter_agent_msg", f_, t, attrs=attrs, evidence=fact.evidence)
                )
            case _:
                continue

    for node_id in graph.nodes_of_type("L"):
        call_site = graph.nodes[node_id]
        if isinstance(call_site, LLMCallSiteF):
            add_edge(
                Edge("ACDG", "uses_model", node_id, call_site.model_id, evidence=call_site.evidence)
            )
            if call_site.prompt_id is not None:
                add_edge(
                    Edge(
                        "ACDG",
                        "uses_prompt",
                        node_id,
                        call_site.prompt_id,
                        evidence=call_site.evidence,
                    )
                )

    graph.edges = sorted(
        edge_index.values(), key=lambda e: (e.family, e.type, e.src, e.dst, e.attrs)
    )
    return result
