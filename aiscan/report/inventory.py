"""``inventory.html`` renderer (SPEC-6) — the AI inventory record.

This module owns the shared rendering primitives and the **record body**:
the plain-English inventory a bank reviewer reads. ``report.html`` embeds the
identical body and appends the technical annex (``aiscan.report.html`` imports
from here, never the reverse — the two artifacts cannot drift apart).

Language rule (SPEC-6 §4): the record body carries no scanner jargon — values
render through the canonical-model display and phrase maps; absence renders as
"none detected in analysed code" (D∅), never "none".
"""

from __future__ import annotations

import html as _html
from collections.abc import Callable
from typing import Any

from aiscan.derive.inventory import canonical_model
from aiscan.inventory.schema import (
    AgentRecord,
    EnrichedSlot,
    Record,
    ToolRecord,
)
from aiscan.ir.values import JsonRepr
from aiscan.parse.registry import PIPELINE_EXTS

PROMPT_CAP = 2000

# SPEC_INVENTORY: liveness is a confidence tier (emphasis), never proof of runtime
# execution — the labels say so, and "defined" is never phrased as "dormant".
_LIVENESS_LABEL = {
    "invoked": "invoked — an invocation site (e.g. a run call) was detected",
    "reachable": "reachable — handed off to from an invoked agent",
    "defined": "defined in code — not confirmed reachable from an entrypoint",
}

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; img-src data:"
)

CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1a1d21}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.4rem;margin:.2rem 0}
h2{font-size:1.05rem;margin:1.6rem 0 .5rem;border-bottom:1px solid #d9dde3;padding-bottom:.25rem}
.meta{color:#5a6270;font-size:.85rem}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.8rem;
font-weight:600;vertical-align:middle}
.badge.ai_detected{background:#0b6b3a;color:#fff}
.badge.ai_signals_only{background:#b7791f;color:#fff}
.badge.no_ai{background:#5a6270;color:#fff}
.badge.draft{background:#5b3fbf;color:#fff;font-size:.7rem;margin-left:6px}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:#e4e7ec;
font-size:.78rem;margin:1px 2px}
.chip.on{background:#c62828;color:#fff}
.chip.soft{background:#dde7f3}
.sev{font-weight:700;font-size:.75rem;padding:1px 8px;border-radius:10px}
.sev.high{background:#c62828;color:#fff}.sev.medium{background:#e08c00;color:#fff}.sev.low{background:#946200;color:#fff}.sev.info{background:#5a6270;color:#fff}
table{border-collapse:collapse;width:100%;font-size:.85rem;background:#fff}
th,td{border:1px solid #d9dde3;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#eceff3}
details{background:#fff;border:1px solid #d9dde3;border-radius:6px;margin:6px 0;padding:0}
details>summary{cursor:pointer;padding:8px 12px;font-weight:600}
details>div{padding:4px 14px 12px;border-top:1px solid #eceff3}
dl{display:grid;grid-template-columns:220px 1fr;gap:2px 12px;font-size:.85rem;margin:.4rem 0}
dt{color:#5a6270}dd{margin:0;overflow-wrap:anywhere}
pre{background:#f0f2f5;padding:8px;border-radius:4px;font-size:.78rem;white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto}
.note{background:#fff8e6;border:1px solid #e5cf8c;border-radius:6px;padding:8px 12px;
font-size:.85rem}
.locgroup>summary{background:#eceff3}
footer{margin-top:36px;color:#5a6270;font-size:.8rem}
""".strip()

_EFFECT_PHRASES = {
    "read": "reads external data",
    "write": "writes files",
    "external_send": "sends data externally",
    "code_exec": "executes code",
    "admin_mutation": "modifies accounts or permissions",
}

_FLAG_PHRASES = {
    "sends_external": "sends data to external services",
    "executes_code": "executes code",
    "moves_money": "can move money",
    "mutates_identities": "modifies identities or permissions",
    "reads_sensitive": "reads sensitive data",
}

_STATE_PHRASES = {
    "messages": "conversation history (in-session)",
    "session": "session store",
    "vectorstore": "vector store (semantic index)",
    "shared_object": "shared state object",
}

_PROVIDER_PHRASES = {
    "vendor_external": "external vendor",
    "internal_gateway": "internal gateway",
    "self_hosted": "self-hosted",
    "unknown": "unknown",
}

# api_style names a wire dialect, never a vendor — an OpenAI-compatible API
# may be served by OpenRouter, Ollama, litellm or an internal gateway.
_API_PROVIDERS = {
    "openai": "OpenAI-compatible API",
    "anthropic": "Anthropic-compatible API",
    "bedrock": "AWS Bedrock API",
    "vertex": "Google Vertex API",
}

_TASK_PHRASES = {
    "chat": "chat / generation",
    "completion": "completion",
    "embedding": "embeddings",
    "stt": "speech-to-text",
    "tts": "text-to-speech",
}


# -- shared primitives (imported by report.html) ------------------------------


def esc(value: object) -> str:
    return _html.escape(str(value), quote=True)


def val(value: JsonRepr) -> str:
    """Machine-value rendering (annex-grade) — escaped."""
    if value is None:
        return "&mdash;"
    if isinstance(value, str):
        return esc(value)
    if isinstance(value, dict):
        if "symbolic" in value:
            return f"<code>{esc(value['symbolic'])}</code> <span class=meta>(symbolic)</span>"
        if "unresolved" in value:
            return f"<span class=meta>unresolved: {esc(value['unresolved'])}</span>"
        if "template" in value:
            return f"<code>{esc(value['template'])}</code> <span class=meta>(template)</span>"
        if isinstance(value.get("union"), list):
            members = value["union"]
            assert isinstance(members, list)
            return " &cup; ".join(val(v) for v in members)
        if "instance" in value:
            return f"<code>{esc(value['instance'])}</code>"
    return esc(str(value))


def evidence_links(spans: tuple[str, ...]) -> str:
    return ", ".join(f"<code>{esc(s)}</code>" for s in spans) if spans else "&mdash;"


def enriched_slot(slot: EnrichedSlot) -> str:
    if slot.value is not None:
        confirmed = (
            f" <span class=meta>confirmed by {esc(slot.confirmed_by)}</span>"
            if slot.confirmed_by
            else ""
        )
        return f"{esc(slot.value)} <span class='badge draft'>DRAFT (LLM)</span>{confirmed}"
    if slot.insufficient_evidence is not None:
        return f"<span class=meta>no draft: {esc(slot.insufficient_evidence)}</span>"
    return "<span class=meta>not drafted — run <code>--enrich</code></span>"


def dv(record: Record, field: str) -> JsonRepr:
    if record.derived is None:
        return None
    value: JsonRepr = getattr(record.derived, field).value
    return value


def coverage_banner(record: Record) -> str:
    """Partial-coverage warning: the difference between "no AI found" and
    "no AI found in the part we can see" — pinned above everything."""
    unanalysed = {
        ext: n
        for ext, n in record.scan_health.language_files.items()
        if ext not in PIPELINE_EXTS and n > 0
    }
    if not unanalysed:
        return ""
    counts = ", ".join(
        f"{n} &times; <code>{esc(ext)}</code>" for ext, n in sorted(unanalysed.items())
    )
    return (
        "<p class=note><b>&#9888; Partial coverage:</b> aiscan analyses Python "
        f"and TypeScript/JavaScript. Not analysed: {counts} source files. Any AI "
        "usage in that code is <b>not</b> reflected in this record.</p>"
    )


def page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title>"
        f"<style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


# -- record body sections -----------------------------------------------------


def plain_value(value: JsonRepr) -> str:
    """Record-body rendering of a machine value: plain English, no scanner
    vocabulary (the annex uses ``val`` for the machine-grade form)."""
    if value is None:
        return "&mdash;"
    if isinstance(value, str):
        return esc(value)
    if isinstance(value, dict):
        if "symbolic" in value:
            sym = str(value["symbolic"])
            kind, _, key = sym.partition(":")
            label = {"env": "environment variable", "config": "config file"}.get(kind, kind)
            return f"<code>{esc(key)}</code> <span class=meta>({esc(label)})</span>"
        if "unresolved" in value:
            return "<span class=meta>not determined from code</span>"
        if "template" in value:
            return (
                f"<code>{esc(value['template'])}</code>"
                " <span class=meta>(built at runtime)</span>"
            )
        if isinstance(value.get("union"), list):
            members = value["union"]
            assert isinstance(members, list)
            return " <span class=meta>or</span> ".join(plain_value(v) for v in members)
        if "instance" in value:
            return f"<code>{esc(value['instance'])}</code>"
    return esc(str(value))


def _soft_chips(quals: object) -> str:
    """Soft qualifier chips for a list/tuple of qualifier strings; "" for
    anything else (a non-list value from a machine dict has no chips)."""
    if not isinstance(quals, list | tuple):
        return ""
    return "".join(f"<span class='chip soft'>{esc(q)}</span>" for q in quals)


def _model_cell(value: JsonRepr, record: Record, api_style: str = "unknown") -> str:
    display, quals = canonical_model(value, record.env_defaults)
    provider = _API_PROVIDERS.get(api_style, "")
    chips = _soft_chips(quals)
    provider_html = f" <span class=meta>({esc(provider)})</span>" if provider else ""
    return f"<b>{esc(display)}</b>{provider_html} {chips}"


def _identity(record: Record) -> str:
    repo = f" &middot; {esc(record.repo_url)}" if record.repo_url else ""
    meta = (
        f"<div class=meta>{esc(record.bundle_id)}{repo}"
        f" &middot; version (commit) <code>{esc(record.scanned_commit[:12])}</code>"
        f" &middot; scanned {esc(record.inventory_provenance.scanned_at)}</div>"
    )
    owner = (
        f"{esc(record.owner.value)}"
        if record.owner.value
        else (
            f"{esc(record.owner.candidate)} <span class=meta>(candidate — unconfirmed)</span>"
            if record.owner.candidate
            else "<span class=meta>&mdash;</span>"
        )
    )
    rows = [f"<dt>Owner</dt><dd>{owner}</dd>"]
    if record.contributor_candidates:
        rows.append(
            "<dt>Contributors</dt><dd>"
            + ", ".join(esc(c) for c in record.contributor_candidates)
            + " <span class=meta>(candidates, from commit history)</span></dd>"
        )
    if record.ai_commit_range:
        first = record.ai_commit_range.get("first", "")[:10]
        last = record.ai_commit_range.get("last", "")[:10]
        rows.append(f"<dt>AI code activity</dt><dd>{esc(first)} &rarr; {esc(last)}</dd>")
    return (
        f"<h1>{esc(record.name)} "
        f"<span class='badge {record.ai_verdict}'>"
        f"{esc(record.ai_verdict.replace('_', ' ').upper())}</span></h1>"
        + meta
        + coverage_banner(record)
        + "<dl>"
        + "".join(rows)
        + "</dl>"
    )


def _location_split(by_loc: JsonRepr) -> str:
    """`(production: n / example: n / test: n)` meta span, or "" when empty."""
    if not isinstance(by_loc, dict) or not any(by_loc.values()):
        return ""
    return (
        " <span class=meta>("
        + " / ".join(f"{k}: {by_loc.get(k, 0)}" for k in ("production", "example", "test"))
        + ")</span>"
    )


def _what_it_is(record: Record) -> str:
    st = dv(record, "system_type")
    type_text = "&mdash;"
    if isinstance(st, dict):
        kind = str(st.get("value", ""))
        components = st.get("components")
        comp_text = (
            f" ({', '.join(str(c) for c in components)})"
            if isinstance(components, list) and components
            else ""
        )
        type_text = esc(kind + comp_text)
    loc_text = _location_split(dv(record, "agents_by_location"))
    tool_loc_text = _location_split(dv(record, "tools_by_location"))
    models_all = dv(record, "models_used")
    outside_tests = None
    if isinstance(models_all, list):
        outside_tests = sum(
            1
            for m in models_all
            if isinstance(m, dict) and set(m.get("locations") or []) != {"test"}
        )
    show_note = (
        outside_tests is not None
        and isinstance(models_all, list)
        and outside_tests < len(models_all)
    )
    model_note = (
        f" <span class=meta>({outside_tests} outside test code)</span>"
        if show_note
        else ""
    )
    counts = (
        f"{val(dv(record, 'agent_count'))} agents{loc_text} &middot; "
        f"{val(dv(record, 'tool_count'))} tools{tool_loc_text} &middot; "
        f"{val(dv(record, 'model_count'))} models{model_note} &middot; "
        f"{val(dv(record, 'mcp_count'))} MCP servers"
    )
    flags = dv(record, "capability_flags")
    caps = "<span class=meta>none detected in analysed code</span>"
    if isinstance(flags, dict):
        on = [_FLAG_PHRASES.get(k, k) for k, v in sorted(flags.items()) if v]
        if on:
            caps = "".join(f"<span class='chip on'>{esc(p)}</span>" for p in on)
    autonomy = dv(record, "autonomy_profile")
    autonomy_html = (
        "human-approval gate found in code"
        if autonomy == "approval_gated"
        else "no human-approval gate detected in code"
        if autonomy == "autonomous"
        else "&mdash;"
    )
    signals = dv(record, "data_signals")
    signals_html = "<span class=meta>none detected in analysed code</span>"
    if isinstance(signals, list) and signals:
        signals_html = "; ".join(esc(str(s)) for s in signals)
    return (
        "<h2>What this system is</h2><dl>"
        f"<dt>Description</dt><dd>{enriched_slot(record.description)}</dd>"
        f"<dt>Purpose</dt><dd>{enriched_slot(record.system_summary)}</dd>"
        f"<dt>AI type</dt><dd>{type_text}</dd>"
        f"<dt>Composition</dt><dd>{counts}</dd>"
        f"<dt>Capabilities</dt><dd>{caps}</dd>"
        f"<dt>Autonomy</dt><dd>{autonomy_html}</dd>"
        f"<dt>Data in / data out</dt><dd>{enriched_slot(record.data_interaction_summary)}</dd>"
        f"<dt>Data signals</dt><dd>{signals_html}</dd>"
        "</dl>"
    )


def _agent_card(
    agent: AgentRecord,
    record: Record,
    tools_by_id: dict[str, ToolRecord],
    open_card: bool = True,
) -> str:
    rows: list[str] = []
    rows.append(f"<dt>Role</dt><dd>{enriched_slot(agent.agent_summary)}</dd>")
    if agent.model is not None:
        rows.append(
            f"<dt>Foundation model</dt><dd>"
            f"{_model_cell(agent.model.value, record, agent.model.api_style)}</dd>"
        )
    else:
        rows.append(
            "<dt>Foundation model</dt><dd><span class=meta>not determined "
            "from code</span></dd>"
        )
    # SPEC_INVENTORY: governed-model reconciliation — flag an external vendor
    # endpoint on the agent's own model, and note when >1 model is bound.
    if agent.model is not None and agent.model.provider_class == "vendor_external":
        rows.append(
            "<dt>Model provider</dt><dd>external vendor endpoint"
            + (
                " <span class=meta>(+ more models bound — see record.json)</span>"
                if agent.has_additional_models
                else ""
            )
            + "</dd>"
        )
    elif agent.has_additional_models:
        rows.append(
            "<dt>Model provider</dt><dd><span class=meta>more than one model bound "
            "— see record.json</span></dd>"
        )
    # SPEC_INVENTORY: liveness tier — a confidence signal (emphasis), not proof.
    liveness = agent.liveness.value if agent.liveness else None
    if isinstance(liveness, str):
        rows.append(f"<dt>Liveness</dt><dd>{_LIVENESS_LABEL.get(liveness, esc(liveness))}</dd>")
    # Tools, joined inline with plain-English effects.
    tool_bits: list[str] = []
    cred_refs: list[str] = []
    for tid in agent.tools:
        tool = tools_by_id.get(tid)
        if tool is None:
            tool_bits.append(f"<code>{esc(tid)}</code>")
            continue
        effects = ", ".join(_EFFECT_PHRASES.get(e, e) for e in tool.side_effects)
        target = (
            f" &rarr; {esc(tool.external_target)}"
            if isinstance(tool.external_target, str)
            else ""
        )
        detail = f" <span class=meta>({effects}{target})</span>" if effects else ""
        tool_bits.append(f"<code>{esc(tool.tool_id)}</code>{detail}")
        if tool.credential_ref is not None:
            cred_refs.append(plain_value(tool.credential_ref))
    if tool_bits:
        tools_cell = "<br>".join(tool_bits)
    elif any(
        f.kind == "unresolved_tools" and f.subject_ref == f"agent:{agent.agent_id}"
        for f in record.findings
    ):
        # SPEC-7 Z4: a known-unknown is not an absence.
        tools_cell = (
            "<span class=meta>declared in model call — definitions not resolved "
            "from code</span>"
        )
    else:
        tools_cell = "<span class=meta>none detected in analysed code</span>"
    rows.append("<dt>Tools</dt><dd>" + tools_cell + "</dd>")
    flags = agent.capability_flags.value if agent.capability_flags else None
    sends = isinstance(flags, dict) and bool(flags.get("sends_external"))
    rows.append(
        "<dt>Acts externally?</dt><dd>"
        + ("yes — sends data to external services" if sends else "no external sends detected")
        + "</dd>"
    )
    autonomy = agent.autonomy_level.value if agent.autonomy_level else None
    if autonomy == "approval_gated":
        ev = agent.autonomy_level.evidence if agent.autonomy_level else ()
        rows.append(
            f"<dt>Autonomy</dt><dd>human-approval gate found ({evidence_links(ev)})</dd>"
        )
    else:
        rows.append("<dt>Autonomy</dt><dd>no human-approval gate detected in code</dd>")
    if agent.state:
        rows.append(
            "<dt>Memory / state</dt><dd>"
            + ", ".join(_STATE_PHRASES.get(s.kind, s.kind) for s in agent.state)
            + "</dd>"
        )
    connections = [*agent.handoffs, *agent.routes]
    rows.append(
        "<dt>Connections</dt><dd>"
        + (
            ", ".join(f"<code>{esc(c)}</code>" for c in connections)
            if connections
            else "<span class=meta>no connections to other agents detected</span>"
        )
        + "</dd>"
    )
    guardrails = [p.kind for p in agent.control_policies if p.kind != "approval"]
    rows.append(
        "<dt>Guardrails</dt><dd>"
        + (
            ", ".join(esc(g) for g in guardrails)
            if guardrails
            else "<span class=meta>none detected in analysed code</span>"
        )
        + "</dd>"
    )
    if cred_refs:
        seen: list[str] = []
        for c in cred_refs:
            if c not in seen:
                seen.append(c)
        rows.append("<dt>Credential references</dt><dd>" + ", ".join(seen) + "</dd>")
    rows.append(
        f"<dt>Detected at</dt><dd>{evidence_links(agent.detection.evidence)}"
        f" <span class=meta>(confidence {esc(agent.detection.confidence)})</span></dd>"
    )
    body = "<dl>" + "".join(rows) + "</dl>"
    if agent.system_prompt is not None:
        badges = []
        if agent.system_prompt.dynamic:
            badges.append("<span class=chip>built at runtime</span>")
        badges.append(f"<span class=chip>source: {esc(agent.system_prompt.origin)}</span>")
        text = ""
        if isinstance(agent.system_prompt.value, str):
            raw = agent.system_prompt.value
            truncated = (
                " <span class=meta>[truncated — full text in record.json]</span>"
                if len(raw) > PROMPT_CAP
                else ""
            )
            text = f"<pre>{esc(raw[:PROMPT_CAP])}</pre>{truncated}"
        else:
            text = f"<p>{plain_value(agent.system_prompt.value)}</p>"
        body += f"<p><b>Instructions / system prompt</b> {' '.join(badges)}</p>{text}"
    else:
        # D∅: the agent runs with instructions the scan could not capture —
        # say so, never silently omit the field.
        body += (
            "<p><b>Instructions / system prompt</b> <span class=meta>none captured "
            "from analysed code — the prompt may be assembled at runtime</span></p>"
        )
    lang = f"<span class=chip>{esc(agent.language)}</span>"
    live = agent.liveness.value if agent.liveness else None
    live_chip = f" <span class=chip>{esc(live)}</span>" if isinstance(live, str) else ""
    return (
        f"<details id='agent-{esc(agent.agent_id)}'{' open' if open_card else ''}>"
        f"<summary>{esc(agent.agent_id)} {lang}{live_chip}</summary>"
        f"<div>{body}</div></details>"
    )


def _by_location(
    heading: str,
    noun: str,
    items: list[Any],
    render_card: Callable[[Any, bool], str],
) -> str:
    """Section grouped by location (production | example | test) — example/test
    collapse into a <details> so a framework repo's fixtures don't read as the
    production estate. Production cards pre-open only below 20 total items."""
    parts = [f"<h2>{heading}</h2>"]
    total = len(items)
    for loc in ("production", "example", "test"):
        group = [x for x in items if x.location == loc]
        if not group:
            continue
        if loc != "production":
            parts.append(
                f"<details class=locgroup><summary>{esc(loc)} {noun} "
                f"({len(group)})</summary><div>"
            )
        open_cards = loc == "production" and total <= 20
        for x in group:
            parts.append(render_card(x, open_cards))
        if loc != "production":
            parts.append("</div></details>")
    return "".join(parts)


def _agents_section(record: Record) -> str:
    if not record.agents:
        return "<h2>Agents</h2><p class=meta>None detected.</p>"
    tools_by_id = {t.tool_id: t for t in record.tools}
    return _by_location(
        "Agents",
        "agents",
        list(record.agents),
        lambda a, oc: _agent_card(a, record, tools_by_id, oc),
    )


def _tools_section(record: Record) -> str:
    """Per-tool cards in the record: what it does, effects, who uses it.
    Grouped by location like the agents section — example/test tools collapse
    so a framework repo's fixtures don't read as the production estate."""
    if not record.tools:
        return ""
    used_by: dict[str, list[str]] = {}
    for agent in record.agents:
        for tid in agent.tools:
            used_by.setdefault(tid, []).append(agent.agent_id)
    return _by_location(
        "Tools", "tools", list(record.tools), lambda t, oc: _tool_card(t, used_by, oc)
    )


def _tool_card(tool: ToolRecord, used_by: dict[str, list[str]], open_card: bool) -> str:
    sensitive = (
        "<span class='chip on'>sensitive</span>"
        if tool.is_sensitive is not None and tool.is_sensitive.value
        else ""
    )
    effects = ", ".join(_EFFECT_PHRASES.get(e, e) for e in tool.side_effects)
    rows = [
        f"<dt>What it does</dt><dd>{enriched_slot(tool.tool_summary)}</dd>",
        "<dt>Effects</dt><dd>"
        + (
            esc(effects)
            if effects
            else "<span class=meta>none detected in analysed code</span>"
        )
        + "</dd>",
    ]
    if isinstance(tool.external_target, str):
        rows.append(
            f"<dt>External target</dt><dd><code>{esc(tool.external_target)}</code></dd>"
        )
    if tool.credential_ref is not None:
        rows.append(
            f"<dt>Credential reference</dt><dd>{plain_value(tool.credential_ref)}</dd>"
        )
    users = used_by.get(tool.tool_id)
    rows.append(
        "<dt>Used by</dt><dd>"
        + (
            ", ".join(f"<code>{esc(a)}</code>" for a in sorted(users))
            if users
            else "<span class=meta>not bound to a detected agent</span>"
        )
        + "</dd>"
    )
    rows.append(f"<dt>Defined at</dt><dd>{evidence_links(tool.evidence)}</dd>")
    return (
        f"<details{' open' if open_card else ''}>"
        f"<summary>{esc(tool.tool_id)} {sensitive}</summary>"
        f"<div><dl>{''.join(rows)}</dl></div></details>"
    )


def _models_section(record: Record) -> str:
    models = dv(record, "models_used")
    if not isinstance(models, list) or not models:
        return ""
    # SPEC-7: models seen ONLY in test code collapse into one line — a
    # framework repo's fake models must not read as the production estate.
    visible: list[object] = []
    test_only = 0
    for m in models:
        locs = m.get("locations") if isinstance(m, dict) else None
        if isinstance(locs, list) and locs and set(locs) == {"test"}:
            test_only += 1
        else:
            visible.append(m)
    rows = []
    for m in visible:
        if not isinstance(m, dict):
            continue
        display = str(m.get("display", ""))
        chips = _soft_chips(m.get("qualifiers"))
        provider = _PROVIDER_PHRASES.get(str(m.get("provider_class", "")), "unknown")
        task = _TASK_PHRASES.get(str(m.get("task", "")), str(m.get("task", "")))
        endpoint = m.get("endpoint")
        if endpoint is None:
            endpoint_html = "<span class=meta>provider default</span>"
        elif isinstance(endpoint, str):
            endpoint_html = esc(endpoint)
        else:
            # Same canonical reading as the model column — a raw union/symbolic
            # machine value is annex-grade, not record-body language.
            ep_display, ep_quals = canonical_model(endpoint, record.env_defaults)
            endpoint_html = esc(ep_display) + _soft_chips(ep_quals)
        rows.append(
            f"<tr><td><b>{esc(display)}</b></td><td>{chips or '&mdash;'}</td>"
            f"<td>{esc(provider)}</td><td>{esc(task)}</td><td>{endpoint_html}</td></tr>"
        )
    note = (
        f"<p class=meta>{test_only} further model(s) seen only in test code — "
        "full list in record.json and the technical annex.</p>"
        if test_only
        else ""
    )
    return (
        "<h2>Models used</h2><table><tr><th>Model</th><th>Configuration</th>"
        "<th>Provider</th><th>Used for</th><th>Endpoint</th></tr>"
        + "".join(rows)
        + "</table>"
        + note
    )


def _connections_section(record: Record) -> str:
    parts = ["<h2>Connections &amp; dependencies</h2><dl>"]
    external = dv(record, "external_systems")
    parts.append(
        "<dt>External services</dt><dd>"
        + (
            ", ".join(f"<code>{esc(e)}</code>" for e in external)
            if isinstance(external, list) and external
            else "<span class=meta>none detected</span>"
        )
        + "</dd>"
    )
    if record.mcp_servers:
        parts.append(
            "<dt>MCP servers</dt><dd>"
            + "<br>".join(
                f"{val(s.server)} <span class=meta>({esc(s.transport)})</span>"
                for s in record.mcp_servers
            )
            + "</dd>"
        )
    deps = [
        f"<code>{esc(d.package)}</code>{' ' + esc(d.version) if d.version else ''}"
        f" ({esc(d.ecosystem)}{', unused' if not d.used else ''})"
        for d in record.ai_dependencies
    ]
    parts.append(
        "<dt>AI components</dt><dd>"
        + (", ".join(deps) if deps else "<span class=meta>none declared</span>")
        + "</dd>"
    )
    parts.append(
        "<dt>Sourcing</dt><dd>"
        + (
            "vendor AI components as listed above; "
            if deps
            else ""
        )
        + "<span class=meta>repository code sourcing not determined from code</span>"
        + "</dd></dl>"
    )
    return "".join(parts)


def _assurance_line(record: Record, annex_href: str) -> str:
    health = record.scan_health
    unresolved = dv(record, "has_unresolved_models")
    note = (
        "some model values could not be determined from code; "
        if unresolved
        else ""
    )
    return (
        "<h2>About this record</h2><p class=meta>"
        f"Produced by static code analysis of {health.files} files "
        f"({health.loc} lines) — nothing was executed. {esc(note)}"
        f"{health.findings} item(s) recorded for review. "
        f"Full detail: <a href='{esc(annex_href)}'>technical annex</a>; "
        "machine-readable source of truth: record.json.</p>"
    )


_NO_AI_CHECKLIST = (
    "AI package names in dependency manifests",
    "AI SDK import statements",
    "known provider hosts and org gateway hosts",
    "chat-completion URL path fragments",
    "model/messages payload shapes near HTTP clients",
    "prompt and agent artefacts (prompts/, *.prompt, system*.md, SKILL.md, mcp.json)",
    "imports of org-declared wrapper packages",
)


def no_ai_body(record: Record) -> str:
    checks = "".join(f"<li>{esc(c)}</li>" for c in _NO_AI_CHECKLIST)
    return (
        "<h2>No AI detected</h2>"
        "<p>The triage gate found <b>zero AI signals</b> in this repository. "
        "This record is the negative attestation: what was checked, at which commit, when.</p>"
        f"<p><b>Checked for:</b></p><ul>{checks}</ul>"
    )


def inventory_body(record: Record, graph_svg: str | None, annex_href: str) -> str:
    """The record body shared verbatim by inventory.html and report.html."""
    if record.ai_verdict == "no_ai":
        return _identity(record) + no_ai_body(record)
    sections = [
        _identity(record),
        _what_it_is(record),
        _agents_section(record),
        _tools_section(record),
        _models_section(record),
        _connections_section(record),
    ]
    if graph_svg:
        sections.append(f"<h2>System graph</h2>{graph_svg}")
    sections.append(_assurance_line(record, annex_href))
    return "".join(sections)


def render_inventory(record: Record, graph_svg: str | None = None) -> str:
    """Standalone AI inventory record page."""
    body = inventory_body(record, graph_svg, annex_href="report.html")
    body += (
        "<footer>AI inventory record generated by aiscan &middot; "
        "technical annex: report.html &middot; machine-readable: record.json</footer>"
    )
    return page(f"{record.name} — AI inventory record", body)
