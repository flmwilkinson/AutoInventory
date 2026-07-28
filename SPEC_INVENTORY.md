# SPEC_INVENTORY — agent-centric, liveness-weighted AI inventory

**Status:** in progress · **Depends on:** SPEC-10 (embeddable core + write-through dataset)
**Goal:** shape aiscan's output into a bank-grade **AI inventory** — the estate of AI systems, their
agents, and the models/tools those agents actually use — that is *agent-centric*, *liveness-weighted*,
and *governance-safe*. Audited against the current code by a 6-agent workflow: the methodology is
already ~70% there; this spec closes the gaps and, importantly, **corrects a category error** (never
let liveness suppress risk) and pins the **definition of an AI component**.

## The definition (the crux)

> **An AI component ("agent") = code with a reachable LLM call site** — anything that can actually
> invoke an LLM. This includes a plain script calling the API (a "bare" LLM call), a bespoke
> LLM-in-a-loop, and a framework `Agent(...)`. All are live AI usage and are inventoried.
>
> **A model merely *named* is NOT an AI component** — a foundation model in a `.env`/config value, or a
> dependency declared but never called, is a *reference*, not an actioned agent. It becomes relevant
> only when "picked up" (code actually calls with it). This is the low-priority **dormant** bucket.
>
> **Reachability-from-an-entrypoint is a *confidence* attribute of a component, never the definition.**
> "The code can call an LLM" makes it an agent; "we can prove it runs" only sets its liveness tier.

The current pipeline already enforces the second clause: env candidates are kept only if a detected
fact references them (SPEC-5 §6 — `cli`/`collect_env_defaults` gating), and detection anchors on
resolved **call sites (sinks)**, not on names in config.

## Two orthogonal axes (the correction)

The earlier draft made a governance-category error: it treated *liveness* (wired to a runnable agent)
as *severity*, which would bury an ungoverned LLM-on-PII script in an "info" appendix — the exact
shadow AI a regulator fails you on. **Liveness and risk are orthogonal.**

| Axis | What it is | What it drives | Rule |
|---|---|---|---|
| **Liveness** | is this component reachable/invoked? (a confidence *tier*) | **emphasis** in the agent drill-down | never filters or gates what is inventoried |
| **Risk** | data sensitivity · external endpoint · unapproved model · autonomy · capability (moves_money…) | **governance attention / severity** | independent of liveness; a live external-LLM-on-PII call is HIGH risk whether or not it's a formal agent |

**Views enforce the split:**
- **Per-agent drill-down** → liveness-weighted (invoked/reachable agents headline; "defined" demoted).
- **Estate model register** (approved-model reconciliation) → **liveness-agnostic, complete, prominent** — every external model incl. shadow ones.
- **Sensitive-capability list** (moves_money / code_exec / admin) → **liveness-agnostic** — inventoried regardless of the binder's liveness.
- **Coverage / negative-attestation** (`scan_health`, skipped languages) → **elevated** — "no shadow AI found" is always qualified by coverage, never read as assurance.

## Liveness is a *confidence tier*, not a boolean (soundness)

Static analysis cannot prove an agent "runs." The `entrypoint` signal exists only in the openai-agents
packs (`Runner.run`), so most of a real bank estate (LangGraph/AutoGen/CrewAI/bespoke) has **no**
entrypoint coverage. Therefore liveness is a **tier with evidence**, and the design **never asserts
"dormant"** (which reads as "dead") when the truth is "we lack coverage":

