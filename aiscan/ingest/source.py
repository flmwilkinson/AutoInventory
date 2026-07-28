"""Read-only view of a materialised source tree (SPEC-10 §H).

Decouples the analysis passes from the local filesystem: the same discovery and
config-reading code reads from an ephemeral clone today (``LocalDirSource``) or a
GitHub blob/tree API later (a future ``GitHubApiSource``) behind one interface.
Readers accept ``Source | Path`` and coerce a bare ``repo_root`` via
:func:`as_source`, so every existing caller keeps working unchanged while a
service can inject a non-filesystem source. Git history (owner/provenance) is a
separate concern and stays on ``repo_root`` — it is not a file read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

# Directories never analysed: VCS internals, vendored deps, build/venv caches.
# The single skip policy for list_files(); callers apply any extra filtering
# (e.g. the source-file discovery also skips dot-directories).
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", "venv"})


@runtime_checkable
class Source(Protocol):
    """A materialised tree the pipeline reads. Paths are repo-relative POSIX."""

    def list_files(self) -> list[str]:
        """All files under the tree (skip-dirs excluded), sorted."""

    def read_text(self, rel: str) -> str:
        """UTF-8 text with ``errors='replace'`` (parsers tolerate any bytes)."""

    def read_bytes(self, rel: str) -> bytes | None:
        """Raw bytes, or None if the file is absent/unreadable."""

    def exists(self, rel: str) -> bool:
        """Whether ``rel`` is a readable file in the tree."""


class LocalDirSource:
    """A :class:`Source` backed by a local directory (a clone or a local path)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: list[str] | None = None

    def list_files(self) -> list[str]:
        if self._files is None:
            found: list[str] = []
            stack = [self.root]
            while stack:
                current = stack.pop()
                try:
                    entries = sorted(current.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.is_dir():
                        if entry.name not in SKIP_DIRS:
                            stack.append(entry)
                    elif entry.is_file():
                        found.append(entry.relative_to(self.root).as_posix())
            self._files = sorted(found)
        return self._files

    def read_text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, rel: str) -> bytes | None:
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()


def as_source(source: Source | Path) -> Source:
    """Coerce a ``repo_root`` Path to a :class:`LocalDirSource`; pass a Source
    through unchanged. Lets readers accept either without a caller migration."""
    return LocalDirSource(source) if isinstance(source, Path) else source
