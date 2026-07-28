"""Repository ingestion (SPEC §6.1, SPEC-10 §2): local paths and ephemeral
blobless git clones.

``git`` is the only subprocess the scanner ever runs, and checkouts are
read-only: we never install dependencies, never build, never execute repo code.
A URL target is cloned into a temp dir (``--filter=blob:none``, full history)
that is deleted after the scan — there is no persistent clone cache. For a local
path with ``--commit`` we do *not* mutate the working tree; the requested commit
is recorded but the tree is scanned as-is.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_S = 300


def _force_writable(func: Any, path: Any, _exc: Any) -> None:
    """rmtree error handler: git packs are read-only, so chmod +w and retry —
    lets an ephemeral clone be removed on Windows."""
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
        func(path)


def _rmtree(path: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=_force_writable)


@dataclass(frozen=True, slots=True)
class IngestResult:
    repo_root: Path
    repo_url: str | None
    commit: str
    bundle_name: str
    # SPEC-8: a local working tree with uncommitted changes is not addressable
    # by its HEAD commit, so it must never be cached against or skipped.
    dirty: bool = False


class IngestError(Exception):
    """Raised when the scan target cannot be materialised at all."""


def _git(args: list[str], cwd: Path | None, logger: logging.Logger) -> str | None:
    """Run a git command; return stripped stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s failed: %s", " ".join(args[:2]), exc)
        return None
    if proc.returncode != 0:
        logger.debug("git %s exited %d: %s", " ".join(args[:2]), proc.returncode, proc.stderr)
        return None
    return proc.stdout.strip()


def _bundle_name_from_url(url: str) -> str:
    """A filesystem-safe ``owner__repo`` slug (SPEC-10 §5b).

    Keying by the URL basename alone collides across an org — ``org-a/api`` and
    ``org-b/api`` both become ``api``. Including the owner makes the bundle name
    a globally unique repo key (the ``repo`` half of the ``(repo, commit)`` key
    the cache/store are keyed by). scp-style ``git@host:owner/repo`` is
    normalised so the owner/repo tail is uniform; a host-like owner (contains a
    dot, e.g. ``github.com``) is dropped so a bare ``host/repo`` URL doesn't
    yield a ``github.com__repo`` slug."""
    parts = [p for p in url.rstrip("/").replace(":", "/").split("/") if p]
    tail = parts[-1] if parts else "repo"
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    owner = parts[-2] if len(parts) >= 2 else ""
    slug = f"{owner}__{tail}" if owner and "." not in owner else tail
    return re.sub(r"[^A-Za-z0-9._-]", "-", slug) or "repo"


def _looks_like_url(target: str) -> bool:
    return bool(
        re.match(r"^(https?://|git@|ssh://|git://|file://)", target)
        or (target.count("/") == 1 and target.endswith(".git"))
    )


class FetchedSource:
    """A materialised source tree with cleanup (SPEC-10 §2).

    ``repo_root`` is a real directory the pipeline reads. For a URL target it is
    an *ephemeral* clone that ``cleanup()`` deletes — nothing is persisted
    between scans (no ``_clones/`` cache). Use as a context manager to guarantee
    teardown even on error."""

    __slots__ = ("_cleanup_dir", "result")

    def __init__(self, result: IngestResult, cleanup_dir: Path | None) -> None:
        self.result = result
        self._cleanup_dir = cleanup_dir

    @property
    def repo_root(self) -> Path:
        return self.result.repo_root

    def cleanup(self) -> None:
        if self._cleanup_dir is not None:
            _rmtree(self._cleanup_dir)

    def __enter__(self) -> FetchedSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def fetch_source(
    target: str, commit: str | None, logger: logging.Logger
) -> FetchedSource:
    """Materialise ``target`` read-only and return it with cleanup (SPEC-10 §2).

    A local directory is scanned in place (no cleanup). A git URL is cloned
    *ephemerally* into a temp dir — blobless, full history — which ``cleanup()``
    removes; there is no persistent clone cache. Never installs dependencies,
    never builds, never executes repo code."""
    local = Path(target).expanduser()
    if local.is_dir():
        return FetchedSource(_ingest_local(local, commit, logger), None)
    if _looks_like_url(target):
        dest = Path(tempfile.mkdtemp(prefix="aiscan-src-"))
        try:
            result = _clone_ephemeral(target, commit, dest, logger)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return FetchedSource(result, dest)
    raise IngestError(f"target is neither an existing directory nor a git URL: {target}")


def _ingest_local(root: Path, commit: str | None, logger: logging.Logger) -> IngestResult:
    root = root.resolve()
    head = _git(["rev-parse", "HEAD"], cwd=root, logger=logger)
    url = _git(["remote", "get-url", "origin"], cwd=root, logger=logger)
    # A non-empty porcelain status means uncommitted edits: the tree's content
    # is not what HEAD names, so it cannot be cached against.
    status = _git(["status", "--porcelain"], cwd=root, logger=logger)
    dirty = bool(status) if head else True
    if commit and head and not head.startswith(commit):
        logger.warning(
            "local path scanned as-is at HEAD %s; requested commit %s not checked out "
            "(checkouts of local paths would mutate the working tree)",
            head[:12],
            commit[:12],
        )
    return IngestResult(
        repo_root=root,
        repo_url=url,
        commit=head or "unversioned",
        bundle_name=root.name,
        dirty=dirty,
    )


def _clone_ephemeral(
    url: str, commit: str | None, dest: Path, logger: logging.Logger
) -> IngestResult:
    """Clone ``url`` into the (empty) temp dir ``dest`` for a single scan.

    Blobless (``--filter=blob:none``): the full commit/tree history is present —
    so ``git log`` provenance (owner/ai_provenance) and ``base..head`` diffs work
    — while file blobs are fetched lazily on demand. Never ``--depth``: a shallow
    clone would truncate the history those provenance fields read and break
    byte-identity vs a full scan (SPEC-10 §2). The clone is discarded after the
    scan by ``FetchedSource.cleanup``."""
    name = _bundle_name_from_url(url)
    if _git(["clone", "--filter=blob:none", url, str(dest)], cwd=None, logger=logger) is None:
        raise IngestError(f"git clone failed for {url}")
    if commit:
        # A pinned commit may be off the default branch — make sure it is present,
        # then check it out. Base availability for diffs comes from full history.
        _git(["fetch", "origin", commit], cwd=dest, logger=logger)
        if _git(["checkout", "--detach", commit], cwd=dest, logger=logger) is None:
            logger.warning("checkout of %s failed; scanning current HEAD", commit[:12])
    head = _git(["rev-parse", "HEAD"], cwd=dest, logger=logger)
    return IngestResult(
        repo_root=dest,
        repo_url=url,
        commit=head or "unknown",
        bundle_name=name,
    )


def changed_files(
    repo_root: Path, base: str, head: str, logger: logging.Logger
) -> list[str] | None:
    """Repo-relative POSIX paths that differ between two commits (added,
    modified, deleted, renamed — both sides of a rename are reported). None when
    the diff cannot be computed (e.g. base commit absent), signalling the caller
    to fall back to a full scan."""
    out = _git(
        ["diff", "--name-only", "--no-renames", f"{base}", f"{head}"],
        cwd=repo_root,
        logger=logger,
    )
    if out is None:
        return None
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]
