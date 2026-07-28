"""Shared OpenAI-compatible LLM provider (stdlib HTTP, no dependency).

Both LLM tiers use this: **adjudication** (SPEC-1 §6.10 — resolves ambiguous
*detection*) and **enrichment** (SPEC-2 §4 — drafts *descriptions* of what was
detected). Per SPEC-1 §17 the endpoint is a client-approved deployment — real
OpenAI, in-tenant Azure OpenAI, or an in-VPC gateway — selected through
``base_url``. The API key is read from the environment at call time and is
never stored, logged, or written to any artifact. Structured output uses
``json_object`` mode (widely supported across gateways) with the caller's JSON
schema echoed into the prompt; the reply is validated by the caller with
Pydantic, so a malformed response is handled (abstain / grounded=false) rather
than trusted.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

_TIMEOUT_S = 60

CallFn = Callable[[str, str], str]
"""``(system_prompt, user_content) -> raw model text (JSON)``."""


class OpenAICompatibleError(RuntimeError):
    """Raised for a missing key, a bad endpoint, or a failed request."""


def resolve_api_key() -> str | None:
    """The LLM key: a dedicated one wins, else the standard OpenAI var."""
    for name in ("AISCAN_ADJUDICATE_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def build_openai_call_fn(
    base_url: str,
    model: str,
    logger: logging.Logger,
    api_key: str | None = None,
    schema_hint: str | None = None,
) -> CallFn:
    """Build a ``(system, user) -> raw JSON text`` call against an OpenAI-
    compatible chat-completions endpoint.

    ``schema_hint`` (a JSON-schema string) is appended to the system prompt so
    ``json_object`` mode returns the caller's shape. Raises
    :class:`OpenAICompatibleError` if no key is available or the endpoint scheme
    is not http(s)."""
    key = api_key or resolve_api_key()
    if not key:
        raise OpenAICompatibleError(
            "no LLM API key: set OPENAI_API_KEY (or AISCAN_ADJUDICATE_API_KEY) "
            "in your .env or environment"
        )
    endpoint = base_url.rstrip("/") + "/chat/completions"
    if not endpoint.startswith(("http://", "https://")):
        raise OpenAICompatibleError(f"LLM base_url must be http(s): {base_url!r}")

    def call(system_prompt: str, user_content: str) -> str:
        system = system_prompt
        if schema_hint is not None:
            system += (
                "\n\nReturn only a single JSON object that conforms to this JSON schema:\n"
                + schema_hint
            )
        payload = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Log the destination and model, never the key or the slice content.
        logger.info("llm call -> %s (model=%s)", endpoint, model)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OpenAICompatibleError(f"HTTP {exc.code} from {endpoint}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise OpenAICompatibleError(f"request to {endpoint} failed: {exc}") from exc
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAICompatibleError("unexpected chat-completions response shape") from exc

    return call
