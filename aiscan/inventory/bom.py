"""AI-BOM builder (SPEC-3 §4.1): the dependency dimension of the inventory.

Pure function of the triage signals, lockfile versions, and the set of
top-level packages the code actually imports. A row with ``used: false`` is
the classic dormant dependency — declared in a manifest, never imported —
which is exactly the ``ai_signals_only`` story.
"""

from __future__ import annotations

from aiscan.context import OrgPack
from aiscan.inventory.schema import AiDependency


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def build_ai_bom(
    triage: dict[str, list[str]],
    versions: dict[str, str],
    imported_roots: frozenset[str],
    org_pack: OrgPack,
    npm_versions: dict[str, str] | None = None,
) -> tuple[AiDependency, ...]:
    """Rows = manifest-declared AI packages plus imported org wrapper roots."""
    npm_versions = npm_versions or {}
    imported_raw = set(imported_roots)
    imported_norm = {_norm(r) for r in imported_roots}
    evidence: dict[str, set[str]] = {}
    names: dict[str, str] = {}

    for signal in triage.get("manifest_ai_package", ()):
        path, _, pkg = signal.rpartition(":")
        if not pkg:
            continue
        key = _norm(pkg)
        names.setdefault(key, pkg)
        evidence.setdefault(key, set()).add(path)

    for wrapper in org_pack.known_wrapper_packages:
        root = wrapper.fqname.split(".")[0]
        key = _norm(root)
        if key in names or key not in imported_norm:
            continue
        names[key] = root
        evidence[key] = set()

    rows = [
        AiDependency(
            package=names[key],
            version=versions.get(key),
            source=min(evidence[key]) if evidence[key] else None,
            used=key in imported_norm,
            evidence=tuple(sorted(evidence[key])),
        )
        for key in names
    ]

    # npm AI packages (package.json dependency keys). SPEC-4 W6: TS/JS is now
    # analysed, so `used` reflects a real import (the module graph knows).
    js_evidence: dict[str, set[str]] = {}
    for signal in triage.get("manifest_ai_package_js", ()):
        path, _, pkg = signal.rpartition(":")
        if pkg:
            js_evidence.setdefault(pkg, set()).add(path)
    for pkg in sorted(js_evidence):
        rows.append(
            AiDependency(
                package=pkg,
                version=npm_versions.get(pkg),
                source=min(js_evidence[pkg]) if js_evidence[pkg] else None,
                used=pkg in imported_raw,
                ecosystem="npm",
                evidence=tuple(sorted(js_evidence[pkg])),
            )
        )
    return tuple(sorted(rows, key=lambda d: (d.package, d.ecosystem)))
