"""SPEC-10 end-to-end: the composed seams work for the PR-triggered flow, with
no network and no service. Two commits of a local git repo are scanned via the
*ephemeral* clone provider; state (manifest + facts) persists in the StateStore
across the discarded clones, so the second scan runs INCREMENTALLY and its BOM
matches a from-scratch full scan; the write-through SQLite reflects both commits
and ``bom_diff`` surfaces exactly the PR's new agent.

This is the shape a future webhook worker uses: fetch(base/head) -> core.scan
(carrying prior facts from the store) -> persist -> diff.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aiscan.cli import run_scan
from aiscan.dataset.store import bom_diff
from tests.conftest import fixture_repo
from tests.harness import normalize_record

_BASE_FILES = ("main.py", "requirements.txt")  # the known-good detection fixture
_ANALYTICS = (
    "from agents import Agent\n\n"
    'analytics_agent = Agent(\n'
    '    name="Analytics Agent",\n'
    '    instructions="Analyse product usage.",\n'
    '    model="gpt-4o-mini",\n'
    ")\n"
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_two_commits(origin: Path) -> tuple[str, str]:
    """A repo whose base commit has 2 agents (the fixture) and whose head adds a
    third in a new module. Returns (base_sha, head_sha)."""
    src = fixture_repo("fw_openai_agents_basic")
    origin.mkdir(parents=True)
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@example.com"], origin)
    _git(["config", "user.name", "Test"], origin)
    _git(["config", "uploadpack.allowFilter", "true"], origin)  # allow blobless clone
    for name in _BASE_FILES:
        (origin / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "base: triage + billing agents"], origin)
    base = _git(["rev-parse", "HEAD"], origin)
    (origin / "analytics.py").write_text(_ANALYTICS, encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "PR: add analytics agent"], origin)
    head = _git(["rev-parse", "HEAD"], origin)
    return base, head


def _agent_ids(record: dict[str, object]) -> set[str]:
    agents = record.get("agents") or []
    return {str(a["agent_id"]) for a in agents}  # type: ignore[index]


def test_pr_flow_ephemeral_incremental_and_diff(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    base, head = _seed_two_commits(origin)
    url = origin.as_uri()
    estate = tmp_path / "estate"

    # Push 1 — scan the base commit (the previously-scanned state).
    d1 = run_scan(url, commit=base, out=estate)
    r1 = json.loads((d1 / "record.json").read_text(encoding="utf-8"))
    assert len(_agent_ids(r1)) == 2  # billing + triage
    # The ephemeral clone was discarded; the state lives in the store.
    assert (estate / "_cache").is_dir()

    # Push 2 — the "PR": scan head. A fresh ephemeral clone is made, but the
    # manifest + facts from push 1 persist in the store, so this runs
    # incrementally (base == last-scanned, only analytics.py is affected).
    d2 = run_scan(url, commit=head, out=estate)
    r2 = json.loads((d2 / "record.json").read_text(encoding="utf-8"))
    assert len(_agent_ids(r2)) == 3  # + analytics

    # Correctness: the incremental BOM (across ephemeral clones) equals a
    # from-scratch full scan of head in a clean estate.
    dref = run_scan(url, commit=head, out=tmp_path / "ref", full=True)
    rref = json.loads((dref / "record.json").read_text(encoding="utf-8"))
    assert _agent_ids(r2) == _agent_ids(rref)
    # Byte-identical bar scan_health (incremental counters reflect the re-analysed
    # slice only) — the same exclusion the incremental-equivalence harness uses.
    n2, nref = normalize_record(dict(r2)), normalize_record(dict(rref))
    n2.pop("scan_health", None)
    nref.pop("scan_health", None)
    assert n2 == nref

    # The write-through DB holds both commits; bom_diff surfaces the PR's delta.
    diff = bom_diff(estate / "inventory.db", base, head)
    assert any("analytics" in a.lower() for a in diff["agents"]["added"])
    assert not diff["agents"]["removed"]
