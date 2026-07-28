"""Graph view (SPEC-3 §6): the ADG as deterministic inline SVG.

Layout is a pure function of the record: four columns (system, agents,
tools+MCP, models), nodes sorted by id, positions from sorted order. Scale
guards for the 1,703-agent degenerate case: location groups over
``_GROUP_CAP`` collapse into one aggregate node; a hard ``_NODE_CAP`` then
collapses whole location groups (test first, then example, then production
grouped by top-level directory). The legend always states "showing X of Y" —
no silent truncation; record.json remains the complete inventory.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field

from aiscan.inventory.schema import AgentRecord, Record

_GROUP_CAP = 20
_NODE_CAP = 150

_W = 200
_H = 34
_VGAP = 10
_COL_X = {"system": 10, "agent": 280, "component": 560, "model": 840}
_SVG_W = 1050

_FILL = {
    "system": "#1f3a5f",
    "agent": "#0b6b3a",
    "aggregate": "#5a6270",
    "tool": "#7a4b12",
    "mcp": "#5b3fbf",
    "model": "#8a1f1f",
}
_DASH = {"production": "", "example": "6,3", "test": "2,3"}


def _esc(value: object) -> str:
    return _html.escape(str(value), quote=True)


def _label(text: str, cap: int = 26) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


@dataclass(slots=True)
class _Node:
    node_id: str
    kind: str  # system | agent | aggregate | tool | mcp | model
    label: str
    sublabel: str = ""
    location: str = "production"
    href: str | None = None
    x: int = 0
    y: int = 0


@dataclass(slots=True)
class _Layout:
    nodes: dict[str, _Node] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # src, dst, kind
    total_agents: int = 0
    shown_agents: int = 0
    grouped: int = 0


def _model_key(agent: AgentRecord) -> str | None:
    if agent.model is None:
        return None
    return f"model:{agent.model.value!r}|{agent.model.endpoint!r}"


def _model_label(agent: AgentRecord) -> str:
    value = agent.model.value if agent.model is not None else None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for marker in ("symbolic", "unresolved", "template", "instance"):
            if marker in value:
                return f"{marker}: {value[marker]}"
    return "model"


def _build_layout(record: Record) -> _Layout:
    layout = _Layout()
    layout.total_agents = len(record.agents)

    layout.nodes["system"] = _Node("system", "system", _label(record.name), "system")

    # Decide which location groups collapse (SPEC-3 §6 scale guard).
    groups: dict[str, list[AgentRecord]] = {"production": [], "example": [], "test": []}
    for agent in record.agents:
        groups[agent.location].append(agent)
    collapsed = {loc for loc, members in groups.items() if len(members) > _GROUP_CAP}
    shown = sum(len(groups[loc]) for loc in groups if loc not in collapsed)
    for loc in ("test", "example", "production"):
        if shown <= _NODE_CAP or loc in collapsed:
            continue
        collapsed.add(loc)
        shown -= len(groups[loc])

    agent_node: dict[str, str] = {}  # agent_id -> node id representing it
    for loc in ("production", "example", "test"):
        members = groups[loc]
        if not members:
            continue
        if loc in collapsed:
            node_id = f"agg:{loc}"
            layout.nodes[node_id] = _Node(
                node_id,
                "aggregate",
                f"{loc} agents ({len(members)})",
                "grouped",
                location=loc,
            )
            layout.grouped += len(members)
            for agent in members:
                agent_node[agent.agent_id] = node_id
        else:
            for agent in sorted(members, key=lambda a: a.agent_id):
                node_id = f"agent:{agent.agent_id}"
                layout.nodes[node_id] = _Node(
                    node_id,
                    "agent",
                    _label(agent.agent_id),
                    agent.location,
                    location=loc,
                    href=f"#agent-{agent.agent_id}",
                )
                agent_node[agent.agent_id] = node_id
                layout.shown_agents += 1

    edge_seen: set[tuple[str, str, str]] = set()

    def add_edge(src: str, dst: str, kind: str) -> None:
        key = (src, dst, kind)
        if key not in edge_seen:
            edge_seen.add(key)
            layout.edges.append(key)

    tool_ids = {t.tool_id for t in record.tools}
    for agent in record.agents:
        src = agent_node[agent.agent_id]
        add_edge("system", src, "contains")
        for tool in agent.tools:
            if tool in tool_ids:
                dst = f"tool:{tool}"
                if dst not in layout.nodes:
                    layout.nodes[dst] = _Node(dst, "tool", _label(tool), "tool")
                add_edge(src, dst, "tool")
        for server in agent.mcp_servers:
            dst = f"mcp:{server}"
            if dst not in layout.nodes:
                layout.nodes[dst] = _Node(dst, "mcp", _label(server), "mcp")
            add_edge(src, dst, "mcp")
        model_key = _model_key(agent)
        if model_key is not None:
            if model_key not in layout.nodes:
                layout.nodes[model_key] = _Node(
                    model_key, "model", _label(_model_label(agent)), "model"
                )
            add_edge(src, model_key, "model")
        for target, kind in ((agent.handoffs, "handoff"), (agent.routes, "route")):
            for other in target:
                if other in agent_node and agent_node[other] != src:
                    add_edge(src, agent_node[other], kind)

    # Positions: sorted order within each column.
    def place(kinds: tuple[str, ...], x: int) -> int:
        ids = sorted(nid for nid, n in layout.nodes.items() if n.kind in kinds)
        for i, nid in enumerate(ids):
            layout.nodes[nid].x = x
            layout.nodes[nid].y = 10 + i * (_H + _VGAP)
        return len(ids)

    place(("system",), _COL_X["system"])
    n_agents = place(("agent", "aggregate"), _COL_X["agent"])
    n_comp = place(("tool", "mcp"), _COL_X["component"])
    n_models = place(("model",), _COL_X["model"])
    # Centre the system node vertically against the agent column.
    rows = max(n_agents, n_comp, n_models, 1)
    layout.nodes["system"].y = max(10, 10 + ((rows * (_H + _VGAP)) - _H) // 2 - _VGAP)
    return layout


def _edge_path(src: _Node, dst: _Node, kind: str) -> str:
    x1, y1 = src.x + _W, src.y + _H // 2
    x2, y2 = dst.x, dst.y + _H // 2
    if kind in ("handoff", "route"):
        # Same-column arc bulging right of the agent column.
        bulge = _W + 60
        return (
            f"M {x1} {y1} C {src.x + bulge} {y1}, {dst.x + bulge} {y2}, "
            f"{dst.x + _W} {y2}"
        )
    mid = (x1 + x2) // 2
    return f"M {x1} {y1} C {mid} {y1}, {mid} {y2}, {x2} {y2}"


def render_graph_svg(record: Record) -> str:
    """Render the graph section's SVG (empty string when nothing to draw)."""
    if not record.agents and not record.model_usages:
        return ""
    layout = _build_layout(record)

    rows = 0
    for node in layout.nodes.values():
        rows = max(rows, node.y + _H)
    height = rows + 20

    parts: list[str] = [
        f"<svg viewBox='0 0 {_SVG_W} {height}' width='100%' role='img' "
        "aria-label='Agent dependency graph' xmlns='http://www.w3.org/2000/svg'>",
        "<defs><marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' "
        "markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
        "<path d='M 0 0 L 10 5 L 0 10 z' fill='#5a6270'/></marker></defs>",
    ]

    for src_id, dst_id, kind in sorted(layout.edges):
        src, dst = layout.nodes[src_id], layout.nodes[dst_id]
        stroke = "#9aa2ad" if kind == "contains" else "#5a6270"
        marker = "" if kind == "contains" else " marker-end='url(#arr)'"
        dash = " stroke-dasharray='4,3'" if kind == "route" else ""
        parts.append(
            f"<path d='{_edge_path(src, dst, kind)}' fill='none' "
            f"stroke='{stroke}' stroke-width='1.2'{dash}{marker}/>"
        )
        if kind in ("handoff", "route"):
            label_x = src.x + _W + 64
            label_y = (src.y + dst.y) // 2 + _H // 2
            parts.append(
                f"<text x='{label_x}' y='{label_y}' font-size='10' fill='#5a6270'>"
                f"{_esc(kind)}</text>"
            )

    for node_id in sorted(layout.nodes):
        node = layout.nodes[node_id]
        dash = _DASH.get(node.location, "")
        dash_attr = f" stroke-dasharray='{dash}'" if dash else ""
        rect = (
            f"<rect x='{node.x}' y='{node.y}' width='{_W}' height='{_H}' rx='6' "
            f"fill='{_FILL[node.kind]}' stroke='#1a1d21'{dash_attr}/>"
        )
        text = (
            f"<text x='{node.x + 8}' y='{node.y + 15}' font-size='11' fill='#fff' "
            f"font-family='system-ui,sans-serif'>{_esc(node.label)}</text>"
            f"<text x='{node.x + 8}' y='{node.y + 28}' font-size='9' fill='#d5d9df' "
            f"font-family='system-ui,sans-serif'>{_esc(node.sublabel)}</text>"
        )
        if node.href is not None:
            parts.append(f"<a href='{_esc(node.href)}'>{rect}{text}</a>")
        else:
            parts.append(rect + text)

    parts.append("</svg>")

    legend = (
        f"<p class=meta>Showing {layout.shown_agents} of {layout.total_agents} agents"
        " individually"
        + (
            f"; {layout.grouped} grouped into location aggregate nodes"
            if layout.grouped
            else ""
        )
        + ". Border style: solid = production, dashed = example, dotted = test."
        # SPEC-7 Z10: only claim clickability when agent nodes are shown.
        + (" Click an agent to jump to its detail." if layout.shown_agents else "")
        + "</p>"
    )
    return legend + "<div style='overflow-x:auto'>" + "".join(parts) + "</div>"
