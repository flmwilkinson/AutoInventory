"""Per-bundle scan manifest — what was last scanned, and with what.

One JSON file per bundle under ``<out>/_cache/<bundle>.json``. It records the
last-scanned commit and every input whose change would invalidate the cache, so
a rescan can decide between *skip*, *incremental*, and *full* without re-running
the pipeline. Written on every successful scan; read at the start of the next.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Manifest:
    """Snapshot of the inputs behind one scan of a bundle."""

    bundle: str
    last_scanned_commit: str
    scan_out_dir: str
    scanner_version: str
    analysis_version: int
    # The gate compares these against the current environment; any mismatch
    # forces a full rescan (a cached result must never outlive its inputs).
    rulepack_versions: dict[str, str] = field(default_factory=dict)
    source_roots: list[str] = field(default_factory=list)
    deps_hash: str = ""
    tsconfig_hash: str = ""
    org_pack_hash: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def manifest_path(cache_dir: Path, bundle: str) -> Path:
    """Location of a bundle's manifest under the cache directory."""
    safe = bundle.replace("/", "-").replace("\\", "-")
    return cache_dir / f"{safe}.json"


def read_manifest(path: Path) -> Manifest | None:
    """Load a manifest, or None if absent/unreadable/malformed (never raises —
    a missing or corrupt manifest simply means 'no cache', i.e. full scan)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Manifest(
            bundle=str(data["bundle"]),
            last_scanned_commit=str(data["last_scanned_commit"]),
            scan_out_dir=str(data["scan_out_dir"]),
            scanner_version=str(data["scanner_version"]),
            analysis_version=int(data["analysis_version"]),
            rulepack_versions=dict(data.get("rulepack_versions") or {}),
            source_roots=list(data.get("source_roots") or []),
            deps_hash=str(data.get("deps_hash", "")),
            tsconfig_hash=str(data.get("tsconfig_hash", "")),
            org_pack_hash=str(data.get("org_pack_hash", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Persist a manifest (creates the cache directory as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.to_json(), encoding="utf-8")


def hash_files(repo_root: Path, rel_paths: list[str]) -> str:
    """Stable digest of the given files' contents (missing files contribute
    their name only), for the manifest's global-input hashes. Deterministic:
    inputs are sorted, and both path and bytes feed the digest."""
    h = hashlib.sha256()
    for rel in sorted(set(rel_paths)):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        fp = repo_root / rel
        try:
            h.update(fp.read_bytes())
        except OSError:
            h.update(b"<absent>")
        h.update(b"\0")
    return h.hexdigest()[:16]
