"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_repo(name: str) -> Path:
    path = FIXTURES / name / "repo"
    assert path.is_dir(), f"missing fixture repo: {path}"
    return path


def all_fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if (p / "repo").is_dir())
