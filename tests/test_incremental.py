"""SPEC-8 incremental scanning — the load-bearing equivalence guarantee.

For a battery of representative diffs, an incremental rescan of ``base -> head``
must produce the *same BOM* as a from-scratch full scan of ``head``. The record
is compared minus ``scan_health`` (diagnostics — sink/resolver counters reflect
only the re-analysed slice and are not part of the BOM). If any diff diverges,
the incremental path is unsound for it and must fall back to a full scan.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aiscan.cli import run_scan
from tests.harness import normalize_record

# A small multi-module agentic repo (consumer style: `agents` is third-party).
# The pyproject declares openai-agents so the triage gate proceeds; it is
# constant across every diff (so deps_hash matches and incremental is allowed).
BASE_REPO: dict[str, str] = {
    "pyproject.toml": (
        "[project]\n"
        "name = 'demo'\n"
        "version = '0.1.0'\n"
        "dependencies = ['openai-agents>=0.1']\n"
    ),
    "app/planner.py": (
        "from agents import Agent\n\n"
        "planner = Agent(\n"
        "    name='Planner',\n"
        "    model='gpt-4o-mini',\n"
        "    instructions='Plan the work.',\n"
        ")\n"
    ),
    "app/writer.py": (
        "from agents import Agent\n\n"
        "writer = Agent(name='Writer', model='gpt-4o', instructions='Write it up.')\n"
    ),
    "app/tools.py": (
        "from agents import function_tool\n\n\n"
        "@function_tool\n"
        "def search(query: str) -> str:\n"
        "    return query\n"
    ),
    "app/main.py": (
        "from agents import Runner\n"
        "from app.planner import planner\n\n"
        "Runner.run(planner)\n"
    ),
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    # Sync the tree to exactly `files`: remove tracked source files that are no
    # longer present (so a deletion diff is real), then (re)write the rest.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.split()
    for rel in tracked:
        if rel not in files and (repo / rel).is_file():
            (repo / rel).unlink()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _init_repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    base = _commit(repo, files, "base")
    return repo, base


def _bom_view(scan_dir: Path) -> dict:
    """The BOM as compared for equivalence: the record minus diagnostics-only
    scan_health, plus the ADG graph. Timestamps normalised. facts.jsonl is
    intentionally not compared — it may differ in fields the record never
    consumes (e.g. the unused AgentDefF.entrypoint flag); the record + graph are
    the BOM."""
    record = json.loads((scan_dir / "record.json").read_text(encoding="utf-8"))
    record = normalize_record(record)
    record.pop("scan_health", None)
    graph = json.loads((scan_dir / "graph.json").read_text(encoding="utf-8"))
    return {"record": record, "graph": graph}


# Each case mutates BASE_REPO into the head state.
def _change_model(files: dict[str, str]) -> dict[str, str]:
    files["app/planner.py"] = files["app/planner.py"].replace("gpt-4o-mini", "o3-mini")
    return files


def _add_tool_to_agent(files: dict[str, str]) -> dict[str, str]:
    files["app/planner.py"] = (
        "from agents import Agent\n"
        "from app.tools import search\n\n"
        "planner = Agent(\n"
        "    name='Planner',\n"
        "    model='gpt-4o-mini',\n"
        "    instructions='Plan the work.',\n"
        "    tools=[search],\n"
        ")\n"
    )
    return files


def _add_new_agent_module(files: dict[str, str]) -> dict[str, str]:
    files["app/reviewer.py"] = (
        "from agents import Agent\n\n"
        "reviewer = Agent(name='Reviewer', model='gpt-4o', instructions='Review.')\n"
    )
    return files


def _add_unrelated_file(files: dict[str, str]) -> dict[str, str]:
    files["README.md"] = "# docs\nSome docs, no code.\n"
    return files


def _add_handoff_cross_module(files: dict[str, str]) -> dict[str, str]:
    # planner hands off to writer (defined in another module) — the cross-module
    # structural case the post-passes must resolve against carried facts.
    files["app/planner.py"] = (
        "from agents import Agent\n"
        "from app.writer import writer\n\n"
        "planner = Agent(\n"
        "    name='Planner',\n"
        "    model='gpt-4o-mini',\n"
        "    instructions='Plan the work.',\n"
        "    handoffs=[writer],\n"
        ")\n"
    )
    return files


def _change_entrypoint_target(files: dict[str, str]) -> dict[str, str]:
    # main now runs the writer instead of the planner — moves an entrypoint mark
    # across modules.
    files["app/main.py"] = (
        "from agents import Runner\n"
        "from app.writer import writer\n\n"
        "Runner.run(writer)\n"
    )
    return files


def _change_prompt(files: dict[str, str]) -> dict[str, str]:
    files["app/writer.py"] = files["app/writer.py"].replace(
        "Write it up.", "Write a detailed report."
    )
    return files


def _delete_agent_module(files: dict[str, str]) -> dict[str, str]:
    # Deleting a source module escalates to a full scan (a dangling cross-module
    # reference cannot be reasoned about incrementally) — must still equal full.
    del files["app/writer.py"]
    return files


CASES = {
    "change_model": _change_model,
    "add_tool_to_agent": _add_tool_to_agent,
    "add_new_agent_module": _add_new_agent_module,
    "add_unrelated_file": _add_unrelated_file,
    "add_handoff_cross_module": _add_handoff_cross_module,
    "change_entrypoint_target": _change_entrypoint_target,
    "change_prompt": _change_prompt,
    "delete_agent_module": _delete_agent_module,
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_incremental_matches_full(case: str, tmp_path: Path) -> None:
    mutate = CASES[case]
    repo, _base = _init_repo(tmp_path, dict(BASE_REPO))

    out = tmp_path / "out"
    # Seed: full scan at base -> writes the manifest + fact cache.
    run_scan(str(repo), out=out)

    # Advance the working tree to head and commit.
    _commit(repo, mutate(dict(BASE_REPO)), "head")

    # Incremental rescan (auto: manifest present, diff base..head).
    incr_dir = run_scan(str(repo), out=out)
    # Reference: a from-scratch full scan of head in a clean out dir.
    ref_dir = run_scan(str(repo), out=tmp_path / "ref", full=True)

    incr = _bom_view(incr_dir)
    ref = _bom_view(ref_dir)
    assert incr == ref, f"incremental BOM diverged from full for diff '{case}'"


def _manifest(**overrides: object):
    from aiscan.incremental import ANALYSIS_VERSION, Manifest

    defaults = dict(
        bundle="demo",
        last_scanned_commit="abc123",
        scan_out_dir="/out/demo-abc123",
        scanner_version="aiscan 0.1.0",
        analysis_version=ANALYSIS_VERSION,
        rulepack_versions={"openai-agents": "0.1"},
        source_roots=["src"],
        deps_hash="d0",
        tsconfig_hash="t0",
        org_pack_hash="",
    )
    defaults.update(overrides)
    return Manifest(**defaults)  # type: ignore[arg-type]


class TestGate:
    def test_is_global_signal(self) -> None:
        from aiscan.incremental.gate import is_global_signal

        assert is_global_signal("uv.lock")
        assert is_global_signal("services/api/package.json")
        assert is_global_signal("tsconfig.json")
        assert is_global_signal("requirements-dev.txt")
        assert not is_global_signal("app/agent.py")
        assert not is_global_signal("README.md")

    def test_should_skip_matches(self, tmp_path: Path) -> None:
        from aiscan.incremental.gate import should_skip

        (tmp_path / "record.json").write_text("{}", encoding="utf-8")
        m = _manifest(last_scanned_commit="deadbeef", scan_out_dir=str(tmp_path))
        assert should_skip(
            m,
            head_commit="deadbeef",
            scanner_version="aiscan 0.1.0",
            rulepack_versions={"openai-agents": "0.1"},
            org_pack_digest="",
            scan_out_dir=tmp_path,
        )

    def test_no_skip_on_version_or_commit_change(self, tmp_path: Path) -> None:
        from aiscan.incremental.gate import should_skip

        (tmp_path / "record.json").write_text("{}", encoding="utf-8")
        base = dict(
            head_commit="deadbeef",
            scanner_version="aiscan 0.1.0",
            rulepack_versions={"openai-agents": "0.1"},
            org_pack_digest="",
            scan_out_dir=tmp_path,
        )
        m = _manifest(last_scanned_commit="deadbeef", scan_out_dir=str(tmp_path))
        assert not should_skip(m, **{**base, "head_commit": "other"})
        assert not should_skip(m, **{**base, "scanner_version": "aiscan 0.2.0"})
        assert not should_skip(m, **{**base, "org_pack_digest": "changed"})

    def test_can_incremental_gates(self) -> None:
        from aiscan.incremental.gate import can_incremental

        common = dict(
            scanner_version="aiscan 0.1.0",
            rulepack_versions={"openai-agents": "0.1"},
            current_deps_hash="d0",
            current_tsconfig_hash="t0",
            current_org_hash="",
            current_source_roots=["src"],
            base_available=True,
        )
        m = _manifest()
        assert can_incremental(m, changed_files=["app/a.py"], **common).mode == "incremental"
        assert can_incremental(None, changed_files=["app/a.py"], **common).mode == "full"
        assert (
            can_incremental(m, changed_files=["uv.lock"], **common).mode == "full"
        )  # global signal
        assert (
            can_incremental(
                m, changed_files=["app/a.py"], **{**common, "current_deps_hash": "d1"}
            ).mode
            == "full"
        )  # deps changed
        assert (
            can_incremental(
                m, changed_files=["app/a.py"], **{**common, "base_available": False}
            ).mode
            == "full"
        )  # no base to diff


def test_incremental_actually_ran(tmp_path: Path) -> None:
    """Guard against the test silently exercising only full scans: the second
    scan must take the incremental path (fewer sinks analysed than a full)."""
    repo, _base = _init_repo(tmp_path, dict(BASE_REPO))
    out = tmp_path / "out"
    run_scan(str(repo), out=out)
    _commit(repo, _change_model(dict(BASE_REPO)), "head")
    incr_dir = run_scan(str(repo), out=out)
    log = (incr_dir / "scan.log").read_text(encoding="utf-8")
    assert "incremental:" in log, "expected the incremental path to run"


def test_incremental_duplicate_agent_names(tmp_path: Path) -> None:
    """Regression for the agent-id collision bug: agent ids are
    collision-disambiguated (first same-named agent gets a bare id, others get
    ``@file:line``). A full scan sees all of them; an incremental scan sees only
    the affected module and must still assign the same id. Two modules each
    define an 'Assistant'; changing one must not drop or merge either."""
    repo_files = {
        "pyproject.toml": (
            "[project]\nname='demo'\nversion='0.1'\ndependencies=['openai-agents']\n"
        ),
        "app/alpha.py": (
            "from agents import Agent\n\n"
            "assistant = Agent(name='Assistant', model='gpt-4o-mini', instructions='A')\n"
        ),
        "app/beta.py": (
            "from agents import Agent\n\n"
            "assistant = Agent(name='Assistant', model='gpt-4o', instructions='B')\n"
        ),
    }
    repo, _base = _init_repo(tmp_path, repo_files)
    out = tmp_path / "out"
    run_scan(str(repo), out=out)  # seed

    head = dict(repo_files)
    head["app/beta.py"] = head["app/beta.py"].replace("gpt-4o'", "o3-mini'")
    _commit(repo, head, "head")
    incr_dir = run_scan(str(repo), out=out)
    assert "incremental:" in (incr_dir / "scan.log").read_text(encoding="utf-8")
    ref = run_scan(str(repo), out=tmp_path / "ref", full=True)
    assert _bom_view(incr_dir) == _bom_view(ref)


def test_chained_incrementals_match_full(tmp_path: Path) -> None:
    """An incremental scan must write a cache complete enough for the *next*
    incremental: base -> mid -> head, both hops incremental, must still equal a
    full scan of head. This guards the 'merged fact/mark set is persisted'
    property that makes chained CI runs sound."""
    repo, _base = _init_repo(tmp_path, dict(BASE_REPO))
    out = tmp_path / "out"
    run_scan(str(repo), out=out)  # seed at base

    mid = _add_tool_to_agent(dict(BASE_REPO))
    _commit(repo, mid, "mid")
    d_mid = run_scan(str(repo), out=out)  # incremental #1
    assert "incremental:" in (d_mid / "scan.log").read_text(encoding="utf-8")

    head = _change_model(dict(mid))  # start from mid, also change the model
    _commit(repo, head, "head")
    d_head = run_scan(str(repo), out=out)  # incremental #2 (from the incr cache)
    assert "incremental:" in (d_head / "scan.log").read_text(encoding="utf-8")

    ref = run_scan(str(repo), out=tmp_path / "ref", full=True)
    assert _bom_view(d_head) == _bom_view(ref)
