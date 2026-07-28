"""Repository ingestion (SPEC §6.1): local paths and shallow git clones.

``git`` is the only subprocess the scanner ever runs, and checkouts are
read-only: we never install dependencies, never build, never execute repo code.
For a local path with ``--commit`` we do *not* mutate the working tree; the
requested commit is recorded but the tree is scanned as-is.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 300


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
    tail = url.rstrip("/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return re.sub(r"[^A-Za-z0-9._-]", "-", tail) or "repo"


def _looks_like_url(target: str) -> bool:
    return bool(
        re.match(r"^(https?://|git@|ssh://|git://)", target)
        or (target.count("/") == 1 and target.endswith(".git"))
    )


def ingest(
    target: str,
    commit: str | None,
    clone_parent: Path,
    logger: logging.Logger,
) -> IngestResult:
    """Materialise the scan target read-only and identify (root, url, commit)."""
    local = Path(target).expanduser()
    if local.is_dir():
        return _ingest_local(local, commit, logger)
    if _looks_like_url(target):
        return _ingest_url(target, commit, clone_parent, logger)
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


def _ingest_url(
    url: str, commit: str | None, clone_parent: Path, logger: logging.Logger
) -> IngestResult:
    name = _bundle_name_from_url(url)
    dest = clone_parent / name
    if (dest / ".git").is_dir():
        # SPEC-8: reuse an existing clone — fetch, don't re-clone. This is what
        # makes "skip when nothing was pushed" and incremental scans cheap; the
        # checkout stays read-only (no build, no dependency install).
        logger.info("reusing existing clone at %s (fetch, not re-clone)", dest)
        _git(["fetch", "--tags", "--force", "origin"], cwd=dest, logger=logger)
    else:
        if dest.exists():
            raise IngestError(f"clone destination exists but is not a git repo: {dest}")
        clone_parent.mkdir(parents=True, exist_ok=True)
        if _git(["clone", url, str(dest)], cwd=None, logger=logger) is None:
            raise IngestError(f"git clone failed for {url}")
    if commit:
        _git(["fetch", "origin", commit], cwd=dest, logger=logger)
        if _git(["checkout", "--detach", commit], cwd=dest, logger=logger) is None:
            logger.warning("checkout of %s failed; scanning current HEAD", commit[:12])
    else:
        # No pinned commit: advance to the remote default-branch head.
        remote_head = _git(["rev-parse", "origin/HEAD"], cwd=dest, logger=logger)
        if remote_head:
            _git(["checkout", "--detach", remote_head], cwd=dest, logger=logger)
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
