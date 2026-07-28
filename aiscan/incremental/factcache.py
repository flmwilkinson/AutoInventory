"""Load, partition, and merge the prior scan's facts and analysis findings.

The prior scan's ``facts.jsonl`` and ``analysis_findings.jsonl`` (both written by
``write_artifacts``) are the cache. On an incremental run we keep the entries
belonging to *unaffected* modules and let the analysis stages regenerate the
rest, then reassemble the record from the merged set. Facts and findings are
attributed to a module through their source path, so partitioning is exact;
anything not attributable to an in-repo module (config-file facts, secret
findings) is never cached and is always regenerated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aiscan.facts.models import (
    AgentCandidateF,
    AgentDefF,
    AttachPolicyF,
    BindMCPF,
    BindModelF,
    BindPromptF,
    BindStateF,
    BindToolF,
    FindingRecord,
    InterAgentMsgF,
    LLMCallSiteF,
    MCPDefF,
    ModelRefF,
    PolicyDefF,
    PromptDefF,
    StateDefF,
    ToolDefF,
    TransferF,
)

_FACT_TYPES = {
    cls.__name__: cls
    for cls in (
        AgentDefF,
        AgentCandidateF,
        PromptDefF,
        ModelRefF,
        ToolDefF,
        MCPDefF,
        StateDefF,
        PolicyDefF,
        LLMCallSiteF,
        BindModelF,
        BindPromptF,
        BindToolF,
        BindMCPF,
        BindStateF,
        AttachPolicyF,
        TransferF,
        InterAgentMsgF,
    )
}

_ANALYSIS_FINDINGS = "analysis_findings.jsonl"
_ENTRYPOINT_MARKS = "entrypoint_marks.jsonl"


@dataclass(frozen=True)
class CachedAnalysis:
    """The prior scan's raw analysis output, before global re-derivation."""

    facts: list[object]
    analysis_findings: list[FindingRecord]
    # (framework, agent name, source module) — entrypoints are cross-module.
    entrypoint_marks: list[tuple[str, str, str]]


def _path_of(evidence_or_source: str) -> str:
    """The bare file path from a ``file:line`` evidence span or plain path."""
    text = evidence_or_source.replace("\\", "/")
    # Evidence spans are ``path:line`` or ``path:start-end``; a path may itself
    # contain no colon on POSIX, so split on the last colon only when the tail
    # is line-shaped.
    head, sep, tail = text.rpartition(":")
    if sep and (tail.isdigit() or ("-" in tail and tail.replace("-", "").isdigit())):
        return head
    return text


def module_of_fact(fact: object, name_by_path: dict[str, str]) -> str | None:
    """Module name a fact belongs to, via its first source file. None when the
    source is not an in-repo module (config files, generated facts)."""
    source_files = getattr(fact, "source_files", ()) or ()
    for src in source_files:
        name = name_by_path.get(_path_of(src))
        if name is not None:
            return name
    evidence = getattr(fact, "evidence", ()) or ()
    for ev in evidence:
        name = name_by_path.get(_path_of(ev))
        if name is not None:
            return name
    return None


def module_of_evidence(
    finding: FindingRecord, name_by_path: dict[str, str]
) -> str | None:
    for ev in finding.evidence:
        name = name_by_path.get(_path_of(ev))
        if name is not None:
            return name
    return None


def load_cached_analysis(scan_out_dir: Path) -> CachedAnalysis | None:
    """Reconstruct the prior scan's facts + analysis findings. None if the
    facts file is missing (treated as no cache -> full scan)."""
    facts_path = scan_out_dir / "facts.jsonl"
    if not facts_path.is_file():
        return None
    facts: list[object] = []
    for line in facts_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            return None
        cls = _FACT_TYPES.get(str(payload.pop("fact_type", "")))
        if cls is None:
            return None
        try:
            facts.append(cls.model_validate(payload))
        except (ValueError, TypeError):
            return None
    findings: list[FindingRecord] = []
    findings_path = scan_out_dir / _ANALYSIS_FINDINGS
    if findings_path.is_file():
        for line in findings_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                findings.append(FindingRecord.model_validate_json(line))
            except ValueError:
                return None
    marks: list[tuple[str, str, str]] = []
    marks_path = scan_out_dir / _ENTRYPOINT_MARKS
    if marks_path.is_file():
        for line in marks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                fw, name, module = json.loads(line)
            except (ValueError, TypeError):
                return None
            marks.append((str(fw), str(name), str(module)))
    return CachedAnalysis(
        facts=facts, analysis_findings=findings, entrypoint_marks=marks
    )


def keep_unaffected_marks(
    cached: list[tuple[str, str, str]], affected: set[str]
) -> list[tuple[str, str, str]]:
    """Entrypoint marks from modules not re-analysed this run (the affected
    modules' marks are regenerated fresh)."""
    return [m for m in cached if m[2] not in affected]


def keep_unaffected_facts(
    cached: list[object],
    affected: set[str],
    name_by_path: dict[str, str],
    changed_files: set[str],
) -> list[object]:
    """Cached facts to carry forward: those whose module is unaffected and whose
    source file is unchanged. Facts not attributable to an in-repo module are
    dropped (regenerated by the always-global config/secret passes)."""
    kept: list[object] = []
    for fact in cached:
        module = module_of_fact(fact, name_by_path)
        if module is None or module in affected:
            continue
        srcs = {_path_of(s) for s in getattr(fact, "source_files", ()) or ()}
        srcs |= {_path_of(e) for e in getattr(fact, "evidence", ()) or ()}
        if srcs & changed_files:
            continue
        kept.append(fact)
    return kept


def keep_unaffected_findings(
    cached: list[FindingRecord],
    affected: set[str],
    name_by_path: dict[str, str],
    changed_files: set[str],
) -> list[FindingRecord]:
    kept: list[FindingRecord] = []
    for finding in cached:
        module = module_of_evidence(finding, name_by_path)
        if module is None or module in affected:
            continue
        srcs = {_path_of(e) for e in finding.evidence}
        if srcs & changed_files:
            continue
        kept.append(finding)
    return kept


def analysis_findings_jsonl(findings: list[FindingRecord]) -> list[str]:
    """Serialise analysis findings for the cache (sorted, byte-stable)."""
    return sorted(
        {f.model_dump_json() for f in findings}
    )
