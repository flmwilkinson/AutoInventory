# aiscan — AI/agent inventory scanner

`aiscan` statically scans a **Python and TypeScript/JavaScript** repository and
produces a **bank-grade AI inventory record** of every LLM-using system in it:
framework agents (OpenAI Agents SDK — Python *and* TS, LangChain/LangGraph +
LangGraph.js, CrewAI, the Vercel AI SDK), bespoke hand-rolled agent loops, and
plain LLM call sites. Each agent is bound to its model, endpoint, system
prompt, tools, MCP servers, memory and control policies. On top of the raw
detection it computes risk indicators, drafts human-readable summaries
(optional LLM tier), renders a self-contained HTML report with a graph view,
and can roll many repos up into one queryable estate inventory.

Two properties are the point:

- **Endpoint-agnostic detection.** An LLM call is recognised by the *shape* of
  the request (payload keys, path fragments, response access), not the URL
  host. A call to `https://gw.bank.internal/llm/v1/chat` with
  `{"model": ..., "messages": [...]}` is detected — and the model attributed —
  exactly as a call to `api.openai.com` would be. In-house wrapper SDKs need
  no signatures: they are classified by a fixed-point algorithm and remembered
  in a reusable registry.
- **Nothing is ever guessed.** Unresolvable values are recorded as symbolic
  (`{"symbolic": "env:LLM_MODEL"}`) or `{"unresolved": <reason>}`, never
  invented. Every populated field carries evidence (`file:line`), the method
  that produced it, and a confidence. LLM-drafted text is always marked as a
  draft and never overrides a detected fact.

---

## Install

Requires Python 3.12.

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv/Scripts/python.exe
```

The `aiscan` command is then available at `.venv/Scripts/aiscan.exe`
(Windows) or `.venv/bin/aiscan` (Unix) — or just `aiscan` with the venv
activated. The examples below assume the venv is active.

## Quickstart (60 seconds)

```bash
# 1. Scan a repository (local path or git URL)
aiscan path/to/repo --out aiscan-out
aiscan https://github.com/org/repo --out aiscan-out

# 2. Open the inventory record
#    -> aiscan-out/<repo>-<commit>/inventory.html   (the record)
#    -> aiscan-out/<repo>-<commit>/report.html      (record + technical annex)
```

That's the whole loop: scan, then open `inventory.html` in any browser (both
pages are fully self-contained — no network, works on an air-gapped laptop).
`record.json` next to them is the machine-readable source of truth.

More invocations:

```bash
# Pin a commit; apply your organisation's tailoring pack
aiscan https://github.com/org/repo --commit <sha> --org-pack org.yaml

# Add the optional LLM tiers (see "LLM tiers" below)
aiscan path/to/repo --enrich            # draft the summary/description fields
aiscan path/to/repo --adjudicate        # resolve ambiguous detections

# Re-enrich an existing scan output WITHOUT rescanning (enrich-in-place)
aiscan aiscan-out/repo-abc12345 --enrich --repo path/to/repo

# Fleet mode: scan a whole list of repos into one estate inventory
aiscan --repos repos.txt --out aiscan-fleet
#    -> aiscan-fleet/index.html  (estate overview, links to every report)

