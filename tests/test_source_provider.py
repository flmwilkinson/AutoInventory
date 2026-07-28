"""SPEC-10 §2: the ephemeral source provider — blobless clone into a temp dir,
full-history diff, cleanup on exit. Uses a local ``file://`` origin (no network).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from aiscan.ingest.git import changed_files, fetch_source

_LOG = logging.getLogger("test-source")


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _make_origin(root: Path) -> tuple[str, str]:
    """A two-commit git repo that permits partial (blobless) clone."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "uploadpack.allowFilter", "true"], root)  # enable --filter clone
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "one"], root)
    base = _git(["rev-parse", "HEAD"], root)
    (root / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "two"], root)
    head = _git(["rev-parse", "HEAD"], root)
    return base, head


def test_ephemeral_clone_materialises_full_tree_diffs_and_cleans_up(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    base, head = _make_origin(origin)

    with fetch_source(origin.as_uri(), None, _LOG) as source:
        root = source.repo_root
        # Full head tree is materialised (whole-program invariant), not just a diff.
        assert (root / "a.py").is_file()
        assert (root / "b.py").is_file()
        assert source.result.commit == head
        # Full history is present (blobless, not shallow) -> base..head diff works.
        assert changed_files(root, base, head, _LOG) == ["b.py"]
        clone_dir = root

    # The ephemeral clone is gone once the context exits — nothing persisted.
    assert not clone_dir.exists()


def test_pinned_commit_is_checked_out(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    base, _head = _make_origin(origin)
    with fetch_source(origin.as_uri(), base, _LOG) as source:
        assert source.result.commit == base
        # b.py was added in the second commit; at base it must be absent.
        assert not (source.repo_root / "b.py").exists()


def test_local_dir_scanned_in_place_not_cleaned(tmp_path: Path) -> None:
    local = tmp_path / "local"
    local.mkdir()
    (local / "x.py").write_text("z = 3\n", encoding="utf-8")
    with fetch_source(str(local), None, _LOG) as source:
        assert source.repo_root == local.resolve()
        assert source.result.commit == "unversioned"
    assert local.exists()  # a local dir is never cleaned up


def test_unknown_target_raises(tmp_path: Path) -> None:
    from aiscan.ingest.git import IngestError

    with pytest.raises(IngestError):
        fetch_source(str(tmp_path / "does-not-exist"), None, _LOG)
