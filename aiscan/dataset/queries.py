"""Named estate queries (SPEC-3 §8) — plain SQL over the dataset tables.

Each is one filter over the projection; each has an expected-rows test over
the fixture corpus. Run with :func:`run_query`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

QUERIES: dict[str, str] = {
    # Agents that can move money across the estate.
    "agents_move_money": (
        "SELECT bundle_id, agent_id, model_value, model_endpoint FROM agents "
        "WHERE moves_money = 1 ORDER BY bundle_id, agent_id"
    ),
    # Models in use grouped by provider class.
    "models_by_provider_class": (
        "SELECT provider_class, model_value, endpoint, COUNT(*) AS uses FROM models "
        "GROUP BY provider_class, model_value, endpoint "
        "ORDER BY provider_class, model_value"
    ),
    # Systems calling an endpoint outside the approved host lists.
    "systems_unapproved_endpoint": (
        "SELECT bundle_id, name, ai_verdict FROM systems "
        "WHERE has_unapproved_endpoint = 1 ORDER BY bundle_id"
    ),
    # Bespoke vs framework agent counts by repo.
    "agent_kinds_by_repo": (
        "SELECT bundle_id, "
        "SUM(CASE WHEN detection_method LIKE '%bespoke%' THEN 1 ELSE 0 END) AS bespoke, "
        "SUM(CASE WHEN detection_method NOT LIKE '%bespoke%' THEN 1 ELSE 0 END) AS framework "
        "FROM agents GROUP BY bundle_id ORDER BY bundle_id"
    ),
    # Tools with admin mutation in systems with no approval-gated agent.
    "admin_tools_without_approval": (
        "SELECT t.bundle_id, t.tool_id FROM tools t "
        "WHERE t.side_effects LIKE '%admin_mutation%' AND NOT EXISTS ("
        "  SELECT 1 FROM agents a WHERE a.bundle_id = t.bundle_id "
        "  AND a.scan_id = t.scan_id AND a.autonomy_level = 'approval_gated') "
        "ORDER BY t.bundle_id, t.tool_id"
    ),
    # Systems with dynamic prompts and high-privilege capability.
    "dynamic_prompt_high_privilege": (
        "SELECT bundle_id, name FROM systems WHERE has_dynamic_prompts = 1 "
        "AND (moves_money = 1 OR executes_code = 1 OR mutates_identities = 1) "
        "ORDER BY bundle_id"
    ),
    # Repos with AI dependencies declared but nothing detected (dormant).
    "dormant_ai_repos": (
        "SELECT bundle_id, name FROM systems WHERE ai_verdict = 'ai_signals_only' "
        "ORDER BY bundle_id"
    ),
    # High findings across the estate.
    "high_findings": (
        "SELECT bundle_id, kind, subject_ref, detail FROM findings "
        "WHERE severity = 'high' ORDER BY bundle_id, kind, finding_id"
    ),
}


def run_query(db_path: Path, name: str) -> list[tuple[object, ...]]:
    sql = QUERIES[name]
    conn = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in conn.execute(sql)]
    finally:
        conn.close()
