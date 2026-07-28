# SPEC-3.md — Full Bank Inventory Record, Fast Verdict Path, Report, Graph & Estate Layer (v3 Build Brief)

**Audience: Claude Code.** Build this **after** SPEC-1 (detection core) and the SPEC-2 §4
enrichment layer are green — both are, at time of writing. Same engineering standards as SPEC-1
§3 (Python 3.12, `mypy --strict`, Pydantic v2, determinism, never execute scanned code,
evidence-or-it-didn't-happen). **SPEC-1's record schema still holds; everything here is
additive.** This brief supersedes SPEC-2 as the definition of what Phase-2/3 material gets
built: it carries over SPEC-2's derived-indicator engine, the useful governance/external
fields, the dataset, and the fleet runner — and permanently trims the over-detailed remainder
(§0 lists the trims).

---

## 0. Context and mission

The detection core answers *"what AI is in this repo?"*. v3 turns each scan into the artifact a
bank's AI-inventory process actually consumes, and turns many scans into an estate inventory.
Six goals:

1. **A verdict, fast.** Most of a bank's estate contains no AI. Those repos must exit in
   seconds with an attestable *negative* record ("scanned commit X on date Y, no AI found,
   here is what we checked") — and must be structurally incapable of triggering an LLM call
   or the slow pipeline stages. Estate coverage is built from negative attestations as much
   as positive detections.
2. **A full record once AI is detected.** Deterministic *indicators* on top of the entities:
   capability flags, autonomy, per-agent blast radius, models/endpoints/vendors in use,
   external systems touched, AI package dependencies, severity-ranked findings.
3. **Governance-ready slots.** The record carries the [G]/[X] fields a bank review process
   fills — lifecycle, approval, review dates, CMDB link, model approval — with the scanner
   contributing **candidates only** (E→G), never values.
4. **A human-readable report.** `report.html`: system-level summary, collapsible drill-down
   per agent / tool / MCP server, severity-sorted findings, location filters, enrichment
   drafts clearly badged as drafts. One self-contained file that opens on an air-gapped laptop.
5. **A graph view.** The ADG rendered as inline SVG inside the report — system → agents →
   tools/MCP → models, handoff arcs — for blast-radius conversations.
6. **An estate layer.** Flatten records into a queryable dataset (stdlib SQLite + CSV) and a
   fleet runner over a repo list, producing one dataset, one shared wrapper registry, and an
   estate index page. Records remain the source of truth; the dataset is a rebuildable
   projection.

### Non-goals and permanent trims (deliberate, do not build)

- **SM&CR-grain ownership** (`accountable_manager`, per-agent `agent_owner` /
  `agent_approval_status`): governance workflow detail beyond the record's job. System-level
  `owner` + candidates suffice; agent-grain governance is a client-specific extension.
- **`control_attestations`, `review_cadence`, `deployment_environments`,
  `out_of_scope_use` / `intended_use`**: workflow state and manifest-reading we can't ground;
  one Pydantic field each when a real client process asks.
- **NHI linkage detail** (`credential_owner`, MCP `identity`/`granted_scopes`/`server_owner`):
  the [X] refs that exist (`effective_entitlement`, `credential_ref`) already carry the hook;
  resolving them is the §4.4 interface's job, later.
- **DuckDB/Parquet/pandas**: the dataset is stdlib `sqlite3` + CSV exports. DuckDB is a
  swap-in if an estate outgrows SQLite — the `flatten()` contract is the abstraction.
- **GitHub-org enumeration**: fleet input is an explicit repo-list file (URLs/paths). Org
  crawling needs API tokens and rate handling; later.
- **Diff/drift, web service, JS frameworks, other languages** — unchanged from SPEC-1.
- **No new pip dependencies.** Report = stdlib string building + `html.escape`; SVG layout
  computed in Python; dataset = stdlib `sqlite3` + `csv`.

---

## 1. Deliverables

`aiscan <target>` now writes to `--out`:

```
record.json        # SPEC-1 schema + v3 additive fields (§3–§4)
graph.json         # unchanged
facts.jsonl        # unchanged
scan_health.json   # + triage timing, verdict
scan.log
report.html        # NEW — self-contained report incl. graph view (§5–§6)
```

Fleet mode additionally produces (in the fleet `--out` root):

```
records/<repo>-<commit>/…      # one full artifact set per repo
inventory.db                   # SQLite dataset (§8), rebuildable from records/
csv/systems.csv agents.csv tools.csv models.csv findings.csv
index.html                     # estate index page (§9.3)
org_registry.json              # shared wrapper registry, reused across the run
```

CLI surface (single default command preserved):

```
aiscan <path|git-url|prior-out-dir> [--commit] [--out] [--org-pack] [--json-logs]
       [--adjudicate] [--enrich] [--enrich-tests] [--repo PATH]
aiscan --repos FILE [--out DIR] [--org-pack] [--enrich]      # fleet mode (§9)
aiscan --rebuild-dataset DIR                                  # dataset projection (§8)
```

- `<prior-out-dir>`: a target containing `record.json` enters **enrich-in-place mode** (§7.3)
  — no rescan, requires `--enrich`.
- `--enrich-tests`: include `location == "test"` nodes in enrichment (default: skipped).
- `--repo PATH`: source tree for enrichment slices in enrich-in-place mode.

---

## 2. The verdict fast path (V0)

### 2.1 `ai_verdict`

Every record carries a top-level three-way verdict (additive field; `no_ai_detected: bool`
kept for back-compat, derived from it):

| Verdict | Meaning | Pipeline behaviour |
|---|---|---|
| `no_ai` | Zero triage signals (SPEC-1 §6.1 gate) | **Fast exit**: no parse, no resolve, no frontends. Emit negative-attestation record + report. |
| `ai_signals_only` | Triage fired (dep/string/import signal) but the full pipeline detected zero agents, zero sinks, zero model usages | Full pipeline ran; record has empty entity lists + the AI-BOM (§4.1) explaining *why* triage fired |
| `ai_detected` | ≥1 agent, sink, or model usage | Full record |

The negative-attestation record for `no_ai` is a **complete, valid `Record`**: header,
verdict, the triage signals table (what was checked, per SPEC-1 §6.1), scan timing, and
provenance. The report renders it as a one-screen "No AI detected" page with the checklist.

### 2.2 Hard guarantees (tested, not aspirational)

- **LLM guard:** on a `no_ai` verdict, `--enrich`/`--adjudicate` make **zero network calls**;
  the flags produce a visible `scan_health` note (`"skipped: no_ai verdict"`), never a
  silent no-op. On `ai_signals_only`, enrichment runs at most the system node (there is
  nothing else to describe).
- **No mid-pipeline bailouts.** The fast exit is triage-level only. Once any signal fires,
  the *full* pipeline runs — recall is the product; a "weak signals → skip F2" shortcut is
  exactly how a bank misses the internal-gateway agent. Do not add one.
- **Timing budget:** `no_ai` path p50 ≤ 3 s / p95 ≤ 10 s on 100k LOC (triage is already
  capped at 1 MB/file grep). `scan_health.stage_ms.triage` records it.

---

## 3. Derived-indicator engine (V1) — `aiscan/derive/`

Deterministic, runs at record-emission time, **no LLM**, pure function of
`(graph, facts, org pack, hosts registry)`. Every derived field carries
`{value, source: "derived", evidence}` where evidence = the fact ids/spans it aggregates.
This is SPEC-2 §5 *minus* what didn't survive scrutiny (noted inline).

### 3.1 System-level `derived` block (one new sub-model on `Record`)

```
agent_count, tool_count, model_count, mcp_count      # entity counts
bespoke_agent_count, framework_agent_count
agents_by_location          = {production: n, example: n, test: n}
autonomy_profile            = min over production agents of autonomy_level
                              (approval_gated < autonomous; absent if no production agents)
capability_flags            = OR over production+example agents' flags (§3.2)
has_unapproved_endpoint     = ∃ ModelRef.endpoint with host ∉ (hosts.yaml ∪ org.gateway_hosts)
has_dynamic_prompts         = ∃ agent PromptBinding.dynamic
has_unresolved_models       = ∃ agent/usage model unresolved(reason)
models_used                 = distinct {model, endpoint, api_style, provider_class}
external_systems            = distinct non-LLM tool external_targets
```

`provider_class` = `vendor_external` (host ∈ hosts.yaml) | `internal_gateway`
(host ∈ org.gateway_hosts) | `self_hosted` (localhost/private-IP literal) | `unknown`.

### 3.2 Per-entity derived fields (inline, additive)

Agent: `role_class` (`supervisor` if out-handoffs > 0; `worker` if in-handoffs > 0 and out = 0;
`router` if route transfers; `solo` otherwise; supervisor wins ties) ·
`autonomy_level` (**simplified vs SPEC-2**: `approval_gated` if ≥1 approval PolicyDef attached,
else `autonomous`; `human_in_loop` requires dominance analysis we don't have — not emitted,
recorded as a v4 candidate in DECISIONS) ·
`capability_flags` over the agent's tools ·
`reachable_tools` (transitive via handoff edges, cycle-safe — the blast radius).

Tool: `is_sensitive` = target ∈ org.sensitive_hosts ∨ side_effect ∈ {admin_mutation, code_exec}.
MCP: `approval_required` (from approval_policy), `transport_risk` (`low` stdio / `medium`
localhost http-sse / `high` remote).
Model usage: `provider_class`.

Capability flags (each `∃ tool` with):
`executes_code` (code_exec) · `mutates_identities` (admin_mutation) · `sends_external`
(external_send) · `reads_sensitive` (read ∧ target ∈ org.sensitive_hosts) ·
`moves_money` (external_send ∧ target ∈ **org.payment_hosts** — new *optional* org-pack key;
SPEC-2's "sensitive_hosts(payment/ledger)" assumed host categories that don't exist. No
`payment_hosts` in the org pack → flag computed as absent, never guessed).

### 3.3 Findings: severity, additively

`FindingRecord` gains **optional** `severity: "high"|"medium"|"low"|"info"|None` and
`subject_ref: str|None` — existing records stay valid. Fixed severity map (report sorts by it):

| Finding | Sev |
|---|---|
| `secret_literal_redacted`, `unapproved_gateway` (new), `high_privilege_agent` (new) | high |
| `unresolved_model` (new), `dynamic_prompt` (new: dynamic prompt on an agent whose tools include write/external_send) | medium |
| `suspected_llm_call`, `ambiguous_agent_shape` | low |
| `llm_call_in_test_or_main`, `orphan_model_usage` (new) | info |

New finding rules are exactly SPEC-2 §5.2 minus `external_opaque_wrapper` (never emitted per
DECISIONS P5). `high_privilege_agent` = agent capability_flags ∩
{moves_money, executes_code, mutates_identities} ≠ ∅, **production/example only** (a test
fixture that "moves money" is not a high finding — location matters).

Assigning severities to *existing* finding kinds changes all goldens: **re-bless once at V1**,
explicitly authorised here.

---

## 4. Record completion (V2)

### 4.1 AI-BOM

`record.ai_dependencies`: the dependency dimension of the inventory.

```
[{package: "openai", version: "1.35.7"|null, source: "poetry.lock",
  used: true, evidence: ["requirements.txt:3", "app/client.py:2"]}]
```

- Packages = the SPEC-1 §6.1 triage AI-package list ∪ org-pack `known_wrapper_packages` roots.
- `version` from lockfiles (machinery exists in `modules/graph.py`); null when unpinned.
- `used` = the package is imported somewhere in the analysed tree (module graph knows);
  a dependency with `used: false` is the classic `ai_signals_only` story — declared but dormant.
- Sorted by package name; deterministic.

### 4.2 Governance slots (carried over from SPEC-2 §3, trimmed per §0)

System level, all `[G]` (scanner never writes `value`): existing five (`owner`, `risk_tier`,
`purpose`, `data_classification`, `regulatory_scope`) **plus** `lifecycle_status`
(`in_dev`/`live`/`retired` when filled), `approval_status`, `approver`, `approval_date`,
`last_review`, `next_review`. New `[X]` slot: `cmdb_app_id` (`{ref, source: "external",
resolved: null}` — link to the bank's application register). Model-usage level: new
`model_approved` `[G]` slot (the bank's approved-model register decision lands here; the [X]
link to that register is the same `EntitlementSlot` shape). Tool level: new
`data_classification_touched` `[G]` slot.

### 4.3 E→G candidates

`GovernanceSlot` gains `candidate: str | None` (as `OwnerSlot` already has), populated only by:

- `regulatory_scope.candidate` ← the enrichment `suggested_aia_risk_category` (the [E] field
  keeps its slot too; the candidate is a *copy into the governance slot*, per SPEC-2 §4.3 —
  never the `value`).
- `purpose.candidate` ← a new one-line `purpose` field in the system-node enrichment response
  (drafted only when `grounded=true`).

Rule preserved: enrichment/derivation may fill `candidate`, never `value`; a human moving
candidate → value happens outside the tool.

### 4.4 External-resolution interfaces (typed stubs, from SPEC-2 §7.3)

Define in `aiscan/inventory/resolvers.py`, implement nothing:

```python
class EntitlementResolver(Protocol):
    def resolve(self, credential_ref: JsonRepr) -> Entitlement | Unresolved: ...
class ConfigResolver(Protocol):
    def resolve(self, symbol: str, environment: str) -> str | Unresolved: ...
class OwnerResolver(Protocol):
    def resolve(self, candidate: str) -> AccountableOwner | Unresolved: ...
```

Default implementations return `Unresolved`. Wiring to a client's IAM / config store /
directory is a later phase; the [X] refs in the record are what these will resolve.

---

## 5. Report — `report.html` (V3)

### 5.1 Product shape

One self-contained HTML file, generated on **every** scan (all three verdicts), from
`record.json` + `graph.json` content only — same inputs ⇒ byte-identical output.

Layout, top to bottom:

1. **Header**: bundle name, repo URL, commit, scan date, verdict badge
   (`AI DETECTED` / `AI SIGNALS ONLY` / `NO AI DETECTED`), owner (value or `candidate`,
   labelled), `suggested_aia_risk_category` shown as "suggested — unconfirmed" when present.
2. **Summary strip**: entity counts with by-location split, capability flags as chips,
   autonomy profile, models_used table (model · endpoint · provider_class · approved?),
   external systems, AI-BOM table.
3. **Governance panel**: the [G]/[X] slots — filled values, `candidate` values clearly
   labelled "candidate — unconfirmed", empty slots shown as awaiting governance (so a
   reviewer sees at a glance what their process still owes this record).
4. **Findings**: severity-sorted table (severity chip, kind, subject, evidence spans,
   detail/rationale).
5. **Agents**: grouped production → example → test (groups collapsed by default for
   example/test; location filter checkboxes toggle visibility). Each agent is a
   `<details>` dropdown: detection method+confidence+evidence, model binding (value,
   endpoint, attribution rung, deployment), system prompt (truncated at 2,000 chars with
   "full text in record.json"; dynamic/origin badges), tools (with side-effect chips and
   external targets), MCP servers, state, policies, handoffs, derived fields (role_class,
   autonomy_level, capability flags, reachable_tools) — and the [E] fields
   (`agent_summary`, `responsibilities`, `guardrails_summary`) each badged **DRAFT (LLM)**
   with `confirmed_by` status, or their `insufficient_evidence` reason.
6. **Tools / MCP servers / Model usages**: same dropdown treatment.
7. **Graph view** (§6).
8. **Footer**: enrichment status banner (`ok` counts / `unavailable: <reason>` /
   "not requested — run with --enrich"), scan health summary, full provenance block.

Dropdowns are native `<details>/<summary>` (zero JS needed); the only JavaScript is the
location filter (~20 lines, inline) and it must degrade gracefully (no JS ⇒ everything
visible).

### 5.2 Security hardening (bank-grade, gated by fixture)

Everything that originated in scanned code or an LLM response — names, prompts, summaries,
paths, model strings, finding details — passes through `html.escape` (attributes included).
A hostile repo defining `Agent(name="<script>…")` or a poisoned enrichment summary must
render as text, never execute. Additionally: `<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">`
— the report can never load an external resource even if escaping failed. Secrets are
already redacted upstream; the report introduces no new copies of anything unredacted.

---

## 6. Graph view (V4)

Inline SVG inside `report.html`, layout computed deterministically in Python:

- **Columns**: 0 system · 1 agents · 2 tools + MCP servers · 3 models. Nodes sorted by id
  within a column; positions are a pure function of sorted order. Node colour by type,
  border style by location (solid production, dashed example, dotted test). Legend always
  rendered.
- **Edges**: agent→tool / agent→mcp / agent→model bindings as lines; agent→agent
  handoffs/routes as arcs within column 1 (arrowheads, labelled `handoff`/`route`).
- **Click-through**: each agent node is an `<a href="#agent-…">` jumping to (and opening)
  its dropdown.
- **Scale guard — the 1,703-agent case is real** (openai-agents-python). Rules, in order:
  (a) any location group with > 20 agents collapses to one aggregate node
  ("test agents (1,557)") whose edges aggregate too;
  (b) hard cap of 150 rendered nodes, collapsing test → example → grouping production by
  top-level directory if still over;
  (c) the legend **always** prints "showing X of Y nodes (Z grouped)" — no silent truncation,
  ever. `record.json` remains the complete inventory; the graph is a view.

---

## 7. Enrichment integration (V5)

### 7.1 Location-aware planning

Node order: system → production agents → production tools/MCP → example agents/tools →
(only with `--enrich-tests`) test nodes. Budget (`max_nodes`, default 200) consumes in that
order, so money goes to the estate, not the test suite. `scan_health.enrichment` gains
`skipped_test_nodes: n` when applicable. The system-node response gains the one-line
`purpose` draft (§4.3); no extra calls — same single call per node.

### 7.2 Report surfacing

The report's enrichment banner is the single place a user learns why summaries are empty
(directly from `scan_health.enrichment.status`/`reason` — machinery already exists). [E]
values always carry the DRAFT badge; `confirmed_by` shown when set.

### 7.3 Enrich-in-place

`aiscan <prior-out-dir> --enrich [--repo PATH]`: loads the existing `record.json`, runs
enrichment only, rewrites `record.json` + regenerates `report.html`. Slices need source:
`--repo` (or the original local target if still present) provides it; when absent,
enrichment proceeds **facts-only** and `scan_health.enrichment.grounding = "facts_only"`
records the weaker grounding honestly. Detection artifacts (`graph.json`, `facts.jsonl`)
are never touched in this mode.

---

## 8. Inventory dataset (V6) — `aiscan/dataset/`

SPEC-2 §6 carried over, stdlib-only. **Records are the source of truth; the dataset is a
pure projection, fully rebuildable**: `aiscan --rebuild-dataset <dir>` walks every
`record.json` under `<dir>`, runs a pure `flatten(record) -> rows`, and writes `inventory.db`
(stdlib `sqlite3`) plus `csv/*.csv` exports (stdlib `csv`) for GRC ingest. Rebuilding twice
from the same records produces identical tables (sqlite file bytes may differ; the gate is
identical table *content* — assert via ordered SELECT dumps — and byte-identical CSVs).

Tables (grain → key columns; keys **bold**):

```
systems  : **bundle_id, scan_id** — name, repo_url, commit, ai_verdict, agent_count,
           tool_count, agents_by_location(cols), autonomy_profile, capability_flags(cols),
           has_unapproved_endpoint, has_dynamic_prompts, has_unresolved_models,
           suggested_aia_risk_category, owner_value, owner_candidate, risk_tier,
           lifecycle_status, approval_status, scanned_at, scanner_ver
agents   : **bundle_id, scan_id, agent_id** — kind, framework, location, role_class,
           autonomy_level, model_value, model_endpoint, api_style, provider_class,
           prompt_dynamic, tool_count, reachable_tool_count, capability_flags(cols),
           detection_method, confidence
tools    : **bundle_id, scan_id, tool_id** — kind, side_effects, external_target,
           is_sensitive, declared_authorisation, credential_ref, capability_class
models   : **bundle_id, scan_id, model_key** — model_value, endpoint, api_style,
           provider_class, task, in_agent, model_approved
findings : **bundle_id, scan_id, finding_id** — kind, severity, subject_ref, detail
```

`scan_id = sha256(bundle_id | commit | scanned_at)[:16]` — many repos and repeated scans
coexist as point-in-time snapshots without a diff engine.

`aiscan/dataset/queries.py` ships named queries (plain SQL strings + a tiny runner), e.g.:
"agents that can move money across the estate", "models grouped by provider_class",
"systems calling an unapproved endpoint", "tools with admin_mutation and no approval
policy", "systems with dynamic prompts and high-privilege tools", "repos with AI
dependencies but no detected usage". Each is one filter over the tables above; each has a
test against the fixture corpus.

---

## 9. Fleet runner (V7) — `aiscan/fleet/`

### 9.1 Enumerate → scan → collect

Input: `--repos FILE` — one git URL or local path per line (`#` comments allowed). For each:
run the normal scan (+ `--enrich` if flagged) into `<out>/records/<repo>-<commit>/`.
Bounded concurrency (default 2), **deterministic collection order** (input order), and a
repo failure is logged into the run summary and skipped — never fatal to the run.

### 9.2 Shared registry & snapshots

One `org_registry.json` for the whole run (SPEC-1 §6.7.1 machinery): a wrapper classified in
repo A is reused in repo B — asserted by a test. Each run is a dated snapshot via `scan_id`;
re-running appends new scan_ids, giving point-in-time estate inventories with no diff engine.

### 9.3 Estate index — `index.html`

Generated from the dataset at end of run (same self-contained/escaping rules as §5): one
table row per system — name, verdict badge, agent/tool counts (by location), capability
flag chips, autonomy, highest finding severity, enrichment status — linking to each repo's
`report.html`. This is the page a bank owner opens first; per-repo reports are the drill-down.

---

## 10. Testing

### New fixtures (same golden discipline as SPEC-1 §8)

| Fixture | Purpose / must-hold |
|---|---|
| `no_ai_clean` | Plain Flask CRUD app, zero AI → `ai_verdict: no_ai`, fast-path record + one-screen report; with `--enrich` forced on in the test: zero LLM calls, visible skip note |
| `ai_deps_only` | `openai` pinned in requirements, never imported → `ai_verdict: ai_signals_only`, empty entities, AI-BOM row `{openai, used: false}` |
| `adversarial_html_escape` | Agent name + prompt + tool docstring containing `<script>`, `"><img onerror=…`, `' onmouseover='` → report contains no unescaped occurrence of any payload (asserted by scanning report bytes); CSP meta present |
| `derived_indicators` | Extends `bespoke_gateway_loop`'s org.yaml with `payment_hosts: [payments-core.internal]` → `moves_money: true`, `high_privilege_agent` finding (high), `autonomy_level: autonomous`, `provider_class: internal_gateway`; without org pack (unit test) → `has_unapproved_endpoint: true` + `unapproved_gateway` finding |

### Goldens & gates

- All 15 existing fixtures re-blessed **once** at V1 (derived fields + severities). After
  that, byte-stable as always.
- `report.html` golden for `bespoke_gateway_loop` (flagship, `ai_detected`),
  `fw_openai_agents_basic` (framework + handoff graph), and `no_ai_clean` (negative page).
  Same normalize-timestamps treatment as `record.json` goldens.
- Graph unit tests: identical record ⇒ byte-identical SVG; synthetic record with 300 test
  agents ⇒ aggregate node + correct "showing X of Y" legend; handoff arc endpoints match
  agent anchors.
- Dataset: rebuild-twice content-identity gate; every named query has an expected-rows test
  over the fixture corpus.
- Fleet: run over 3 local fixture repos (no network) → one dataset with 3 systems, shared
  registry reuse asserted, `index.html` lists all three with correct badges; one
  deliberately-broken path in the list → logged + skipped, run completes.
- Determinism double-run gate now covers `report.html` too.
- Existing gates unchanged: `mypy --strict`, `ruff`, no-exec canary, injection corpus.

## 11. Security & performance constraints (restated deltas)

- Never execute scanned code; git-only subprocess; network = clone + LLM endpoint when
  flagged — **and structurally never on a `no_ai` verdict** (§2.2).
- Report + index: escape-everything + CSP (§5.2); prompts truncated; no external resources;
  no new copies of unredacted content.
- Budgets: `no_ai` p50 ≤ 3 s / p95 ≤ 10 s on 100k LOC; report + graph generation ≤ 2 s on a
  200-node record; dataset rebuild ≤ 5 s per 100 records; SPEC-1 full-scan budgets unchanged.
- No new dependencies (stdlib `html`, `sqlite3`, `csv`, string templates, computed SVG).

## 12. Build order (each phase green before the next: phase tests + mypy/ruff + goldens)

- **V0 — verdict & fast path.** `ai_verdict`, negative-attestation record, LLM guards,
  timing. *Exit: `no_ai_clean` + `ai_deps_only` verdicts correct (BOM row lands in V2 —
  here just verdict + empty entities); LLM-guard test proves zero network calls; timing
  asserted loosely (< 10 s).*
- **V1 — derived engine.** `derive/`, system `derived` block, per-entity fields, findings
  severities + new finding rules; one authorised re-bless. *Exit: `derived_indicators`
  assertions pass; all goldens green post-bless.*
- **V2 — record completion.** AI-BOM incl. `used`; governance slots + `candidate` on
  `GovernanceSlot`; `cmdb_app_id`/`model_approved`/`data_classification_touched`; resolver
  stubs. *Exit: `ai_deps_only` golden complete; schema round-trips; stubs typed and inert.*
- **V3 — report.** Generator + governance panel + escape hardening + enrichment banner.
  *Exit: three report goldens pass; `adversarial_html_escape` gate green; determinism
  double-run includes report.*
- **V4 — graph view.** SVG layout + scale guards + click-through. *Exit: graph unit tests
  incl. 300-agent collapse; `fw_openai_agents_basic` report golden shows 2 agents +
  handoff arc.*
- **V5 — enrichment integration.** Location-aware planning, `--enrich-tests`,
  enrich-in-place + `--repo`, facts-only grounding note, `purpose` candidate wiring.
  *Exit: planning-order unit test; enrich-in-place round-trip on a fixture leaves detection
  artifacts byte-identical and populates [E] + `purpose.candidate`; real-repo check:
  openai-agents-python report renders with test agents grouped and enrichment spent on
  example/production nodes only.*
- **V6 — dataset.** `flatten`, SQLite + CSV writers, `--rebuild-dataset`, named queries.
  *Exit: rebuild content-identity gate; query tests green over fixture corpus.*
- **V7 — fleet runner.** `--repos`, bounded concurrency, shared registry, run summary,
  estate `index.html`. *Exit: 3-repo fleet test incl. failure tolerance and registry reuse.*

## 13. Ambiguity protocol

As SPEC-1 §12: pick the simpler deterministic option, one line in `DECISIONS.md`, no
features beyond this spec, no TODO-stubbed logic — a phase ships working or it isn't done.
