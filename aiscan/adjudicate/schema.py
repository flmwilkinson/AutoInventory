"""Adjudicator response schema (SPEC §6.10) — schema-validated JSON only."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdjudicatedAgent(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    model_expr: str | None = None
    prompt_ref: str | None = None
    tools: tuple[str, ...] = ()


class WrapperVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_llm_wrapper: bool
    attribution: Literal["fixed", "passthrough", "default"] = "default"


class AdjudicationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_agent: bool
    confidence: float = Field(ge=0.0, le=1.0)
    agents: tuple[AdjudicatedAgent, ...] = ()
    wrapper: WrapperVerdict | None = None
    abstain: bool = False
    rationale: str = Field(default="", max_length=400)


RESPONSE_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "is_agent": {"type": "boolean"},
        "confidence": {"type": "number"},
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "model_expr": {"type": ["string", "null"]},
                    "prompt_ref": {"type": ["string", "null"]},
                    "tools": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "model_expr", "prompt_ref", "tools"],
                "additionalProperties": False,
            },
        },
        "wrapper": {
            "type": ["object", "null"],
            "properties": {
                "is_llm_wrapper": {"type": "boolean"},
                "attribution": {"enum": ["fixed", "passthrough", "default"]},
            },
            "required": ["is_llm_wrapper", "attribution"],
            "additionalProperties": False,
        },
        "abstain": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["is_agent", "confidence", "agents", "wrapper", "abstain", "rationale"],
    "additionalProperties": False,
}
