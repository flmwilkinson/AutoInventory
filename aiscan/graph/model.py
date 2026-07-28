"""Typed ADG (SPEC §5.3).

Node types: A(gent) I(nstruction/prompt) M(odel) C(omponent: tool/MCP)
S(tate) G(uard/policy) L(LM call site). Edge families: ACDG (bindings),
ACFG (control), ADFG (data: inter-agent messages).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from aiscan.facts.models import (
    AgentDefF,
    AnyFact,
    LLMCallSiteF,
    MCPDefF,
    ModelRefF,
    PolicyDefF,
    PromptDefF,
    StateDefF,
    ToolDefF,
)

NodeType = Literal["A", "I", "M", "C", "S", "G", "L"]
EdgeFamily = Literal["ACDG", "ACFG", "ADFG"]


def node_type_of(fact: AnyFact) -> NodeType | None:
    match fact:
        case AgentDefF():
            return "A"
        case PromptDefF():
            return "I"
        case ModelRefF():
            return "M"
        case ToolDefF() | MCPDefF():
            return "C"
        case StateDefF():
            return "S"
        case PolicyDefF():
            return "G"
        case LLMCallSiteF():
            return "L"
        case _:
            return None


@dataclass(frozen=True, slots=True)
class Edge:
    family: EdgeFamily
    type: str
    src: str
    dst: str
    attrs: tuple[tuple[str, str], ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(slots=True)
class Graph:
    """Small typed property graph; queries are neighbour lookups."""

    nodes: dict[str, AnyFact] = field(default_factory=dict)
    node_types: dict[str, NodeType] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def neighbors(
        self,
        node_id: str,
        family: EdgeFamily | None = None,
        type: str | None = None,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[str]:
        out: list[str] = []
        for e in self.edges:
            if family is not None and e.family != family:
                continue
            if type is not None and e.type != type:
                continue
            if direction in ("out", "both") and e.src == node_id:
                out.append(e.dst)
            if direction in ("in", "both") and e.dst == node_id:
                out.append(e.src)
        return sorted(set(out))

    def nodes_of_type(self, node_type: NodeType) -> list[str]:
        return sorted(nid for nid, t in self.node_types.items() if t == node_type)

    def edges_from(
        self, node_id: str, family: EdgeFamily | None = None, type: str | None = None
    ) -> list[Edge]:
        return [
            e
            for e in self.edges
            if e.src == node_id
            and (family is None or e.family == family)
            and (type is None or e.type == type)
        ]
