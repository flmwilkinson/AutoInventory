"""Fact vocabulary (SPEC §5.2).

Every fact carries evidence spans (≥1), a confidence, the method (rule or
detector id) that produced it, and its source files (for provenance). Both
frontends emit these; the ADG is assembled from them and never knows which
frontend found what — only ``method`` records provenance.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aiscan.ir.values import JsonRepr

Confidence = Literal["high", "medium", "low"]
SideEffect = Literal["read", "write", "external_send", "code_exec", "admin_mutation"]
PromptOrigin = Literal["literal", "file", "config", "constructed"]
ApiStyle = Literal["openai", "anthropic", "bedrock", "vertex", "unknown"]
Task = Literal["chat", "completion", "embedding", "stt", "tts"]

_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


def max_confidence(a: Confidence, b: Confidence) -> Confidence:
    return a if _CONF_ORDER[a] >= _CONF_ORDER[b] else b


def lower_confidence(c: Confidence) -> Confidence:
    if c == "high":
        return "medium"
    return "low"


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic, short, content-derived fact id."""
    digest = hashlib.sha256("|".join(repr(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:12]}"


class Fact(BaseModel):
    """Common fact base: identity + provenance."""

    model_config = ConfigDict(frozen=True)

    id: str
    evidence: tuple[str, ...] = Field(min_length=1)
    confidence: Confidence
    method: str
    source_files: tuple[str, ...] = ()


class AgentDefF(Fact):
    name: str
    kind: Literal["framework", "bespoke"]
    framework: str | None = None
    entrypoint: bool = False
    location: Literal["production", "test", "example"] = "production"
    # SPEC-4: source language of the defining file (from its extension).
    language: Literal["python", "typescript", "javascript"] = "python"


class AgentCandidateF(Fact):
    """F1-internal: a graph node that may be promoted to an agent (SPEC §6.6)."""

    name: str
    fn_ref: str


class PromptDefF(Fact):
    content: JsonRepr = None
    dynamic: bool = False
    origin: PromptOrigin = "literal"
    file_ref: str | None = None
    content_hash: str | None = None


class ModelRefF(Fact):
    provider: str | None = None
    model: JsonRepr = None
    endpoint: JsonRepr = None
    deployment: JsonRepr = None
    api_style: ApiStyle = "unknown"
    task: Task = "chat"


class SignatureF(BaseModel):
    model_config = ConfigDict(frozen=True)

    params: tuple[str, ...] = ()
    returns: str | None = None


class ToolDefF(Fact):
    name: str
    kind: Literal["function", "mcp_tool", "schema_declared", "retriever", "agent_as_tool"]
    signature: SignatureF | None = None
    side_effects: tuple[SideEffect, ...] = ()
    external_target: JsonRepr = None
    credential_ref: JsonRepr = None
    auth_verb: str | None = None  # HTTP verb of the external_send call, for [D] authorisation


class MCPDefF(Fact):
    server: JsonRepr = None
    transport: Literal["stdio", "sse", "http", "ws"] = "stdio"
    declared_tools: tuple[str, ...] | Literal["dynamic"] = "dynamic"
    approval_policy: JsonRepr = None


class StateDefF(Fact):
    kind: Literal["messages", "session", "vectorstore", "shared_object"]
    backing: JsonRepr = None


class PolicyDefF(Fact):
    kind: Literal["approval", "guardrail", "permission_check"]
    params: JsonRepr = None


class LLMCallSiteF(Fact):
    """Model use with no agent shape — inventoried, not an agent (SPEC §6.7.3)."""

    model_id: str
    prompt_id: str | None = None
    task: Task = "chat"
    in_agent: bool = False


class FindingRecord(BaseModel):
    """A scan finding (secret redaction, suspected sink, opaque wrapper, …).

    ``severity``/``subject_ref`` are SPEC-3 §3.3 additive fields: graded by the
    derive engine from a fixed kind→severity map; older records stay valid.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    evidence: tuple[str, ...] = ()
    detail: str | None = None
    severity: Literal["high", "medium", "low", "info"] | None = None
    subject_ref: str | None = None


class BindModelF(Fact):
    agent_id: str
    model_id: str


class BindPromptF(Fact):
    agent_id: str
    prompt_id: str


class BindToolF(Fact):
    agent_id: str
    tool_id: str


class BindMCPF(Fact):
    agent_id: str
    mcp_id: str


class BindStateF(Fact):
    agent_id: str
    state_id: str


class AttachPolicyF(Fact):
    policy_id: str
    target_id: str


class TransferF(Fact):
    from_id: str
    to_id: str
    kind: Literal["handoff", "invoke", "route"]
    guard: str | None = None


class InterAgentMsgF(Fact):
    from_id: str
    to_id: str
    via: str | None = None


AnyFact = (
    AgentDefF
    | AgentCandidateF
    | PromptDefF
    | ModelRefF
    | ToolDefF
    | MCPDefF
    | StateDefF
    | PolicyDefF
    | LLMCallSiteF
    | BindModelF
    | BindPromptF
    | BindToolF
    | BindMCPF
    | BindStateF
    | AttachPolicyF
    | TransferF
    | InterAgentMsgF
)
