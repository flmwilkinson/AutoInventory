"""Canonical serialisation (SPEC §6.8): nodes and edges sorted by stable id,
``sort_keys=True``, no timestamps anywhere in graph.json."""

from __future__ import annotations

import json

from aiscan.graph.model import Graph


def canonical_json(data: object) -> str:
    """Deterministic JSON used for every artifact."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def graph_to_dict(graph: Graph) -> dict[str, object]:
    nodes = [
        {
            "id": nid,
            "type": graph.node_types[nid],
            "fact_type": type(graph.nodes[nid]).__name__,
            "fact": graph.nodes[nid].model_dump(mode="json"),
        }
        for nid in sorted(graph.nodes)
    ]
    edges = [
        {
            "family": e.family,
            "type": e.type,
            "src": e.src,
            "dst": e.dst,
            "attrs": dict(e.attrs),
            "evidence": list(e.evidence),
        }
        for e in sorted(graph.edges, key=lambda e: (e.family, e.type, e.src, e.dst, e.attrs))
    ]
    return {"nodes": nodes, "edges": edges}


def graph_canonical_json(graph: Graph) -> str:
    return canonical_json(graph_to_dict(graph))
