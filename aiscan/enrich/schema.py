"""Enrichment output contract (SPEC-2 §4.2) — schema-validated JSON only."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AiaCategory = Literal["prohibited", "high", "limited", "minimal"]


class Classification(BaseModel):
    """Node-type-dependent classification candidates (§4.2)."""

    model_config = ConfigDict(frozen=True)

    capability_class: str | None = None  # tools
    suggested_aia_risk_category: AiaCategory | None = None  # system
    data_domain: str | None = None  # tools


class EnrichmentResponse(BaseModel):
    """One grounded summarisation per node. Fields not applicable to the node
    type are simply left null (§4.2)."""

    model_config = ConfigDict(frozen=True)

    summary: str | None = None
    one_line: str | None = None
    # system-only extra summaries
    capability_summary: str | None = None
    data_interaction_summary: str | None = None
    human_oversight_summary: str | None = None
    # agent-only extra summaries
    responsibilities: str | None = None
    guardrails_summary: str | None = None
    # system-only: one-line purpose draft → lands as [G] purpose.candidate
    # (SPEC-3 §4.3), never as its value.
    purpose: str | None = None
    classification: Classification = Classification()
    grounded: bool = True
    insufficient_evidence_reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# Compact JSON-schema hint embedded in the prompt for json_object mode.
RESPONSE_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": ["string", "null"]},
        "one_line": {"type": ["string", "null"]},
        "capability_summary": {"type": ["string", "null"]},
        "data_interaction_summary": {"type": ["string", "null"]},
        "human_oversight_summary": {"type": ["string", "null"]},
        "responsibilities": {"type": ["string", "null"]},
        "guardrails_summary": {"type": ["string", "null"]},
        "purpose": {"type": ["string", "null"]},
        "classification": {
            "type": "object",
            "properties": {
                "capability_class": {"type": ["string", "null"]},
                "suggested_aia_risk_category": {
                    "enum": ["prohibited", "high", "limited", "minimal", None]
                },
                "data_domain": {"type": ["string", "null"]},
            },
        },
        "grounded": {"type": "boolean"},
        "insufficient_evidence_reason": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["summary", "grounded", "confidence"],
}

SCHEMA_HINT = json.dumps(RESPONSE_JSON_SCHEMA, sort_keys=True)