- **`invoked`** — an invocation site for this agent was detected (highest confidence).
- **`reachable`** — reached from an invoked agent via handoff/route (forward closure).
- **`defined`** — detected in code, but no invocation/reach confirmed (present; liveness unconfirmed — covers both genuinely-dormant *and* frameworks we don't yet mark). Never labelled "dormant."

An invocation *site* is not proof of runtime execution (could be dead code / test / behind a flag) — it
is named honestly ("invocation detected"), and liveness weights **presentation only**.

## The inventory shape

```
SYSTEM (bundle = Record)                      ← the inventory row; owner, risk tier, approval, verdict
├── AI COMPONENTS  (the drill-down)           ← anything with a reachable LLM call site
│   ├── agents (framework + bespoke)          ← model (embedded) · tools · mcp · handoffs · autonomy
│   │     + liveness tier + reachable_tools
│   └── bare LLM calls (script-level usage)   ← model · low autonomy · STILL first-class, never "floating"
├── ESTATE MODEL REGISTER (liveness-agnostic) ← every external model, incl. shadow — for reconciliation
├── SENSITIVE CAPABILITIES (liveness-agnostic)← moves_money / code_exec / admin tools, any binder
└── DORMANT (de-emphasised, still recorded)   ← named-not-actioned: config-only models, unused deps,
                                                 never-called wrappers, declared-but-absent agents
```

## What's already built (~70% — verified against code)

- **System = row, agent = drill-down** is native: `Record.agents[]`, and there is **no top-level
  `models` collection** — models are already only per-agent attributes (`schema.py`).
- **Model embedded per agent** (`AgentRecord.model: ModelBinding`, built fresh per agent in
  `emit.build_record`); tools/mcp per-agent id-refs; derived `reachable_tools`, autonomy, role.
- **Bare LLM calls already first-class** as `model_usages` (`in_agent=false`) — not hidden today.
- **Dormant buckets already exist and are de-emphasised**: `AiDependency.used=false`,
  `dormant_ai_wrapper`, `declared_agent_artefact`, `dormant_ai_repos`.
- **The runnable signal is already computed** — `AgentDefF.entrypoint` (set from `Runner.run` marks).

## The gaps (what this spec fixes)

1. **`entrypoint` is computed then dropped** — never reaches `AgentRecord`; a test literally calls it
   "the unused entrypoint flag." The inventory can't headline the agent that actually runs. *(the crux)*
2. **`agent → tool` join is lost in the dataset** — only `tool_count` survives; "which agents use tool
   X / what tools does agent Y call" is **unanswerable from SQLite**.
3. **liveness conflated with severity** — the earlier plan; must split into two axes (above).
4. **orphan/bare-call severity hard-coded to `info`** (`derive/engine.py`) — a sensitive/external/
   unapproved live call can't escalate. Latent governance bug.
5. **estate model register would be liveness-filtered** — must be liveness-agnostic for reconciliation.
6. **`provider_class` + approval live on the floating `ModelUsage`, not the agent's `ModelBinding`** —
   after folding, "which agents run an *unapproved* model" gets hard; project it onto the agent.
7. **multi-model truncation** — `emit` keeps only `model_ids[0]`; a multi-model agent silently
   undercounts a bank's model register. At least flag it.
8. **MCP orphans invisible** — unwired MCP config (remote tool execution) is shadow tooling; give it
   the same wired/orphan treatment.

## Phase A — lean & local (existing record.json + SQLite, no new infra)

Ordered; each is `S`/`M`. Record-shape changes re-bless the goldens once (intentional, additive).

1. **Surface liveness on the agent (`S`→`M`).** Add `is_entrypoint: bool` to `AgentRecord`, set from
   `fact.entrypoint` in `build_record`; derive a **`liveness` tier** DerivedValue (`invoked`/`reachable`/
   `defined`) via a forward closure **seeded from entrypoints, ADDED ALONGSIDE** the existing per-agent
   `reachable_tools` (⚠️ do **not** repoint that loop — it would regress `reachable_tools`).
   *Files:* `inventory/schema.py`, `inventory/emit.py`, `derive/engine.py`.
2. **`agent_tools` bridge table (`M`, dataset-only, no re-bless).** `(bundle_id, scan_id, agent_id,
   tool_id)` so the agent↔tool join is queryable. *Files:* `dataset/flatten.py`, `dataset/store.py`.
3. **Split liveness from risk (`M`).** Bare/orphan LLM-call severity is driven by risk
   (sensitive host / external endpoint / unapproved model), not hard-coded `info`; keep the call
   first-class ("live AI usage"), never demoted for lacking an agent shape. *Files:* `derive/engine.py`.
4. **Liveness-agnostic estate views (`S`).** The model register and the sensitive-capability list are
   complete regardless of liveness; only the per-agent drill-down is liveness-weighted.
   *Files:* `derive/inventory.py`, `report/inventory.py`.
5. **Project `provider_class` + an approval slot onto the agent `ModelBinding` (`S`).**
   *Files:* `inventory/schema.py`, `emit.py`/`derive`.
6. **Visible multi-model signal (`S`).** `has_additional_models` flag when `>1` model binding exists
   (full de-truncation deferred). *Files:* `inventory/emit.py`, `schema.py`.
7. **MCP orphan treatment (`S`).** `attached_agent_count`/wired for MCP; include unwired MCP in the
   dormant/shadow surface. *Files:* `derive`, `dataset`.
8. **Elevate coverage (`S`).** Surface `scan_health` coverage + skipped-language census into the
   inventory headline so absence is never read as assurance. *Files:* `report/inventory.py`.

**Explicitly deferred (respecting the anti-over-engineering stance):**
- The **audit spine** (actor/trigger + `scans` event-log) — still wanted (the user asked for who/when/
  what-changed), but it is *scan provenance*, a **separate workstream** from the agent/liveness shape;
  sequenced on its own so neither tangles the other. (See "Audit trail" below.)
- Broadening `MarkEntrypoint` beyond openai-agents (LangGraph `.compile()/.invoke()`, CrewAI `kickoff`,
  AutoGen, bespoke run-loops) — `L`, and needs a before/after recall check; until then most bundles are
  liveness-`unknown`, which is stated, not marketed.
- The `(framework, module, slug)` entrypoint match-key fix — the mark's module is the *run* site, often
  ≠ the *def* site, so it can regress true positives; adopt only after a recall check.
- Full multi-model de-truncation.

## Audit trail (BUILT — local, free, no storage purchase)

The who/when/what-changed audit trail is scan *provenance* + change history, not inventory *content*,
so it lives beside the matrix in the same `inventory.db` and never touches the deterministic record:
- **who/when/why** — `--actor` / `--trigger` on the CLI + `run_scan`; a `scans` event-log row per
  scanned `(bundle, commit)`.
- **tamper-evident ledger** — a `audit_log` table: append-only, **hash-chained** (each row links the
  prior row's `entry_hash`), one row per scan RUN. `aiscan --verify-audit` walks the chain and reports
  any break — cryptographic tamper-EVIDENCE, entirely local (tamper-PREVENTION additionally needs
  OS/WORM controls, e.g. `chattr +i` — free — or a WORM store, only if wanted).
- **what-changed** — `bom_diff(base, head)` between commits (built).
- **governance overlay + reconciliation** — a `governance` table (owner/risk/approval/lifecycle) with
  its own `governance_audit` change-log; `aiscan --govern <bundle> [--owner/--risk/--approve]` records
  an audited decision, kept SEPARATE from detected evidence (never overwrites it). `aiscan --unattested`
  is the killer query: **detected AI systems not approved in the register = the shadow-AI report.**
- **immutable evidence** — per-`(repo, commit)` record.json/HTML kept, idempotent via the content
  `scan_id`; tamper-evident via the `identity_record` hash.
- **default-branch = the inventory; PR = the gate** — the estate reflects merged reality, not drafts.

Everything above runs on a laptop / internal box with SQLite + files — no cloud, no purchase. Cloud only
enters for always-on multi-user service, org-scale concurrency (free self-hosted Postgres), or
storage-enforced immutability — all optional and deferrable.

## Governance framing (why this shape)

Matches how a bank governs AI: the unit is the **system/use-case** (owner, risk tier, approval); agents
are its composition; only **actioned** AI (a real call site) is the estate; **liveness** aids triage but
**never hides risk**; and **reconciliation** (detected estate vs approved register, incl. shadow) is the
core value — not the plumbing. Aligns with EU AI Act inventory duties and SR 11-7-style model inventory
+ change control.

## Verification

- Golden re-bless is expected once (additive `is_entrypoint`/`liveness`/`has_additional_models` fields).
- New dataset tests: the `agent_tools` bridge answers "agents using tool X"; a `floating`/dormant query
  returns only no-call-site references (not live bare calls); an orphan call on a sensitive/external
  endpoint escalates above `info`.
- Soundness assertions: a bundle with no entrypoint marks yields liveness `defined`/unknown for all
  agents (never `dormant`); surfacing `entrypoint` does not change *which* agents are detected.
