"""Adjudication provider — the shared OpenAI-compatible LLM path with the
adjudication response schema pre-bound as the structured-output hint.

The transport lives in :mod:`aiscan.llm` (shared with enrichment); this module
only binds the adjudication schema.
"""

from __future__ import annotations

import json
import logging

from aiscan.adjudicate.schema import RESPONSE_JSON_SCHEMA
from aiscan.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    CallFn,
    OpenAICompatibleError,
    resolve_api_key,
)
from aiscan.llm import build_openai_call_fn as _build

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "CallFn",
    "OpenAICompatibleError",
    "build_openai_call_fn",
    "resolve_api_key",
]

_SCHEMA_HINT = json.dumps(RESPONSE_JSON_SCHEMA, sort_keys=True)


def build_openai_call_fn(
    base_url: str,
    model: str,
    logger: logging.Logger,
    api_key: str | None = None,
) -> CallFn:
    """Adjudication call fn: shared transport + the adjudication schema hint."""
    return _build(base_url, model, logger, api_key=api_key, schema_hint=_SCHEMA_HINT)
