"""Cache-invalidation gate — decides skip / incremental / full.

The gate is the safety switch that keeps a cached BOM honest: whenever any input
the cache depends on has changed in a way the incremental path cannot reason
about, it forces a full scan. Failing safe (a redundant full scan) is always
preferred to a stale or partial record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiscan.incremental.manifest import Manifest, hash_files

# Bump on ANY change to analysis logic whose output a cached fact could encode:
# parsers, resolver, sink registry/shape, frontend packs/engines, side-effects,
# derive. A mismatch invalidates every cache and forces full scans until the
# next successful full scan re-seeds the manifest. (Precedent: the
# org_registry.json schema int, DECISIONS Z6.)
ANALYSIS_VERSION = 2

# Files whose content changes the resolution of *every* module — dependency
# versions and TS path aliases. A diff touching any of these escalates to full.
_DEPS_GLOBS = (
    "requirements*.txt",
    "poetry.lock",
    "uv.lock",
    "pyproject.toml",
)
_DEPS_NAMES = ("package.json", "package-lock.json", "pnpm-lock.yaml")
_TSCONFIG_NAME = "tsconfig.json"

_MAX_WALK = 2000  # bound the config walk on pathological trees


@dataclass(frozen=True)
class GateDecision:
    mode: str  # "skip" | "incremental" | "full"
    reason: str


def _walk_names(repo_root: Path, name: str) -> list[str]:
    out: list[str] = []
    for p in repo_root.rglob(name):
        if any(part in {".git", "node_modules", "__pycache__", "venv"} for part in p.parts):
            continue
        out.append(p.relative_to(repo_root).as_posix())
        if len(out) >= _MAX_WALK:
            break
    return out


def deps_files(repo_root: Path) -> list[str]:
    """Every dependency-manifest / lockfile path that feeds version resolution."""
    out: list[str] = []
    for pattern in _DEPS_GLOBS:
        out.extend(p.relative_to(repo_root).as_posix() for p in repo_root.glob(pattern))
    for name in _DEPS_NAMES:
        out.extend(_walk_names(repo_root, name))
    return sorted(set(out))


def tsconfig_files(repo_root: Path) -> list[str]:
    return sorted(set(_walk_names(repo_root, _TSCONFIG_NAME)))


def deps_hash(repo_root: Path) -> str:
    return hash_files(repo_root, deps_files(repo_root))


def tsconfig_hash(repo_root: Path) -> str:
    return hash_files(repo_root, tsconfig_files(repo_root))


def org_pack_hash(org_pack_path: Path | None) -> str:
    if org_pack_path is None:
        return ""
    return hash_files(org_pack_path.parent, [org_pack_path.name])


def is_global_signal(rel_path: str) -> bool:
    """True when a changed file alters global resolution inputs (deps/tsconfig)
    and therefore must escalate an incremental scan to a full one."""
    base = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    if base in _DEPS_NAMES or base == _TSCONFIG_NAME:
        return True
    if base == "poetry.lock" or base == "uv.lock" or base == "pyproject.toml":
        return True
    return base.startswith("requirements") and base.endswith(".txt")


def should_skip(
    manifest: Manifest | None,
    *,
    head_commit: str,
    scanner_version: str,
    rulepack_versions: dict[str, str],
    org_pack_digest: str,
    record_present: bool,
) -> bool:
    """Pre-parse decision: is the target commit already scanned with identical
    inputs? At an identical commit the repo bytes are identical, so only the
    scanner version, rulepacks, and the (external) org pack can differ.
    ``record_present`` is whether the store already holds that scan's record —
    supplied by the caller so the gate stays free of any storage backend."""
    if manifest is None:
        return False
    return (
        manifest.last_scanned_commit == head_commit
        and head_commit not in ("", "unknown", "unversioned")
        and manifest.scanner_version == scanner_version
        and manifest.analysis_version == ANALYSIS_VERSION
        and manifest.rulepack_versions == rulepack_versions
        and manifest.org_pack_hash == org_pack_digest
        and record_present
    )


def can_incremental(
    manifest: Manifest | None,
    *,
    scanner_version: str,
    rulepack_versions: dict[str, str],
    current_deps_hash: str,
    current_tsconfig_hash: str,
    current_org_hash: str,
    current_source_roots: list[str],
    changed_files: list[str],
    base_available: bool,
) -> GateDecision:
    """Post-parse decision. Returns ``incremental`` only when every global input
    matches the manifest and the diff touches no global-signal file; otherwise
    ``full`` with a reason. Never raises."""
    if manifest is None:
        return GateDecision("full", "no prior scan for this bundle")
    if not base_available:
        return GateDecision("full", "base commit unavailable for diff")
    if manifest.scanner_version != scanner_version:
        return GateDecision("full", "scanner version changed")
    if manifest.analysis_version != ANALYSIS_VERSION:
        return GateDecision("full", "analysis version changed")
    if manifest.rulepack_versions != rulepack_versions:
        return GateDecision("full", "rulepack versions changed")
    if manifest.deps_hash != current_deps_hash:
        return GateDecision("full", "dependency manifests changed")
    if manifest.tsconfig_hash != current_tsconfig_hash:
        return GateDecision("full", "tsconfig changed")
    if manifest.org_pack_hash != current_org_hash:
        return GateDecision("full", "org pack changed")
    if sorted(manifest.source_roots) != sorted(current_source_roots):
        return GateDecision("full", "source-root layout changed")
    if any(is_global_signal(f) for f in changed_files):
        return GateDecision("full", "diff touches a global resolution input")
    return GateDecision("incremental", f"{len(changed_files)} file(s) changed")
