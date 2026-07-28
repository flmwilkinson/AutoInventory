"""HTTP-shape scorer (SPEC §6.5b) — the endpoint-agnostic core.

An LLM call is recognised by the *shape* of the request (payload keys, path
fragments, response access, auth headers), never by its host. A call to
``https://gw.bank.internal/llm/v1/chat`` scores exactly like one to
``api.openai.com``.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiscan.facts.models import ApiStyle, Confidence, lower_confidence
from aiscan.ir.values import Bool, DictVal, Str, Template, ValueSet

PATH_FRAGMENTS = (
    "/chat/completions",
    "/completions",
    "/v1/messages",
    ":generateContent",
    "/converse",
    "/generate",
    "/invoke",
    "/embeddings",
)

_SAMPLING_KEYS = ("temperature", "max_tokens", "max_completion_tokens", "top_p")
_AUTH_KEYS = ("authorization", "x-api-key", "api-key", "anthropic-version")

SINK_THRESHOLD = 5
HIGH_THRESHOLD = 7
SUSPECT_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class ShapeInputs:
    url_text: str
    payload: DictVal | None
    payload_unresolved: bool
    headers: DictVal | None
    # {"choices0", "candidates0", "content0", "stop_reason", "finish_reason"}
    response_fields: frozenset[str]
    stream_hint: bool
    # SPEC-5 §3: the matched SDK-method chain suffix (e.g. "embeddings.create")
    # for calls on opaque roots — the path-fragment analogue for SDK calls.
    chain_hint: str | None = None


@dataclass(frozen=True, slots=True)
class ShapeScore:
    score: int
    signals: tuple[str, ...]
    api_style: ApiStyle
    payload_open: bool

    @property
    def is_sink(self) -> bool:
        return self.score >= SINK_THRESHOLD

    @property
    def is_suspect(self) -> bool:
        return SUSPECT_THRESHOLD <= self.score < SINK_THRESHOLD

    def confidence(self) -> Confidence:
        base: Confidence = "high" if self.score >= HIGH_THRESHOLD else "medium"
        return lower_confidence(base) if self.payload_open else base


def _payload_keys(payload: DictVal | None) -> tuple[str, ...]:
    return payload.keys() if payload is not None else ()


def _header_signals(headers: DictVal | None) -> list[str]:
    if headers is None:
        return []
    out: list[str] = []
    for key, vals in headers.entries:
        lk = key.lower()
        if lk == "authorization":
            if _text_prefix(vals).startswith("Bearer"):
                out.append("auth:bearer")
        elif lk in _AUTH_KEYS:
            out.append(f"auth:{lk}")
    return out


def _text_prefix(vals: ValueSet) -> str:
    for v in vals:
        if isinstance(v, Str):
            return v.s
        if isinstance(v, Template):
            return v.literal_prefix()
    return ""


def score_shape(inputs: ShapeInputs) -> ShapeScore:
    """Score a resolved HTTP call site against the SPEC §6.5b signal table."""
    signals: list[str] = []
    score = 0

    if any(f in inputs.url_text for f in PATH_FRAGMENTS):
        score += 3
        signals.append("path:llm-fragment")

    if inputs.chain_hint:
        # A known SDK chain suffix on an opaque root is decisive by the same
        # logic as a URL path fragment (SPEC-5 §3).
        score += 3
        signals.append("chain:sdk-suffix")

    keys = {k.lower() for k in _payload_keys(inputs.payload)}
    if "model" in keys and "messages" in keys:
        score += 3
        signals.append("payload:model+messages")
    elif "model" in keys and keys & {"prompt", "input", "contents", "file"}:
        # ``file`` is the audio-transcription content field; combined with the
        # matched audio suffix it clears threshold, alone it cannot (model+file
        # = 2 < 5), so this does not manufacture non-audio sinks.
        score += 2
        signals.append("payload:model+prompt")
    elif "messages" in keys:
        # SPEC-7: role-tagged messages without a model key — the LangChain
        # invoke shape (model lives on the receiver object).
        score += 2
        signals.append("payload:messages")

    if inputs.response_fields:
        score += 2
        signals.append("response:" + ",".join(sorted(inputs.response_fields)))

    auth = _header_signals(inputs.headers)
    if auth:
        score += 1
        signals.extend(auth)

    sampling = sum(1 for k in _SAMPLING_KEYS if k in keys)
    if sampling:
        bump = min(sampling, 2)
        score += bump
        signals.append(f"payload:sampling+{bump}")

    stream = inputs.stream_hint
    if not stream and inputs.payload is not None:
        stream_vals = inputs.payload.get("stream")
        stream = stream_vals is not None and Bool(v=True) in stream_vals
    if stream:
        score += 1
        signals.append("stream")

    payload_open = inputs.payload.open if inputs.payload is not None else inputs.payload_unresolved
    return ShapeScore(
        score=score,
        signals=tuple(signals),
        api_style=_infer_api_style(inputs, keys),
        payload_open=payload_open,
    )


def _infer_api_style(inputs: ShapeInputs, keys: set[str]) -> ApiStyle:
    anthropic_signals = (
        "/v1/messages" in inputs.url_text
        or any(s == "auth:anthropic-version" for s in _header_signals(inputs.headers))
        or "stop_reason" in inputs.response_fields
        or "content0" in inputs.response_fields
    )
    if anthropic_signals:
        return "anthropic"
    if (
        ":generateContent" in inputs.url_text
        or "contents" in keys
        or "candidates0" in inputs.response_fields
    ):
        return "vertex"
    if "/converse" in inputs.url_text:
        return "bedrock"
    if keys or "choices0" in inputs.response_fields or inputs.url_text:
        return "openai"
    return "unknown"
