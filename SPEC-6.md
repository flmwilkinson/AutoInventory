# SPEC-6.md — The AI Inventory Record (v6 Build Brief)

**Audience: Claude Code.** Build after SPEC-5 (X0–X5 green). Same standards as SPEC-1 §3
(`mypy --strict`, determinism, never execute scanned code, evidence-or-it-didn't-happen).
This brief adds a **presentation tier**, not a detection tier: a plain-English AI
inventory record (`inventory.html`) rendered from existing facts plus a handful of small
derive additions. `report.html` becomes strictly **the record + a technical annex** —
one shared renderer, so the two artifacts cannot drift apart by construction.

---

## 0. Context and mission

A bank's AI inventory is two-tier everywhere it exists in practice (EU AI Act Annex
VIII registry rows; the US OMB use-case CSVs; IBM watsonx use-case⊃factsheet nesting;
Workday/Entra agent registries; SR 11-7 model inventories): a **one-line-per-system
estate row** expanding into a **structured record with nested agent sub-records**. No
published standard yet covers both halves of an agent record — A2A agent cards describe
the interface, AgentFacts/MIT-Agent-Index fields describe the internals (model, prompt,
tools, memory, autonomy). aiscan's record unions both, with repo evidence.

The current `report.html` is a scanner report, not an inventory record: a reviewer meets
34 findings rows and resolver counters before learning what the system does. SPEC-6
inverts it. **`inventory.html` is the record.** `report.html` = the identical record
body + annex (full findings, per-call-site tables, env tables, full BOM, governance
panel, scan health). The fleet CSV/index carry the same Tier-1 fields so estate queries
match what the record displays.

### Sourcing classes (the record's legend — normative)

- **[D] Detected** — read off the code, evidence-backed.
- **[C] Declared** — the repo's own config says it (env defaults, manifests); candidate,
  never proof of runtime.
- **[E] Drafted** — LLM narrative grounded in detected facts (`--enrich`); always
  DRAFT-badged; renders "not drafted — run `--enrich`" when absent, never silently.
- **[D∅] Absence-report** — rendered as "none detected in analysed code", never "none".
  Applies to autonomy gates and guardrails. Wrong-and-confident is the worst failure.

### Non-goals (deliberate cuts, per user direction)

- **No governance/workflow fields in the record** (risk tier, approvals, reviews): the
  existing governance panel moves to the annex unchanged. The record shows only the
  owner *candidate* (git-derived, labelled).
- **No register-handoff empty slots**: business system name, deployment status, usage
  restrictions, permissions/entitlements are **cut** — not derivable from scanning and
  overkill for this record. (Consequently no deployability file-sniffing either.)
- **No PII yes/no flag** — replaced by detected *data signals* only.
- **No new LLM calls**: the three existing enrichment slots are re-roled, not extended.
- **No CycloneDX/SPDX export yet** (the field dictionary is designed to map onto them;
  export is a future spec).

---

## 1. Deliverable and acceptance (definition of done)

1. `aiscan <repo>` emits **`inventory.html`** alongside the existing artifacts; on the
   DocGen clone it reads as a bank inventory record: identity → what it is → composition
   counts → four agent cards (model, prompt, tools, autonomy, memory, connections) →
   canonical models (~5 rows, not 12) → other AI usage (grouped) → connections &
   dependencies → one assurance line linking to the annex. Zero scanner jargon
   ("sink", "union", "symbolic", "resolver") in the record body.
2. **`report.html` = record body + annex.** Its old summary/agents/tools/models
   sections are deleted, not kept alongside; both artifacts render the record through
   one shared module. The annex carries: governance panel, full findings, per-call-site
   usages, declared-env-defaults table, full BOM, graph footnotes, scan health.
3. **Tier-1 alignment:** fleet `index.html` and `csv/systems.csv` carry the new estate
   columns (type, canonical models, capabilities, autonomy, owner candidate), computed
   from the same derived fields the record displays.
4. `no_ai` verdict: `inventory.html` is the negative-attestation record (what was
   checked, at which commit, when).
5. All SPEC gates: `mypy --strict`, `ruff`, full pytest, determinism double-run,
   goldens re-blessed once per reviewed change; enrich-in-place rewrites
   `inventory.html` too.

---

## 2. The field dictionary (normative)

### Tier 1 — estate row (fleet index + systems.csv)

| Field | Class | Source |
|---|---|---|
| System id (repo name, commit, repo URL) | D | record identity |
| Type — `agentic` \| `genai` \| `none` + component qualifiers (multi-agent, RAG, embeddings, batch LLM) | D | new derive §3.1 |
| What it does (one line) | E | `description` slot |
| Agents / tools / MCP counts | D | existing derived |
| Models (canonical, deduped) | D+C | new derive §3.2 |
| Capabilities (plain-English phrases from flags) | D | existing flags, new phrasing |
| Autonomy | D∅ | existing `autonomy_profile`, re-phrased |
| Owner (candidate) + contributors (candidates) | D | existing owner + new §3.4 |
| AI activity dates (first/last AI-touching commit) | D | new §3.4 |

### Tier 2 — system record (`inventory.html` body)

Identity [D] · purpose paragraph [E: `system_summary`] · inputs/outputs — detected
interface list [D: tool schemas, embedding tasks, prompt origins] + narrative
[E: `data_interaction_summary`] · data signals [D: §3.3] · composition counts [D] ·
agent cards (Tier 3) · canonical models table [D+C: §3.2, env candidates inline] ·
other AI usage grouped by file×model×task [D: §3.2] · connections & dependencies
[D: external systems, AI packages one-liner, MCP servers] · sourcing line [D: in-house
code + vendor components] · assurance line [D: files/LOC analysed, coverage banner state,
unresolved count → "see technical annex"].