# Rebuild the SQLite + CSV dataset from records already on disk
aiscan --rebuild-dataset aiscan-fleet
```

## The verdict — every scan answers one question first

Every record leads with a three-way **`ai_verdict`**:

| Verdict | Meaning | What you get |
|---|---|---|
| `no_ai` | Zero AI signals found by the triage gate | A **negative attestation** in seconds: record + one-screen report stating what was checked, at which commit, when. `--enrich`/`--adjudicate` are guaranteed no-ops (zero network calls, visible "skipped" note). |
| `ai_signals_only` | AI dependencies or strings present, but the full pipeline detected no agents, sinks, or model usages | Empty entity lists + the **AI-BOM** explaining why triage fired (e.g. `openai` pinned in requirements but never imported — a dormant dependency). |
| `ai_detected` | ≥ 1 agent, LLM call, or MCP server | The full record and report described below. |

There is deliberately **no mid-pipeline shortcut**: once any signal fires, the
complete pipeline runs. Recall is the product — a "weak signals, skip the slow
part" optimisation is exactly how an internal-gateway agent gets missed.

**Analysis covers Python and TypeScript/JavaScript** (`.py`, `.ts`, `.tsx`,
`.js`, `.jsx`, `.mjs`, `.cjs`) through one shared detection core — the same
endpoint-agnostic sink engine, wrapper fixed point, and agent-shape detectors
run on both languages, and a scan emits **one record** spanning them, with each
agent tagged by `language`. Dependency-injected clients
(`ctx.openai.chat.completions.create`, common in TS) are caught by call-shape.
For any *other* language still present (Java, Go, …), the record stays honest:
a language census drives a "Partial coverage" banner and an
`unanalysed_language_code` finding, so nothing is ever silently missed.

## What a scan produces

Artifacts land in `<out>/<repo>-<commit>/`:

| File | Contents |
|---|---|
| `record.json` | The inventory record: verdict, agents (location-tagged), tools, MCP servers, model usages, AI-BOM, derived risk indicators, severity-graded findings, governance slots. Machine-readable source of truth. |
| `inventory.html` | **The AI inventory record** (SPEC-6): plain-English, bank-register-shaped — identity, owner/contributor candidates, what the system is, agent cards (model · prompt · tools · autonomy · memory · connections), canonical models, other AI usage, connections & dependencies. No scanner jargon (test-enforced). |
| `report.html` | The same record body + **technical annex**: governance panel, full findings, per-call-site tables, declared env defaults, full BOM, scan health. One shared renderer — the two cannot drift apart. |
| `graph.json` | Canonical Agent Dependency Graph (nodes A/I/M/C/S/G/L; binding + control edges). |
| `facts.jsonl` | Every emitted fact with evidence and method — the audit trail. |
| `scan_health.json` | Timings, parse errors, triage signals, resolver stats, sink counts, LLM-tier status. |
| `scan.log` | Structured log (`--json-logs` for JSON lines). |

### The inventory record (`inventory.html`) — and its annex (`report.html`)

`inventory.html` is the record a reviewer reads (SPEC-6), top to bottom:

1. **Identity** — verdict badge, repo, commit/version, scan date; owner and
   contributor *candidates* (git-derived, labelled); AI code activity dates;
   the partial-coverage banner when other languages are present.
2. **What this system is** — description + purpose (DRAFT-badged when
   LLM-drafted), AI type (`agentic`/`genai` + components such as multi-agent,
   RAG, batch-llm), composition counts, capability phrases, autonomy
   ("no human-approval gate detected in code" — absence is always reported as
   absence-of-evidence), data signals.
3. **Agent cards** — per agent: role, foundation model (canonical display:
   code default + env-configurable keys + declared config defaults), full
   instructions, tools with plain-English side effects, external actions,
   autonomy, memory, connections, guardrails, credential references, evidence.
4. **Models used** — canonical, deduped (one row per model, however many call
   sites); wrapper-mediated calls shown as "via <function> (wrapper)".
5. **Other AI usage** — non-agent call sites grouped by file × model × task.
6. **Connections & dependencies** — external services, MCP servers, AI
   packages, sourcing.
7. **System graph** and the **About this record** assurance line.

`report.html` embeds the identical record body (one shared renderer — the two
cannot drift apart) and appends the **technical annex**: governance panel,
severity-sorted findings, full tool detail, per-call-site model table,
declared env defaults, the full AI-BOM, and scan health/provenance including
resolver give-up counters.

### Reading the record

Every field carries a **sourcing class**:

| Tag | Meaning | Who writes it |
|---|---|---|
| `detected` | Read directly off the code, with evidence + method + confidence | scanner |
| `derived` | Computed deterministically from detected facts (counts, flags, autonomy…) | scanner |
| `enriched` | LLM-drafted prose/classification — **never authoritative**, `confirmed_by` starts null | `--enrich` tier |
| `governance` | Owned by humans/GRC; scanner only ever fills `candidate` | your review process |
| `external` | A reference another system resolves (entitlements, CMDB) | out of band |

Key record content:

- **Agents** are tagged `location: production | example | test` from their
  file path. Nothing detected is ever dropped — test-suite agents stay in the
  record, tagged, so they can be filtered rather than silently lost.
- **Derived indicators** (all deterministic, all evidence-bearing):
  per-agent `role_class` (supervisor/worker/router/solo), `autonomy_level`
  (`approval_gated` if an approval policy is attached, else `autonomous`),
  `capability_flags` (`moves_money`, `executes_code`, `mutates_identities`,
  `sends_external`, `reads_sensitive`), `reachable_tools` (transitive blast
  radius via handoffs); system-level roll-ups plus `models_used` with a
  `provider_class` (`vendor_external` / `internal_gateway` / `self_hosted` /
  `unknown`) and `has_unapproved_endpoint`.
- **AI-BOM** (`ai_dependencies`): every declared AI package with its pinned
  version, where it was declared, and whether the code actually imports it.
- **Findings** are severity-graded: `secret_literal_redacted`,
  `unapproved_gateway`, `high_privilege_agent` (high) ·
  `unresolved_model`, `dynamic_prompt` (medium) · `suspected_llm_call`,
  `ambiguous_agent_shape` (low) · `llm_call_in_test_or_main`,
  `orphan_model_usage` (info). `high_privilege_agent` only fires for
  production/example agents — a test fixture that "moves money" is not a high
  finding.

## LLM tiers (optional, both off by default)

Detection is fully deterministic and complete without any LLM. Two optional
tiers layer on top. Both use the **same OpenAI-compatible endpoint** and
`.env` credentials (stdlib HTTP — no SDK dependency), and both treat scanned
code strictly as data, never instructions (prompt injection in scanned code is
inert — there is a test for it):

- **`--enrich`** drafts *descriptions* of what was already detected: the
  system/agent/tool/MCP summaries, capability class, data domain, suggested
  AI-Act risk category, and a `purpose` candidate for the governance panel.
  Grounded in the detected facts plus one bounded code slice per node; if the
  facts are too thin it records `insufficient_evidence` with a null value —
  never a fabrication. Cached by node content hash, so unchanged nodes are
  free on re-runs. **Test-suite nodes are skipped by default** (the budget
  goes to the estate); add `--enrich-tests` to include them.
- **`--adjudicate`** resolves *detection* ambiguity — the handful of cases the
  deterministic layers abstained on (ambiguous agent shapes, suspected-but-weak
  sinks). Budget-capped (default 20 calls), cached; anything it adds is marked
  `llm_adjudicated` at `medium` confidence and never overrides a deterministic
  fact.

### Setting up `.env`

Copy `.env.example` to `.env`. **For regular OpenAI you need exactly one
line:**

```bash
OPENAI_API_KEY=sk-...your-key...
```

Leave everything else commented out. Only set the base URL if you use a
gateway or Azure — and then it must be a real `https://` URL:

