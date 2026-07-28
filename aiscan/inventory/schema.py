"""Inventory record models (SPEC-1 §5.4 + SPEC-2 §4 enrichment fields).

SPEC-1 defines the schema; SPEC-2's enrichment layer adds the ``[E]`` summary
fields (LLM drafts, never authoritative). Sourcing classes:

- **[D] detected** — scanner-owned, evidence-carrying (``source: detected``).
- **[E] enriched** — LLM draft (``source: enriched``); ``confirmed_by`` starts
  null; ``insufficient_evidence`` is set when the facts were too thin to
  summarise (grounded=false) and ``value`` stays null — never a fabrication.
- **[G] governance** — the scanner never writes a value (null + ``candidate``).
- **[X] external** — the record holds a ref; resolution is out-of-band.

Enrichment may only **add** to ``[E]`` slots; it never writes [G]/[X] and never
overwrites a directly-detected [D] value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aiscan.context import ScanHealth
from aiscan.facts.models import FindingRecord
from aiscan.ingest.env_defaults import EnvDefault
from aiscan.ir.values import JsonRepr

__all__ = ["FindingRecord"]  # re-exported for record consumers

Confidence = Literal["high", "medium", "low"]
SideEffect = Literal["read", "write", "external_send", "code_exec", "admin_mutation"]
PromptOrigin = Literal["literal", "file", "config", "constructed"]
# SPEC-3 §2.1 three-way verdict: no_ai = zero triage signals (fast exit, negative
# attestation); ai_signals_only = triage fired but the full pipeline detected
# nothing; ai_detected = ≥1 agent, sink, model usage, or MCP server.
Verdict = Literal["ai_detected", "ai_signals_only", "no_ai"]


class EnrichedSlot(BaseModel):
    """[E] slot: LLM draft, never authoritative (SPEC-2 §4).

    ``value`` is the drafted prose (or null when grounded=false);
    ``confirmed_by`` is null until a human signs off; ``insufficient_evidence``
    carries the reason when the facts were too thin to summarise.
    """

    model_config = ConfigDict(frozen=True)

    value: str | None = None
    source: Literal["enriched"] = "enriched"
    confirmed_by: str | None = None
    insufficient_evidence: str | None = None


class GovernanceSlot(BaseModel):
    """[G] slot: owned by governance; the scanner never writes ``value``.

    ``candidate`` (SPEC-3 §4.3) is the only part the tool may fill — a cheap
    [D] hint (CODEOWNERS/git) or an [E] enrichment draft, clearly non-binding.
    """

    model_config = ConfigDict(frozen=True)

    value: str | None = None
    source: Literal["governance"] = "governance"
    candidate: str | None = None


class OwnerSlot(GovernanceSlot):
    """[G] owner with a cheap [D] candidate (CODEOWNERS / git log)."""


class DerivedValue(BaseModel):
    """[D derived] field (SPEC-3 §3): computed from other detected facts,
    deterministic, evidence = what it aggregates. Never guessed."""

    model_config = ConfigDict(frozen=True)

    value: JsonRepr = None
    source: Literal["derived"] = "derived"
    evidence: tuple[str, ...] = ()


class DerivedBlock(BaseModel):
    """System-level derived indicators (SPEC-3 §3.1)."""

    model_config = ConfigDict(frozen=True)

    agent_count: DerivedValue = DerivedValue()
    tool_count: DerivedValue = DerivedValue()
    model_count: DerivedValue = DerivedValue()
    mcp_count: DerivedValue = DerivedValue()
    bespoke_agent_count: DerivedValue = DerivedValue()
    framework_agent_count: DerivedValue = DerivedValue()
    agents_by_location: DerivedValue = DerivedValue()
    tools_by_location: DerivedValue = DerivedValue()
    agents_by_language: DerivedValue = DerivedValue()
    autonomy_profile: DerivedValue = DerivedValue()
    capability_flags: DerivedValue = DerivedValue()
    has_unapproved_endpoint: DerivedValue = DerivedValue()
    has_dynamic_prompts: DerivedValue = DerivedValue()
    has_unresolved_models: DerivedValue = DerivedValue()
    models_used: DerivedValue = DerivedValue()
    external_systems: DerivedValue = DerivedValue()
    # SPEC-6 §3: inventory-record derivations.
    system_type: DerivedValue = DerivedValue()
    usage_groups: DerivedValue = DerivedValue()
    data_signals: DerivedValue = DerivedValue()


class AiDependency(BaseModel):
    """[D] one AI-BOM row (SPEC-3 §4.1): a declared AI package, its pinned
    version when a lockfile has one, and whether the code actually imports it
    (``used: false`` = the classic dormant dependency).

    ``ecosystem`` distinguishes Python packages from npm packages found in
    package.json; since SPEC-4 W6 both are analysed, so ``used`` is real for
    npm too (the TS/JS module graph knows the imports).
    """

    model_config = ConfigDict(frozen=True)

    package: str
    version: str | None = None
    source: str | None = None
    used: bool = False
    ecosystem: Literal["python", "npm"] = "python"
    evidence: tuple[str, ...] = ()


class DetectionInfo(BaseModel):
    """Provenance for a detected entity."""

    model_config = ConfigDict(frozen=True)

    method: str
    confidence: Confidence
    evidence: tuple[str, ...] = ()


class DetectedValue(BaseModel):
    """[D] generic detected field with provenance."""

    model_config = ConfigDict(frozen=True)

    value: JsonRepr = None
    source: Literal["detected"] = "detected"
    method: str | None = None
    confidence: Confidence | None = None
    evidence: tuple[str, ...] = ()


class ModelBinding(BaseModel):
    """[D] an agent's model, with endpoint/deployment and attribution rung."""

    model_config = ConfigDict(frozen=True)

    value: JsonRepr = None
    source: Literal["detected"] = "detected"
    method: str | None = None
    endpoint: JsonRepr = None
    api_style: Literal["openai", "anthropic", "bedrock", "vertex", "unknown"] = "unknown"
    deployment: JsonRepr = None


