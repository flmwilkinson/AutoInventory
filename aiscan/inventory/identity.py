"""Record identity normalisation (SPEC-10 §5a).

The deterministic projection of a record used for ``(repo, commit)``
change-detection and golden comparison. Two scans of the *same* commit differ
only in non-deterministic fields — the wall-clock ``scanned_at``, the wall-clock
``scan_health.stage_ms`` timings, and (historically) an absolute ``org_pack``
path. ``identity_record`` strips exactly those, so the remainder is byte-stable
per commit and can serve as a content-identity anchor for a store keyed by
``(repo, commit)``.

This lives in the production package (not the test harness) so the store and the
golden compare share ONE definition of "the same scan" — previously this
normalisation existed only in ``tests/harness.py`` and never ran on the write
path, so the record as physically written was not actually deterministic.
"""

from __future__ import annotations

SCANNED_AT_SENTINEL = "<normalized>"


def identity_record(data: dict[str, object]) -> dict[str, object]:
    """Normalise the non-deterministic fields of a record dict in place and
    return it, so two scans of the same commit compare equal.

    Idempotent: applying it to an already-normalised record is a no-op.
    """
    prov = data.get("inventory_provenance")
    if isinstance(prov, dict):
        prov["scanned_at"] = SCANNED_AT_SENTINEL
        prov["org_pack"] = org_pack_ref(prov.get("org_pack"))
    health = data.get("scan_health")
    if isinstance(health, dict):
        health["stage_ms"] = {}
    return data


def org_pack_ref(value: object) -> object:
    """The basename of an org-pack path (or the value unchanged if not a str).

    An absolute path is both non-deterministic across machines and a minor
    path leak in a multi-tenant context; the basename is the only part that
    identifies which pack was used.
    """
    if isinstance(value, str):
        return value.replace("\\", "/").split("/")[-1]
    return value
