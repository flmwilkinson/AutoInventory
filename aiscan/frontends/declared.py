"""Declared-agent artefacts (SPEC-4 §8).

Repos like DocGen name their agent roster with a prompt tree
(``packages/prompts/agents/<name>/system.md``). A markdown tree is *not*
detection — these are surfaced as declared candidates (a finding + scan_health
entry), bound to a code-detected agent when the slug matches one, and never
promoted to a standalone AgentDef. This keeps the record honest: "the repo
declares these agents; here is which ones we also found in code."
"""

from __future__ import annotations

import re
from pathlib import Path

from aiscan.facts.models import FindingRecord

_SYSTEM_FILE = re.compile(r"^(system|task|prompt)[^/]*\.(md|txt|prompt)$", re.IGNORECASE)
_SKIP = {".git", "node_modules", "__pycache__", "venv", ".venv"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def find_declared_agents(repo_root: Path) -> dict[str, list[str]]:
    """Map ``declared agent name -> repo-relative prompt files`` for every
    ``**/prompts/agents/<name>/`` directory holding a system/task/prompt file."""
    roster: dict[str, list[str]] = {}
    for dirpath in repo_root.rglob("agents"):
        if not dirpath.is_dir() or _SKIP & set(dirpath.parts):
            continue
        # Require the parent to be a `prompts` directory.
        if dirpath.parent.name != "prompts":
            continue
        for agent_dir in sorted(dirpath.iterdir()):
            if not agent_dir.is_dir():
                continue
            prompts = sorted(
                p.relative_to(repo_root).as_posix()
                for p in agent_dir.iterdir()
                if p.is_file() and _SYSTEM_FILE.match(p.name)
            )
            if prompts:
                roster[agent_dir.name] = prompts
    return dict(sorted(roster.items()))


def declared_agent_findings(
    roster: dict[str, list[str]],
    detected_agent_names: set[str],
    ai_signals_only: bool,
) -> list[FindingRecord]:
    """One finding per declared agent NOT matched to a code-detected agent.

    Severity is ``info`` normally, ``medium`` when the whole repo detected no
    agents (declared roster with no analysable code — the DocGen-before-SPEC-4
    story: real agents that live outside what we can read)."""
    detected_slugs = {_slug(n) for n in detected_agent_names}
    findings: list[FindingRecord] = []
    for name, prompts in roster.items():
        if _slug(name) in detected_slugs:
            continue  # bound to a code-detected agent — not a gap
        findings.append(
            FindingRecord(
                kind="declared_agent_artefact",
                evidence=tuple(prompts),
                detail=(
                    f"agent '{name}' is declared by a prompt roster but was not "
                    "found in analysed code"
                ),
                severity="medium" if ai_signals_only else "info",
                subject_ref=f"declared:{name}",
            )
        )
    return findings