class PromptBinding(BaseModel):
    """[D] system prompt with dynamism and origin."""

    model_config = ConfigDict(frozen=True)

    value: JsonRepr = None
    dynamic: bool = False
    origin: PromptOrigin = "literal"
    file_ref: str | None = None
    evidence: tuple[str, ...] = ()


class StateEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["messages", "session", "vectorstore", "shared_object"]
    evidence: tuple[str, ...] = ()


class PolicyEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["approval", "guardrail", "permission_check"]
    params: JsonRepr = None
    evidence: tuple[str, ...] = ()


class AgentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    detection: DetectionInfo
    # [D] where the agent construct lives — production | test | example — so
    # test/example noise can be filtered without dropping anything.
    location: Literal["production", "test", "example"] = "production"
    # [D] source language of the defining file (SPEC-4).
    language: Literal["python", "typescript", "javascript"] = "python"
    model: ModelBinding | None = None
    system_prompt: PromptBinding | None = None
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    state: tuple[StateEntry, ...] = ()
    control_policies: tuple[PolicyEntry, ...] = ()
    handoffs: tuple[str, ...] = ()
    # [D] route-transfer targets (SPEC-3: surfaced for role_class + graph view).
    routes: tuple[str, ...] = ()
    # [D] SPEC_INVENTORY: an invocation site for this agent was detected (e.g. a
    # Runner.run). A *site*, not proof of runtime execution — it seeds the
    # liveness tier, it does not assert the agent runs.
    is_entrypoint: bool = False
    # [D derived] SPEC-3 §3.2
    role_class: DerivedValue | None = None
    autonomy_level: DerivedValue | None = None
    capability_flags: DerivedValue | None = None
    reachable_tools: DerivedValue | None = None
    # [D derived] SPEC_INVENTORY liveness *tier* (never a boolean, never "dormant"):
    # "invoked" (entrypoint site detected) | "reachable" (reached from an invoked
    # agent via handoff/route) | "defined" (present; liveness unconfirmed —
    # includes frameworks without entrypoint coverage). Weights emphasis only.
    liveness: DerivedValue | None = None
    # [E] enrichment
    agent_summary: EnrichedSlot = EnrichedSlot()
    responsibilities: EnrichedSlot = EnrichedSlot()
    guardrails_summary: EnrichedSlot = EnrichedSlot()
    description: EnrichedSlot = EnrichedSlot()


class SignatureInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    params: tuple[str, ...] = ()
    returns: str | None = None


class AuthorisationSlot(BaseModel):
    """[D] '<verb> <target>' when derivable from side-effect analysis."""

    model_config = ConfigDict(frozen=True)

    value: str | None = None
    source: Literal["detected"] = "detected"


class EntitlementSlot(BaseModel):
    """[X] external linkage: reference emitted if detected; resolution is
    out-of-band and never guessed."""

    model_config = ConfigDict(frozen=True)

    ref: str | None = None
    source: Literal["external"] = "external"
    resolved: None = None


class ToolRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_id: str
    kind: Literal["function", "mcp_tool", "schema_declared", "retriever", "agent_as_tool"]
    # [D] where the tool's defining file lives — production | test | example —
    # same filtering affordance as AgentRecord.location.
    location: Literal["production", "test", "example"] = "production"
    signature: SignatureInfo | None = None
    side_effects: tuple[SideEffect, ...] = ()
    external_target: JsonRepr = None
    credential_ref: JsonRepr = None
    declared_authorisation: AuthorisationSlot = AuthorisationSlot()
    effective_entitlement: EntitlementSlot = EntitlementSlot()
    evidence: tuple[str, ...] = ()
    # [D derived] SPEC-3 §3.2
    is_sensitive: DerivedValue | None = None
    # [G] SPEC-3 §4.2
    data_classification_touched: GovernanceSlot = GovernanceSlot()
    # [E] enrichment
    tool_summary: EnrichedSlot = EnrichedSlot()
    capability_class: EnrichedSlot = EnrichedSlot()
    data_domain: EnrichedSlot = EnrichedSlot()
    description: EnrichedSlot = EnrichedSlot()


class McpRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_id: str
    server: JsonRepr = None
    transport: Literal["stdio", "sse", "http", "ws"] = "stdio"
    declared_tools: tuple[str, ...] | Literal["dynamic"] = "dynamic"
    approval_policy: JsonRepr = None
    evidence: tuple[str, ...] = ()
    # [D derived] SPEC-3 §3.2
    approval_required: DerivedValue | None = None
    transport_risk: DerivedValue | None = None
    # [E] enrichment
    server_summary: EnrichedSlot = EnrichedSlot()


class ModelUsage(BaseModel):
    """A plain LLM call site with no agent shape (SPEC-1 §6.7.3)."""

    model_config = ConfigDict(frozen=True)

    model: DetectedValue
    task: Literal["chat", "completion", "embedding", "stt", "tts"] = "chat"
    # [D] resolved endpoint of the call site, when any (SPEC-3: provider_class input).
    endpoint: JsonRepr = None
    evidence: tuple[str, ...] = ()
    in_agent: bool = False
    # [D derived] SPEC-3 §3.2
    provider_class: DerivedValue | None = None
    # [G] SPEC-3 §4.2: the bank's approved-model register decision
    model_approved: GovernanceSlot = GovernanceSlot()
    # [E] enrichment
    model_purpose: EnrichedSlot = EnrichedSlot()


class InventoryProvenance(BaseModel):
    """Everything needed to reproduce this record (SPEC-1 §10)."""

    model_config = ConfigDict(frozen=True)

    scanner: str
    rulepacks: dict[str, str] = Field(default_factory=dict)
    org_pack: str | None = None
    scanned_at: str = ""
    enrichment_model: str | None = None
    detection_basis: Literal["static/declared"] = "static/declared"


SCHEMA_VERSION = "1.0"
"""record.json schema version (SPEC-10 §G). Bump on any breaking change to the
record shape so a downstream consumer / stored dataset can gate on it."""


class Record(BaseModel):
    """The bundle-level inventory record (SPEC-1 Appendix A + [E] summaries)."""

    model_config = ConfigDict(frozen=True)

    bundle_id: str
    schema_version: str = SCHEMA_VERSION
    name: str
    repo_url: str | None = None
    scanned_commit: str = "unknown"
    ai_verdict: Verdict = "ai_detected"
    # Back-compat boolean, always derived from ai_verdict at emission.
    no_ai_detected: bool = False
    # [E] enrichment (system level)
    system_summary: EnrichedSlot = EnrichedSlot()
    capability_summary: EnrichedSlot = EnrichedSlot()
    data_interaction_summary: EnrichedSlot = EnrichedSlot()
    human_oversight_summary: EnrichedSlot = EnrichedSlot()
    suggested_aia_risk_category: EnrichedSlot = EnrichedSlot()
    description: EnrichedSlot = EnrichedSlot()
    # [G] governance
    owner: OwnerSlot = OwnerSlot()
    risk_tier: GovernanceSlot = GovernanceSlot()
    purpose: GovernanceSlot = GovernanceSlot()
    data_classification: GovernanceSlot = GovernanceSlot()
    regulatory_scope: GovernanceSlot = GovernanceSlot()
    # [G] governance workflow state (SPEC-3 §4.2) — the review process owes these
    lifecycle_status: GovernanceSlot = GovernanceSlot()
    approval_status: GovernanceSlot = GovernanceSlot()
    approver: GovernanceSlot = GovernanceSlot()
    approval_date: GovernanceSlot = GovernanceSlot()
    last_review: GovernanceSlot = GovernanceSlot()
    next_review: GovernanceSlot = GovernanceSlot()
    # [X] link into the bank's application register (SPEC-3 §4.2)
    cmdb_app_id: EntitlementSlot = EntitlementSlot()
    # [D] AI-BOM (SPEC-3 §4.1)
    ai_dependencies: tuple[AiDependency, ...] = ()
    # SPEC-5 §6: repo-declared env candidates for env keys referenced by
    # detected facts — declared config with provenance, never a detection.
    env_defaults: dict[str, tuple[EnvDefault, ...]] = Field(default_factory=dict)
    # SPEC-6 §3.4: git-derived candidates over AI-touching files. Candidates,
    # never authoritative ownership.
    contributor_candidates: tuple[str, ...] = ()
    ai_commit_range: dict[str, str] | None = None
    # [D derived] SPEC-3 §3.1 — filled by the derive engine on every scan
    derived: DerivedBlock | None = None
    # entities
    agents: tuple[AgentRecord, ...] = ()
    tools: tuple[ToolRecord, ...] = ()
    mcp_servers: tuple[McpRecord, ...] = ()
    model_usages: tuple[ModelUsage, ...] = ()
    findings: tuple[FindingRecord, ...] = ()
    scan_health: ScanHealth = Field(default_factory=ScanHealth)
    inventory_provenance: InventoryProvenance = InventoryProvenance(scanner="aiscan")
