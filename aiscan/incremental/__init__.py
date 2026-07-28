"""Incremental & cached scanning (SPEC-8).

A full scan is whole-program abstract interpretation and costs minutes on a
large repo. Two savings, both resting on the scanner's per-commit determinism
(same commit -> byte-identical record):

* **Commit skip** — if the target commit was already scanned with this scanner
  version, the prior output is still valid; return it untouched.
* **Incremental analysis** — when a diff *is* pushed, re-analyse only the
  changed modules plus their reverse-dependency closure (callers/importers),
  reuse cached facts for the rest, and re-derive the record globally so every
  cross-module aggregation stays exact.

Soundness rests on three invariants enforced across this package and the
pipeline: resolution is always whole-program; the re-analysed set is a
*superset* of every module whose facts could change; and record assembly runs
over the full merged fact set every time. A hard version gate plus conservative
widening fall back to a full scan whenever any of that is uncertain.
"""

from __future__ import annotations

from aiscan.incremental.depgraph import affected_modules, build_dependency_edges
from aiscan.incremental.factcache import (
    CachedAnalysis,
    keep_unaffected_facts,
    keep_unaffected_findings,
    keep_unaffected_marks,
    load_cached_analysis,
    module_of_evidence,
    module_of_fact,
)
from aiscan.incremental.gate import (
    ANALYSIS_VERSION,
    GateDecision,
    can_incremental,
    should_skip,
)
from aiscan.incremental.manifest import Manifest, read_manifest, write_manifest

__all__ = [
    "ANALYSIS_VERSION",
    "CachedAnalysis",
    "GateDecision",
    "Manifest",
    "affected_modules",
    "build_dependency_edges",
    "can_incremental",
    "keep_unaffected_facts",
    "keep_unaffected_findings",
    "keep_unaffected_marks",
    "load_cached_analysis",
    "module_of_evidence",
    "module_of_fact",
    "read_manifest",
    "should_skip",
    "write_manifest",
]
