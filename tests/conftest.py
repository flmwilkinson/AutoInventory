"""Shared test helpers."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# Fixtures are scanned from an isolated temp tree OUTSIDE any git working copy.
# The project itself is now a git repo, so scanning tests/fixtures/*/repo in
# place would make ingest/owner/ai_provenance walk up to the project's .git and
# report a real commit/owner/URL — breaking the "unversioned" goldens. Copying
# each fixture into a system-temp dir keeps golden scans VCS-state-independent
# (and hermetic in general), which is what the goldens were authored against.
_ISOLATED_ROOT: Path | None = None


def _isolated_root() -> Path:
    global _ISOLATED_ROOT
    if _ISOLATED_ROOT is None:
        _ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="aiscan-fixtures-"))
        atexit.register(shutil.rmtree, _ISOLATED_ROOT, ignore_errors=True)
    return _ISOLATED_ROOT


def fixture_repo(name: str) -> Path:
    src = FIXTURES / name / "repo"
    assert src.is_dir(), f"missing fixture repo: {src}"
    dest = _isolated_root() / name / "repo"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
    return dest


def all_fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if (p / "repo").is_dir())
