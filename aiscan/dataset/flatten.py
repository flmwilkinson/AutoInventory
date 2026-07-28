"""Pure ``flatten(record) -> rows`` (SPEC-3 §8).

Records are the source of truth; these rows are a projection, fully
rebuildable. Works on the record.json *dict* so the dataset can always be
rebuilt from artifacts on disk, whatever scanner version wrote them (missing
fields flatten to null). Complex cells are canonical-JSON-encoded strings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

Row = dict[str, object]

CAPABILITY_FLAGS = (
    "moves_money",
    "executes_code",
    "mutates_identities",
    "sends_external",
    "reads_sensitive",
)


def _j(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def scan_id_for(record: dict[str, Any]) -> str:
    prov = record.get("inventory_provenance") or {}
    basis = f"{record.get('bundle_id')}|{record.get('scanned_commit')}|{prov.get('scanned_at')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _derived(record: dict[str, Any], field: str) -> object:
    derived = record.get("derived") or {}
    slot = derived.get(field) or {}
    return slot.get("value") if isinstance(slot, dict) else None


def _slot_value(entity: dict[str, Any], field: str) -> object:
    slot = entity.get(field)
    return slot.get("value") if isinstance(slot, dict) else None


def _slot_candidate(entity: dict[str, Any], field: str) -> object:
    slot = entity.get(field)
    return slot.get("candidate") if isinstance(slot, dict) else None


def _system_type_text(record: dict[str, Any]) -> str | None:
    """SPEC-6 Tier-1: 'agentic (multi-agent, rag)' — from the derived field."""
    st = _derived(record, "system_type")
    if not isinstance(st, dict):
        return None
    kind = str(st.get("value", ""))
    components = st.get("components")
    if isinstance(components, list) and components:
        return f"{kind} ({', '.join(str(c) for c in components)})"
    return kind or None


def _models_display(record: dict[str, Any]) -> str | None:
    """SPEC-6 Tier-1: canonical model displays joined — matches the record."""
    models = _derived(record, "models_used")
    if not isinstance(models, list):
        return None
    displays: list[str] = []
    for m in models:
        if isinstance(m, dict) and m.get("display") and m["display"] not in displays:
            displays.append(str(m["display"]))
    return "; ".join(displays) or None


def flatten(record: dict[str, Any]) -> dict[str, list[Row]]:
    """One record → rows for the systems/agents/tools/models/findings tables."""
    bundle_id = record.get("bundle_id")
    scan_id = scan_id_for(record)
    prov = record.get("inventory_provenance") or {}
    key = {"bundle_id": bundle_id, "scan_id": scan_id}

    flags = _derived(record, "capability_flags")
    flags = flags if isinstance(flags, dict) else {}
    by_loc = _derived(record, "agents_by_location")
    by_loc = by_loc if isinstance(by_loc, dict) else {}

    systems: list[Row] = [
        {
            **key,
            "name": record.get("name"),
            "repo_url": record.get("repo_url"),
            "commit": record.get("scanned_commit"),
            "ai_verdict": record.get("ai_verdict"),
            "agent_count": _derived(record, "agent_count"),
            "tool_count": _derived(record, "tool_count"),
            "system_type": _system_type_text(record),
            "models_display": _models_display(record),
            "contributors": _j(list(record.get("contributor_candidates") or [])),
            "loc_production": by_loc.get("production"),
            "loc_example": by_loc.get("example"),
            "loc_test": by_loc.get("test"),
            "autonomy_profile": _derived(record, "autonomy_profile"),
            **{flag: bool(flags.get(flag)) for flag in CAPABILITY_FLAGS},
            "has_unapproved_endpoint": _derived(record, "has_unapproved_endpoint"),
            "has_dynamic_prompts": _derived(record, "has_dynamic_prompts"),
            "has_unresolved_models": _derived(record, "has_unresolved_models"),
            "suggested_aia_risk_category": _slot_value(record, "suggested_aia_risk_category"),
            "system_summary": _slot_value(record, "system_summary"),
            "owner_value": _slot_value(record, "owner"),
            "owner_candidate": _slot_candidate(record, "owner"),
            "risk_tier": _slot_value(record, "risk_tier"),
            "lifecycle_status": _slot_value(record, "lifecycle_status"),
            "approval_status": _slot_value(record, "approval_status"),
            "scanned_at": prov.get("scanned_at"),
            "scanner_ver": prov.get("scanner"),
        }
    ]

    agents: list[Row] = []
    for agent in record.get("agents") or []:
        detection = agent.get("detection") or {}
        model = agent.get("model") or {}
        agent_flags = _slot_value(agent, "capability_flags")
        agent_flags = agent_flags if isinstance(agent_flags, dict) else {}
        reachable = _slot_value(agent, "reachable_tools")
        agents.append(
            {
                **key,
                "agent_id": agent.get("agent_id"),
                "location": agent.get("location"),
                "language": agent.get("language"),
                "role_class": _slot_value(agent, "role_class"),
                "autonomy_level": _slot_value(agent, "autonomy_level"),
                "model_value": _j(model.get("value")),
                "model_endpoint": _j(model.get("endpoint")),
                "api_style": model.get("api_style"),
                "prompt_dynamic": bool((agent.get("system_prompt") or {}).get("dynamic")),
                "tool_count": len(agent.get("tools") or []),
                "reachable_tool_count": (
                    len(reachable) if isinstance(reachable, list) else None
                ),
                **{flag: bool(agent_flags.get(flag)) for flag in CAPABILITY_FLAGS},
                "detection_method": detection.get("method"),
                "confidence": detection.get("confidence"),
                "agent_summary": _slot_value(agent, "agent_summary"),
            }
        )

    tools: list[Row] = []
    for tool in record.get("tools") or []:
        tools.append(
            {
                **key,
                "tool_id": tool.get("tool_id"),
                "kind": tool.get("kind"),
                "location": tool.get("location"),
                "side_effects": _j(tool.get("side_effects")),
                "external_target": _j(tool.get("external_target")),
                "is_sensitive": _slot_value(tool, "is_sensitive"),
                "declared_authorisation": _slot_value(tool, "declared_authorisation"),
                "credential_ref": _j(tool.get("credential_ref")),
                "capability_class": _slot_value(tool, "capability_class"),
                "tool_summary": _slot_value(tool, "tool_summary"),
            }
        )

    models: list[Row] = []
    models_used = _derived(record, "models_used")
    if isinstance(models_used, list):
        for entry in models_used:
            if not isinstance(entry, dict):
                continue
            encoded = json.dumps(entry, sort_keys=True)
            models.append(
                {
                    **key,
                    "model_key": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
                    "model_value": _j(entry.get("model")),
                    "display": entry.get("display"),
                    "task": entry.get("task"),
                    "endpoint": _j(entry.get("endpoint")),
                    "api_style": entry.get("api_style"),
                    "provider_class": entry.get("provider_class"),
                }
            )

    findings: list[Row] = []
    for i, finding in enumerate(record.get("findings") or []):
        findings.append(
            {
                **key,
                "finding_id": f"{scan_id}-{i:04d}",
                "kind": finding.get("kind"),
                "severity": finding.get("severity"),
                "subject_ref": finding.get("subject_ref"),
                "detail": finding.get("detail"),
            }
        )

    return {
        "systems": systems,
        "agents": agents,
        "tools": tools,
        "models": models,
        "findings": findings,
    }
