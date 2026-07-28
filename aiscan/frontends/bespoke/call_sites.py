"""LLMCallSite emission (SPEC §6.7.3).

Sinks belonging to no agent become ``LLMCallSite`` facts → ``model_usages[]``
in the record: a bank needs "this repo calls gpt-4o in a batch job" even when
there is no agent. Sinks inside used wrappers are implementation details of
the wrapper, not usages. Test/__main__ sinks are inventoried and flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiscan.facts.models import AnyFact, FindingRecord, LLMCallSiteF, stable_id
from aiscan.ir.nodes import Span
from aiscan.sinks.engine import Sink


@dataclass(slots=True)
class CallSiteResult:
    facts: list[AnyFact] = field(default_factory=list)
    findings: list[FindingRecord] = field(default_factory=list)


def emit_call_sites(
    sinks: list[Sink],
    consumed_spans: frozenset[Span],
    member_spans: frozenset[Span] = frozenset(),
) -> CallSiteResult:
    result = CallSiteResult()
    for sink in sorted(sinks, key=lambda s: (s.span.file, s.span.line_start, s.span.col_start)):
        if sink.span in consumed_spans:
            continue
        result.facts.append(sink.model)
        if sink.prompt is not None:
            result.facts.append(sink.prompt)
        result.facts.append(
            LLMCallSiteF(
                id=stable_id("usage", str(sink.span), sink.kind),
                evidence=(str(sink.span),),
                confidence=sink.confidence,
                method=f"sink:{sink.kind}",
                source_files=(sink.site.path,),
                model_id=sink.model.id,
                prompt_id=sink.prompt.id if sink.prompt is not None else None,
                task=sink.task,
                # SPEC-5 §5: reachable from an agent's call closure.
                in_agent=sink.span in member_spans,
            )
        )
        if sink.site.in_tests or sink.site.under_main:
            location = "tests" if sink.site.in_tests else "__main__"
            result.findings.append(
                FindingRecord(
                    kind="llm_call_in_test_or_main",
                    evidence=(str(sink.span),),
                    detail=location,
                )
            )
    return result
