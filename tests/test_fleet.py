"""SPEC-3 V7: fleet runner — repo list, failure tolerance, shared registry,
dataset + estate index."""

from __future__ import annotations

import json
from pathlib import Path

from aiscan.fleet import run_fleet
from tests.conftest import fixture_repo


def _repo_list(tmp_path: Path) -> Path:
    lines = [
        "# fleet fixture corpus",
        str(fixture_repo("derived_indicators")),
        str(fixture_repo("bespoke_wrapper")),
        str(fixture_repo("no_ai_clean")),
        str(tmp_path / "does-not-exist"),  # deliberately broken
    ]
    path = tmp_path / "repos.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestFleetRun:
    def test_end_to_end(self, tmp_path: Path) -> None:
        out = tmp_path / "fleet"
        result = run_fleet(_repo_list(tmp_path), out)

        # 3 scanned in input order, 1 failure logged + skipped, run completed.
        assert len(result.scanned) == 3
        assert len(result.failed) == 1
        assert "does-not-exist" in result.failed[0][0]

        # One dataset over the whole run.
        assert result.dataset_systems == 3
        assert (out / "inventory.db").is_file()
        assert (out / "csv" / "systems.csv").is_file()

        # Shared wrapper registry at the fleet root, populated by the
        # bespoke_wrapper repo's fixed-point classification.
        registry_text = (out / "org_registry.json").read_text(encoding="utf-8")
        assert "bank_ai" in registry_text  # reused by later repos in bigger runs

        # Estate index: one row per system, verdict badges, failure note.
        index = (out / "index.html").read_text(encoding="utf-8")
        assert index.count("<tr>") == 4  # header + 3 systems
        assert "AI DETECTED" in index and "NO AI" in index
        assert "does-not-exist" in index and "failed" in index
        assert "records/" in index and "inventory.html" in index
        assert "Content-Security-Policy" in index

        # Summary is machine-readable.
        summary = json.loads((out / "fleet_summary.json").read_text(encoding="utf-8"))
        assert summary["dataset_systems"] == 3
        assert len(summary["failed"]) == 1

    def test_registry_reused_across_repos(self, tmp_path: Path) -> None:
        # Same wrapper repo twice: the second scan must hit the registry
        # (its scan.log records wrapper classifications from source "registry").
        lines = [str(fixture_repo("bespoke_wrapper")), str(fixture_repo("bespoke_wrapper"))]
        repo_list = tmp_path / "repos.txt"
        repo_list.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = tmp_path / "fleet"
        result = run_fleet(repo_list, out)
        assert len(result.scanned) == 2
        registry_text = (out / "org_registry.json").read_text(encoding="utf-8")
        assert "bank_ai" in registry_text  # classified once, on disk for the fleet
        # Both scans still recovered the wrapper-backed agent.
        for _, scan_dir in result.scanned:
            record = json.loads((scan_dir / "record.json").read_text(encoding="utf-8"))
            assert record["agents"], "wrapper agent missing"
