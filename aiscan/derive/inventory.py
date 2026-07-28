"""Inventory-record derivations (SPEC-6 §3) — pure, deterministic.

The inventory record renders plain English; these functions turn detected
values into that surface without discarding anything: a canonical display
string plus qualifier chips, with the full machine value staying in the
record. The one interpretive rule is factual, not a guess: the single string
literal in an ``env || literal`` union IS the code default.
"""

from __future__ import annotations

from aiscan.ingest.env_defaults import EnvDefault
from aiscan.inventory.schema import AgentRecord, ModelUsage, ToolRecord
from aiscan.ir.values import JsonRepr

EnvDefaults = dict[str, tuple[EnvDefault, ...]]

_BATCH_PATH_HINTS = ("worker", "job", "jobs", "batch", "cron", "tasks", "pipeline")


# -- canonical model display --------------------------------------------------


def canonical_model(value: JsonRepr, env_defaults: EnvDefaults) -> tuple[str, tuple[str, ...]]:
    """(display, qualifiers) for a detected model value. Members are never
    dropped — the record keeps the machine value; this is the reading."""
    if value is None:
        return "not determined", ("no value",)
    if isinstance(value, str):
        return value, ()
    if isinstance(value, dict):
        union = value.get("union")
        if isinstance(union, list):
            return _canonical_union(union, env_defaults)
        if "symbolic" in value:
            return _canonical_symbolic(str(value["symbolic"]), env_defaults)
        if "unresolved" in value:
            return "not determined", (str(value["unresolved"]),)
        if "template" in value:
            return str(value["template"]), ("template",)
        if "instance" in value:
            fq = str(value["instance"])
            short = fq.rsplit(".", 1)[-1].split("@")[0]
            return short, (f"model class ({fq.split('@')[0]})",)
    return str(value), ()


def _canonical_union(
    members: list[object], env_defaults: EnvDefaults
) -> tuple[str, tuple[str, ...]]:
    literals: list[str] = []
    env_keys: list[str] = []
    other_syms: list[str] = []
    unknown = False
    for m in members:
        if isinstance(m, str):
            if m:  # an empty-string code default is not a model name
                literals.append(m)
        elif isinstance(m, dict):
            if "symbolic" in m:
                sym = str(m["symbolic"])
                if sym.startswith("env:"):
                    env_keys.append(sym[len("env:") :])
                else:
                    other_syms.append(sym)
            elif "unresolved" in m:
                unknown = True
    quals: list[str] = []
    if len(literals) == 1:
        display = literals[0]
        quals.append("code default")
    elif literals:
        display = " | ".join(sorted(literals))
    elif env_keys:
        display = "set via env " + ", ".join(sorted(set(env_keys)))
    elif other_syms:
        display, sym_quals = _canonical_symbolic(other_syms[0], env_defaults)
        quals.extend(sym_quals)
    else:
        display = "not determined"
    if env_keys and literals:
        quals.append("env-configurable: " + ", ".join(sorted(set(env_keys))))
    quals.extend(_declared_qualifiers(env_keys, env_defaults))
    if unknown:
        quals.append("+unknown")
    return display, tuple(quals)


def _canonical_symbolic(sym: str, env_defaults: EnvDefaults) -> tuple[str, tuple[str, ...]]:
    kind, _, key = sym.partition(":")
    if kind == "env":
        return f"set via env {key}", tuple(_declared_qualifiers([key], env_defaults))
    if kind == "wrapper_default":
        fn = key.rsplit(".", 1)[-1]
        return f"via {fn} (wrapper)", ("model chosen by calling code",)
    if kind == "external" and key.startswith("azure:deployment:"):
        return f"azure deployment {key.rsplit(':', 1)[-1]}", ()
    if kind == "config":
        return f"set via config {key}", ()
    return sym, ()


def _declared_qualifiers(env_keys: list[str], env_defaults: EnvDefaults) -> list[str]:
    out: list[str] = []
    for key in sorted(set(env_keys)):
        entries = env_defaults.get(key)
        if entries:
            d = entries[0]
            chip = f"declared {d.value} — {d.source}"
            if chip not in out:  # two env keys, same declared value+file → one chip
                out.append(chip)
        if len(out) == 2:
            break
    return out


# -- models_used and usage groups ---------------------------------------------


