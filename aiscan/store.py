"""Scan state & artifact store (SPEC-10 §3).

One interface, keyed by ``(bundle, commit)``, over everything that must survive a
scan: the per-bundle manifest, a prior scan's cached facts (for incremental), and
the emitted artifacts. The default :class:`LocalDirStore` reproduces today's
on-disk layout byte-for-byte, so a service can later swap a DB / object-store
backend behind the same protocol without touching the pipeline. The incremental
core no longer addresses prior state by an absolute path baked into the manifest;
it asks the store for it by ``(bundle, commit)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from aiscan.incremental.factcache import CachedAnalysis, load_cached_analysis
from aiscan.incremental.manifest import (
    Manifest,
    manifest_path,
    read_manifest,
    write_manifest,
)
from aiscan.inventory.emit import write_artifacts

if TYPE_CHECKING:
    from aiscan.core import ScanResult


def commit_tag(commit: str) -> str:
    """The short commit tag used in a scan's addressable location."""
    return commit[:8] if commit else "unknown"


class StateStore(Protocol):
    """Backs a scan's cross-run state, keyed by ``(bundle, commit)``."""

    def get_manifest(self, bundle: str) -> Manifest | None: ...
    def put_manifest(self, manifest: Manifest) -> None: ...
    def has_record(self, bundle: str, commit: str) -> bool: ...
    def get_prior_facts(self, bundle: str, commit: str) -> CachedAnalysis | None: ...
    def location(self, bundle: str, commit: str) -> Path: ...
    def put_artifacts(self, bundle: str, commit: str, result: ScanResult) -> Path: ...


class LocalDirStore:
    """The default filesystem store: manifests under ``<out>/_cache/<bundle>.json``
    and each scan's artifacts under ``<out>/<bundle>-<commit8>/`` — exactly the
    layout the CLI produced before the seam existed."""

    def __init__(self, out_root: Path) -> None:
        self.out_root = out_root
        self._cache_dir = out_root / "_cache"

    def location(self, bundle: str, commit: str) -> Path:
        return self.out_root / f"{bundle}-{commit_tag(commit)}"

    def get_manifest(self, bundle: str) -> Manifest | None:
        return read_manifest(manifest_path(self._cache_dir, bundle))

    def put_manifest(self, manifest: Manifest) -> None:
        write_manifest(manifest_path(self._cache_dir, manifest.bundle), manifest)

    def has_record(self, bundle: str, commit: str) -> bool:
        return (self.location(bundle, commit) / "record.json").is_file()

    def get_prior_facts(self, bundle: str, commit: str) -> CachedAnalysis | None:
        return load_cached_analysis(self.location(bundle, commit))

    def put_artifacts(self, bundle: str, commit: str, result: ScanResult) -> Path:
        out = self.location(bundle, commit)
        write_artifacts(
            out,
            result.record,
            result.fact_lines,
            result.graph,
            analysis_finding_lines=result.analysis_finding_lines,
            entrypoint_marks=result.entrypoint_marks,
        )
        return out