### Tier 3 — agent card (per agent, the A2A-surface + internals union)

| Field | Class | Source |
|---|---|---|
| Name · defining file · framework · language · confidence | D | AgentRecord |
| Role — what this agent does | E | `agent_summary` |
| Foundation model (canonical + provider + env-configurable qualifier + declared default) | D+C | §3.2 |
| Instructions (system prompt: first lines + expand; dynamic flagged as dynamic) | D | existing |
| Tools — name + plain-English side effects + external target | D | existing records, joined inline |
| Acts externally? | D | `sends_external` flag |
| Autonomy | D∅ | "no human-approval gate detected in code" / cite the gate's evidence |
| Memory / state | D | state kinds |
| Connections (handoffs/routes/invocations) | D | existing edges |
| Guardrails | D∅ | control policies or "none detected in analysed code" |
| Credential references (env/config keys only, never values) | D | tool `credential_ref`s aggregated |

Rendering rules (all tiers): canonical-model qualifier chips are mandatory whenever env
members exist (`code default` / `env-configurable` / `+unknown`); absence fields always
use the D∅ phrasing; every [E] value carries the DRAFT badge; every card links its
evidence (`file:line`).

---

## 3. Derive-layer additions (Y0/Y1 — all additive to `record.json` and the dataset)

### 3.1 `system_type`
`agentic` when ≥1 agent; else `genai` when ≥1 model usage/sink; else `none`.
Component qualifiers derived from facts: `multi-agent` (≥2 agents), `rag`
(embedding usages ∧ vectorstore state), `embeddings`, `batch-llm` (non-agent usages in
worker/job paths). Deterministic string list, no ML.

### 3.2 Canonical models + usage groups
`models_used[]` gains `display` (string) and `qualifiers` (list). Rules:
- Union values: primary = the literal member when exactly one literal exists (the code
  default of an `env || literal` chain — a detected fact); qualifiers gain
  `env-configurable` (+ keys) and `+unknown` for Top members. No literal → the symbolic
  keys themselves. Members are never discarded — the full union stays in the record.
- `wrapper_default:` symbolics render `via <function> (wrapper)` — no cross-join
  guessing of the wrapper's internal model in this spec.
- Dedupe `models_used` by `(display, task)`; env-default candidates attach inline.
- New `usage_groups[]`: model usages grouped by (source file, display, task) with call
  counts and an any-`in_agent` flag. The 34-row table becomes ~12 grouped rows; the
  full table remains in the annex.

### 3.3 `data_signals`
Deterministic list, replaces any PII claim: org-pack sensitive-host touches (with host),
external-send targets, "indexes documents for semantic search" (embeddings ∧
vectorstore). Empty list renders "none detected" — a D∅ field.

### 3.4 Git provenance (`inventory/provenance` addition)
Via the sanctioned `git` subprocess, over the **AI-touching files** already in the
record (agents/tools/usages source files): `contributor_candidates` (top-5 author names
by commit count, candidate-labelled) and `ai_first_commit`/`ai_last_commit` (ISO dates).
Non-git targets (fixtures) → fields absent, rendered "—"; never an error. Determinism:
values derive from repo history, which is pinned by the commit — stable for a given
clone state.

### 3.5 Enrichment slot roles (no new calls)
Prompt instructions clarified: `description` = one-line what-it-does (Tier 1);
`system_summary` = purpose paragraph (Tier 2); `data_interaction_summary` = inputs/
outputs narrative; `agent_summary` = the agent card's role line. Governance `purpose`
candidate is annex-only.

---

## 4. Rendering (Y2)

- New `report/inventory.py`: `inventory_sections(record) -> list[str]` (the shared
  record body) + `render_inventory(record) -> str` (standalone page: same CSS shell,
  record body, minimal footer with scan basis + annex link).
- `report/html.py`: `render_report` = header + **shared record body** + annex sections
  (governance panel, findings, full usages, env-defaults table, full BOM, scan health);
  its superseded summary/agents/tools/models section functions are removed.
- `write_artifacts` (and enrich-in-place) write `inventory.html`; `no_ai` renders the
  attestation record.
- Jargon guard: the record body must not contain "sink", "union", "symbolic",
  "unresolved:" (the annex may) — enforced by a test.

## 5. Fleet & dataset alignment (Y3)

`csv/systems.csv` + fleet `index.html` gain: `system_type`, `models` (canonical display
join), `capabilities`, `autonomy`, `owner_candidate`. Values come from the derived
fields — never recomputed in the fleet layer.

## 6. Testing & gates

- Fixture goldens: `inventory.html` blessed for every fixture that has a `report.html`
  golden (+ `no_ai_clean`); DocGen smoke asserts §1.1 (agent-card count, canonical model
  count ≤ 6, jargon guard, grouped usage count).
- Unit suites: canonical display (union/wrapper/plain/all-symbolic cases), usage
  grouping, `system_type` matrix, data signals, git provenance (injected `git` output +
  non-git fallback).
- Existing goldens: additive-field re-bless only; determinism double-run; `mypy
  --strict`; `ruff`; no-exec canary unchanged (git remains the only subprocess).

## 7. Build order

**Y0** derive additions §3.1–3.3 + schema fields → **Y1** git provenance §3.4 →
**Y2** shared renderer, `inventory.html`, report restructure §4 → **Y3** fleet/CSV +
enrich roles + goldens + DocGen acceptance sweep (README, DECISIONS).

## 8. Ambiguity protocol

As SPEC-1 §12: simpler deterministic option, one DECISIONS line, no features beyond this
spec, no TODO-stubbed rendering — a phase ships working output or it isn't done.