def build_models_used(
    sources: list[tuple[JsonRepr, JsonRepr, str, str, str, str]],
    env_defaults: EnvDefaults,
) -> list[dict[str, object]]:
    """Canonical, deduped models table. ``sources`` rows are
    (model value, endpoint, api_style, provider_class, task, location);
    dedupe key is (display, task) — the same model reached through many call
    sites is one inventory row, carrying every location it was seen in
    (SPEC-7: a framework repo's test fakes must not read as production
    models)."""
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for value, endpoint, api_style, provider_class, task, location in sources:
        display, quals = canonical_model(value, env_defaults)
        key = (display, task)
        entry = by_key.get(key)
        if entry is None:
            by_key[key] = {
                "model": value,
                "display": display,
                "qualifiers": list(quals),
                "endpoint": endpoint,
                "api_style": api_style,
                "provider_class": provider_class,
                "task": task,
                "locations": [location],
            }
            continue
        if entry["endpoint"] is None and endpoint is not None:
            entry["endpoint"] = endpoint
        if entry["api_style"] == "unknown" and api_style != "unknown":
            entry["api_style"] = api_style
        locs = entry["locations"]
        assert isinstance(locs, list)
        if location not in locs:
            locs.append(location)
            locs.sort()
        for q in quals:
            qlist = entry["qualifiers"]
            assert isinstance(qlist, list)
            if q not in qlist:
                qlist.append(q)
    return [by_key[k] for k in sorted(by_key)]


def group_usages(
    usages: list[ModelUsage], env_defaults: EnvDefaults
) -> list[dict[str, object]]:
    """Usages grouped by (file, canonical model, task) with call counts —
    the inventory view; the per-call-site table stays in the annex."""
    groups: dict[tuple[str, str, str], dict[str, object]] = {}
    for u in usages:
        display, _quals = canonical_model(u.model.value, env_defaults)
        file = u.evidence[0].rsplit(":", 1)[0] if u.evidence else "unknown"
        key = (file, display, u.task)
        entry = groups.get(key)
        if entry is None:
            groups[key] = {
                "file": file,
                "model": display,
                "task": u.task,
                "call_sites": 1,
                "in_agent": u.in_agent,
            }
        else:
            entry["call_sites"] = int(entry["call_sites"]) + 1  # type: ignore[call-overload]
            entry["in_agent"] = bool(entry["in_agent"]) or u.in_agent
    return [groups[k] for k in sorted(groups)]


# -- system type and data signals ---------------------------------------------


def system_type_of(
    agents: list[AgentRecord], usages: list[ModelUsage]
) -> dict[str, object]:
    """SPEC-6 §3.1: agentic | genai | none, plus component qualifiers."""
    has_embeddings = any(u.task == "embedding" for u in usages)
    has_vectorstore = any(s.kind == "vectorstore" for a in agents for s in a.state)
    components: list[str] = []
    if len(agents) > 1:
        components.append("multi-agent")
    if has_embeddings and has_vectorstore:
        components.append("rag")
    elif has_embeddings:
        components.append("embeddings")
    if any(
        not u.in_agent and _is_batch_path(u.evidence[0] if u.evidence else "")
        for u in usages
    ):
        components.append("batch-llm")
    if agents:
        kind = "agentic"
    elif usages:
        kind = "genai"
    else:
        kind = "none"
    return {"value": kind, "components": components}


def _is_batch_path(evidence: str) -> bool:
    parts = evidence.rsplit(":", 1)[0].lower().split("/")
    return any(p in _BATCH_PATH_HINTS for p in parts)


def data_signals_of(
    tools: list[ToolRecord], usages: list[ModelUsage], agents: list[AgentRecord]
) -> list[str]:
    """SPEC-6 §3.3: detected data-interaction signals — never a PII claim."""
    signals: list[str] = []
    for t in tools:
        target = t.external_target if isinstance(t.external_target, str) else None
        if t.is_sensitive is not None and t.is_sensitive.value and target:
            signals.append(f"touches org-flagged sensitive host {target}")
        elif "external_send" in t.side_effects and target:
            signals.append(f"sends data to {target}")
    if any(u.task == "embedding" for u in usages):
        if any(s.kind == "vectorstore" for a in agents for s in a.state):
            signals.append("indexes documents for semantic search (RAG)")
        else:
            signals.append("computes embeddings over repository content")
    return sorted(set(signals))
