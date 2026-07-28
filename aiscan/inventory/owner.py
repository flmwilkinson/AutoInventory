"""Owner candidate (SPEC §6.9): best-effort, cheap, clearly marked candidate.

CODEOWNERS first; else the most common author-email local part from recent
git history. The [G] owner value itself is never written by the scanner.
"""

from __future__ import annotations

import logging
import subprocess
from collections import Counter
from pathlib import Path

_CODEOWNER_LOCATIONS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def owner_candidate(repo_root: Path, logger: logging.Logger) -> str | None:
    for rel in _CODEOWNER_LOCATIONS:
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                return parts[1].lstrip("@")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--format=%ae", "-20"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    locals_ = [line.split("@")[0] for line in proc.stdout.splitlines() if "@" in line]
    if not locals_:
        return None
    counts = Counter(locals_)
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best or None


def ai_provenance(
    repo_root: Path, ai_files: list[str], logger: logging.Logger, limit: int = 5
) -> tuple[tuple[str, ...], dict[str, str] | None]:
    """SPEC-6 §3.4: (contributor candidates, {first,last} AI-commit dates) from
    git history over the AI-touching files. Candidates only — a committer is a
    lead, not an owner. Requires ``repo_root`` to be a git repo top level (a
    fixture directory inside some other repo must not inherit that repo's
    history); anything missing degrades to empty, never an error."""
    if not ai_files or not (repo_root / ".git").exists():
        return (), None
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "--format=%an\t%aI",
                "--",
                *sorted(set(ai_files))[:50],
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (), None
    if proc.returncode != 0 or not proc.stdout.strip():
        return (), None
    authors: list[str] = []
    dates: list[str] = []
    for line in proc.stdout.splitlines():
        name, _, date = line.partition("\t")
        if name.strip():
            authors.append(name.strip())
        if date.strip():
            dates.append(date.strip())
    counts = Counter(authors)
    top = tuple(
        name for name, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    )
    commit_range = (
        {"first": min(dates), "last": max(dates)} if dates else None
    )
    logger.info("ai provenance: %d contributors, %d commits", len(counts), len(dates))
    return top, commit_range
