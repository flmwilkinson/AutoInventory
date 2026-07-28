"""P0 exit gate: scanning never executes scanned code (SPEC §2 item 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from aiscan.cli import run_scan
from tests.conftest import fixture_repo

CANARIES = ("canary.txt", "canary2.txt")


def _no_canaries_under(root: Path) -> bool:
    return not any(list(root.rglob(c)) for c in CANARIES)


def test_adversarial_no_exec(tmp_path: Path) -> None:
    repo = fixture_repo("adversarial_no_exec")
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    os_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        out = run_scan(str(repo), out=tmp_path / "out")
    finally:
        os.chdir(os_cwd)

    assert _no_canaries_under(repo)
    assert _no_canaries_under(workdir)
    assert _no_canaries_under(tmp_path)

    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert record["scanned_commit"]
    assert record["scan_health"]["parse_errors"] == []
    # No AI signals in this repo: the triage gate stops before the deep pass,
    # which is itself part of the never-execute guarantee.
    assert record["no_ai_detected"] is True
    assert record["agents"] == []


TS_CANARIES = ("CANARY_TOPLEVEL", "CANARY_IIFE", "CANARY_POSTINSTALL")


def test_ts_no_exec(tmp_path: Path) -> None:
    """SPEC-4 W7: a TS repo with top-level side effects + a postinstall script
    is parsed, never run — no canary is ever created (npm is never invoked)."""
    repo = fixture_repo("ts_no_exec")
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    os_cwd = os.getcwd()
    os.chdir(workdir)
    try:
        out = run_scan(str(repo), out=tmp_path / "out")
    finally:
        os.chdir(os_cwd)

    for canary in TS_CANARIES:
        assert not list(repo.rglob(canary)), f"{canary} was created"
        assert not list(workdir.rglob(canary))
        assert not list(tmp_path.rglob(canary))

    record = json.loads((out / "record.json").read_text(encoding="utf-8"))
    # The code is still analysed (the agent is detected) — just never executed.
    assert record["ai_verdict"] == "ai_detected"