```bash
# AISCAN_ADJUDICATE_BASE_URL=https://gw.internal.example/v1  # default: https://api.openai.com/v1
# AISCAN_ADJUDICATE_MODEL=gpt-4o-mini                        # used by both tiers
# AISCAN_ADJUDICATE_BUDGET=20                                # adjudication call cap
# AISCAN_ADJUDICATE_API_KEY=sk-...                           # dedicated key (overrides OPENAI_API_KEY)
```

The key is read from the environment at call time and is never stored, logged,
or written to any artifact; `.env` is gitignored. Code slices sent to the
endpoint are re-scrubbed for secrets first. With both flags off, output is
byte-identical to a plain scan.

**Did it work?** Check the report's footer banner (or
`scan_health.enrichment` in the record): `ok — N drafted` means yes;
`unavailable: <reason>` tells you exactly what to fix (bad URL, missing key);
`not requested` means you didn't pass `--enrich`. It is never silent.

### Enrich-in-place

Already scanned, then fixed your `.env`? Don't rescan — point `aiscan` at the
existing output directory:

```bash
aiscan aiscan-out/repo-abc12345 --enrich --repo path/to/repo
```

This drafts the summaries and rewrites only `record.json` + `report.html`;
the detection artifacts (`graph.json`, `facts.jsonl`) are untouched. `--repo`
supplies the source tree for grounding slices; without it enrichment still
runs on facts alone and records `grounding: facts_only` honestly.

## The org pack — per-organisation tailoring

```yaml
# org.yaml
gateway_hosts: ["gw.internal.example", "llm.bank.internal"]  # YOUR approved LLM gateways
known_wrapper_packages:                                       # in-house SDK seeds (optional)
  - {fqname: "bank_ai.LLMClient", attribution: "passthrough"}
sensitive_hosts: ["payments-core.internal", "ledger.internal"]  # reads/sends here are flagged
payment_hosts: ["payments-core.internal"]                     # enables the moves_money flag
```

Pass it with `--org-pack org.yaml`. It drives: which endpoints count as
*approved* (anything else literal becomes an `unapproved_gateway` high
finding), which tool targets are *sensitive*, and which flag as *moving
money*. Without a `payment_hosts` list the `moves_money` flag is never
inferred — no guessing.

Wrappers classified during scanning are persisted to `org_registry.json`
(keyed by fully-qualified name + content hash, evidence attached), so later
scans — and other repos in a fleet run — reuse them for free.

## Fleet mode — the estate inventory

```
# repos.txt — one git URL or local path per line, # comments allowed
https://github.com/org/payments-assistant
https://github.com/org/kyc-agent
C:\code\internal-copilot
```

```bash
aiscan --repos repos.txt --out aiscan-fleet [--org-pack org.yaml] [--enrich]
```

This scans every repo (sequentially, in input order; one failure is logged and
skipped, never fatal) and produces:

```
aiscan-fleet/
  index.html            # estate overview: one row per system — verdict badge,
                        # agents by location, capability flags, autonomy,
                        # worst finding, enrichment status — linking to each report
  records/NNN-<name>/<repo>-<commit>/   # full per-repo artifact set
  inventory.db          # SQLite dataset (stdlib; rebuildable projection)
  csv/systems.csv agents.csv tools.csv models.csv findings.csv
  org_registry.json     # shared wrapper registry for the whole run
  fleet_summary.json    # machine-readable run summary incl. failures
```

`index.html` is the page an estate owner opens first; per-repo reports are the
drill-down.

### The dataset

`inventory.db` + `csv/` are a **pure projection** of the records — rebuild any
time with `aiscan --rebuild-dataset <dir>` (it walks every `record.json` under
the directory). No fact lives only in a table. Named estate queries ship in
`aiscan/dataset/queries.py`, e.g.:

- `agents_move_money` — agents that can move money across the estate
- `systems_unapproved_endpoint` — systems calling an unapproved LLM endpoint
- `models_by_provider_class` — models in use, vendor vs internal gateway vs self-hosted
- `admin_tools_without_approval` — admin-mutating tools with no approval gate
- `dynamic_prompt_high_privilege` — dynamic prompts on high-privilege systems
- `dormant_ai_repos` — AI dependencies declared, nothing detected
- `high_findings` — all high-severity findings estate-wide

## CLI reference

```
aiscan [TARGET] [OPTIONS]
```

| Argument / option | Meaning |
|---|---|
| `TARGET` | Local path, git URL, or a prior scan output dir (enrich-in-place) |
| `--commit SHA` | Check out a specific commit (git URLs; recorded either way) |
| `--out DIR` | Artifact root (default `./aiscan-out`) |
| `--org-pack FILE` | Organisation tailoring pack (YAML, see above) |
| `--json-logs` | JSON log lines instead of plain text |
| `--enrich` | Draft the `[E]` summary fields (LLM; needs `.env` key) |
| `--enrich-tests` | Also enrich test-suite nodes (skipped by default) |
| `--adjudicate` | LLM adjudication of ambiguous detections (budget-capped) |
| `--repo PATH` | Source tree for slices in enrich-in-place mode |
| `--repos FILE` | Fleet mode: scan every repo in the list file |
| `--rebuild-dataset DIR` | Rebuild `inventory.db` + `csv/` from records under DIR, then exit |

## Guarantees

- The scanner **never executes scanned code** and never installs its
  dependencies. `git` is the only subprocess. The only network use is
  `git clone` — plus the LLM endpoint when you explicitly pass `--enrich` /
  `--adjudicate`, and **never** on a `no_ai` verdict.
- Secret-shaped literals (API keys, PEM blocks, tokens) are redacted at
  IR-lowering time — before anything is cached or persisted — and reported as
  findings. Slices sent to an LLM are re-scrubbed.
- Output is deterministic: two scans of the same commit produce byte-identical
  `graph.json` and `facts.jsonl`, and identical `record.json` / `report.html`
  apart from the `scanned_at` timestamp.
- The report escapes every string that originated in scanned code or an LLM
  response and carries a CSP forbidding all external resources — a hostile
  repo cannot script the reviewer's browser (gated by an adversarial fixture).
- Every artifact is reproducible from `(repo, commit, scanner version,
  rule-pack versions, org pack)` — all recorded in `inventory_provenance`.

## Performance

- Full scan: p50 ≤ 30 s on 10k–100k LOC Python (p95 ≤ 3 min).
- `no_ai` fast path: p50 ≤ 3 s / p95 ≤ 10 s on 100k LOC.
- Resolver budgets are hard-capped and surfaced in `scan_health`; breaches
  degrade to `unresolved` values, never crashes or hangs.

## Development

```bash
.venv/Scripts/python.exe -m pytest          # unit + golden suites (~260 tests)
.venv/Scripts/python.exe -m mypy            # --strict via pyproject
.venv/Scripts/python.exe -m ruff check .
```

Fixture goldens live in `tests/fixtures/<name>/expected/` (record, graph, and
for three fixtures the report). The harness:

```bash
python -m tests.harness compare <fixture>   # diff a fresh scan vs goldens
python -m tests.harness bless <fixture>     # re-bless after reviewed changes
python -m tests.harness metrics             # per-entity precision/recall table
AISCAN_SMOKE=1 pytest tests/test_smoke_real_repos.py   # network smoke ×3
```

Specs: `SPEC.md` (detection core, P0–P7) · `SPEC-2.md` (enrichment layer) ·
`SPEC-3.md` (verdict path, derived indicators, report, graph, dataset, fleet —
V0–V7) · `SPEC-4.md` (TypeScript/JavaScript frontend — W0–W7) · `SPEC-5.md`
(resolution depth & capability recall — X0–X5: BoolOp/ternary union semantics,
reachable-return call resolution, embeddings chain recall, bespoke ToolDef
extraction with dispatcher-linked side effects, call-closure attribution,
config-at-rest env candidates) · `SPEC-6.md` (the AI inventory record —
Y0–Y3: canonical model display, usage grouping, system type, git provenance,
inventory.html + report-as-annex). Every under-determined design choice is one
line in `DECISIONS.md`.
