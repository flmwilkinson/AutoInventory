# SPEC-5.md — Resolution Depth & Capability Recall (v5 Build Brief)

**Audience: Claude Code.** Build this **after** SPEC-4 (W0–W7 green). Same engineering
standards as SPEC-1 §3 (`mypy --strict`, Pydantic v2, determinism, never execute scanned
code, evidence-or-it-didn't-happen). This brief adds **no new subsystem**: every phase is a
completion or correction of an existing engine, sized to the smallest diff that closes a
demonstrated recall/precision gap. No new dependencies. No new abstractions unless a phase
explicitly names one.

---

## 0. Context and mission

The DocGen rescan after SPEC-4 is the motivating incident — three classes of defect, each
traced to a specific mechanism:

1. **False "unresolved".** All ~20 `unresolved: dynamic` model findings trace to the idiom
   `const LLM_MODEL = getModelName('fast')` → `MODEL_FAST = env || env || ternary ||
   'gpt-4o-mini'`. Two independent resolver give-ups fire: (a) the resolver has **no
   `BoolOpE` case** — TS `||`/`??`/ternary all lower to `BoolOpE`
   (`parse/ts_tree_sitter.py:861-882`) and fall through `case _` to `Top("dynamic")`
   (`resolve/engine.py:349-350`), despite DECISIONS W0/W2 claiming union semantics exist;
   (b) `getModelName` has three `return`s and `_K_RET = 2` (`resolve/engine.py:116,747`)
   kills the inline before values are looked at. The truthful answer —
   `env:MODEL_FAST ∪ "gpt-4o-mini"` — is expressible in the existing value domain today.
2. **Silent recall holes.** Every `embeddings.create` call behind any indirection was
   missed (all of `apps/api` produced **zero facts**): `_CHAIN_SHAPE_SUFFIXES`
   (`sinks/engine.py:98-105`) omits `embeddings.create`, and an embeddings payload
   `{model, input}` scores 2 — below `SUSPECT_THRESHOLD` — in the shape table. **Tools: 0**
   on a repo defining three OpenAI function tools + an `executeTool` dispatcher
   (`llm-tools.ts`): F4 *sees* the `tools:` key but nothing lifts the schemas into
   ToolDefs, so blast radius and capability flags are structurally empty for every bespoke
   repo.
3. **Wrong attribution surface.** Agent membership is a lexical span: helpers called by an
   agent in the same file emit `orphan_model_usage` / `in_agent: false`. The BOM cell
   still prints "outside Python-only analysis" for npm (SPEC-4 §9 W6b regression), and the
   report renders off-state capability chips and "Unapproved endpoint: False" with no org
   pack — all three read as wrong answers delivered confidently.

Mission: make the resolver's documented semantics true, close the two recall holes, fix
the attribution surface — and bind symbolic env values to the repo's own declared config
so a reviewer sees `env:MODEL_FAST (default "gpt-4o-mini", pinned in infra/env.example)`
instead of `unresolved: dynamic`.

### Non-goals and permanent trims

- **No branch pruning in general flow.** `_apply(IfS)` keeps union-of-branches. Test
  evaluation is used **only** for return-reachability inside `_call_function_body` (§2).
- **No type analysis** (unchanged from SPEC-4). The DI-client hole is closed by chain
  shape (§3), not by reading TS annotations.
- **No new languages, no runtime/telemetry reconciliation, no CycloneDX export, no fleet
  parallelism** — future specs.
- **No detector-threshold tuning.** F1–F5 feature logic and promotion rules are untouched;
  a call site that is genuinely outside every agent stays an honest orphan.
- **Env binding never overrides detection.** A `.env.example` value is *declared config*,
  recorded as a candidate with provenance — it never replaces a `Symbolic` with a bare
  string in a detected fact.

---

## 1. Deliverable and acceptance (definition of done)

1. **DocGen proof (the headline).** Rescanning the DocGen clone yields: zero
   `unresolved_model` findings from the `LLM_MODEL`/`getModelName` idiom (models resolve
   to symbolic∪literal unions); the `apps/api` embeddings sites, `code-intelligence.ts`
   embeddings sites, and `model-gateway.ts` `embed()` all emit model usages with
   `task: embedding`; **Tools: 3** (`generate_chart`, `execute_python_analysis`,
   `create_data_table`) bound to the agents whose sink payloads carry them, with
   side-effects classified (`execute_python_analysis` → `code_exec`); helper call sites
   invoked by detected agents are attributed (`in_agent: true`), not orphans; the npm BOM
   row shows real `used` state; env-default candidates from `infra/env.example` appear
   against symbolic models.
2. **Resolver semantics = documented semantics.** BoolOp/ternary union resolution exists
   and the DECISIONS W0/W2 lines describing it are true. `resolver.top.dynamic` on the
   DocGen scan drops by an order of magnitude.
3. All existing goldens: changes are **reviewed value improvements only** (a previously
   `unresolved` field becoming symbolic/union/literal, a previously missing fact
   appearing). No detected fact disappears, no confidence weakens, no span moves.
   Determinism double-run holds. `mypy --strict`, `ruff`, full pytest green at every
   phase gate.
4. Performance budgets unchanged (SPEC-1 §10; resolver budgets and memo behaviour
   untouched apart from the specified cap changes).

---

## 2. X0 — Resolver value-domain completeness (`resolve/engine.py` only)

The single highest-leverage phase. Three additions to `_resolve_inner` + one change to
`_call_function_body`. No new files.

### 2.1 `BoolOpE` — union of operands with or-semantics

New case in `_resolve_inner`:

- `op == "or"` (also covers TS `||`, `??`, and ternary, which lowers to `BoolOpE("or")`):
  resolve operands left to right, unioning as you go, with two refinements:
  - **Falsy drop:** `NoneV` and `Bool(False)` are dropped from every operand **except the
    last** (they cannot be the result of `or`; for `??` this is exactly nullish
    semantics). Other falsy literals (`Str("")`, `Num(0)`) are kept — dropping them is
    wrong for `??` and the noise is negligible.
  - **Truthy short-circuit:** if an operand resolves to a *single definitely-truthy
    literal* (non-empty `Str`, `Bool(True)`, nonzero `Num`), stop — later operands are
    unreachable. `Symbolic`/`Template`/`Top`/refs are not decidable: keep unioning.
- `op == "and"`: plain union of all operands (over-approximation, matches ADG semantics).
  No falsy drop, no short-circuit — `and` chains feed loop guards (F5), not values worth
  refining.
- `Top` members are **kept** in the union (honest "or something unknown"); a union that is
  all-`Top` behaves exactly as today.

### 2.2 `UnaryOpE` and `CompareE` — literal folding only

- `UnaryOpE(op="not")` over a single `Bool` → the negated `Bool`; anything else →
  `Top("dynamic")` (explicitly, not via fall-through).
- `CompareE` with exactly one op in `{"==", "!=", "===", "!==", "is", "is not"}` and both
  sides resolving to single literals (`Str`/`Num`/`Bool`/`NoneV`) → `Bool` of the
  comparison (`===`≙`==`, `!==`≙`!=`). Anything else → `Top("dynamic")`.

These exist to serve §2.3's reachability pruning; they also stop Compare/Unary
expressions inflating `top.dynamic` counts.

### 2.3 Call-return resolution: reachable returns + flow-free lift

`_call_function_body` today: `len(all returns) > _K_RET → Top("dynamic")`. Replace with:

1. **Reachable-return collection.** Walk the body statement list collecting `ReturnS`,
   but at an `IfS` whose test resolves (under the param-bound scope) to a single
   `Bool` literal, descend **only** into the taken branch. Opaque tests: both branches,
   as today. This makes `getModelName('fast')` — a lowered `switch` — yield exactly the
   one return its literal argument selects. Bounded: reuse the existing depth/deadline
   budgets; no new knobs.
2. **Flow-free lift of the cap.** If reachable returns still exceed `_K_RET`, do not give
   up when every return value is **flow-free**: its free names are not assigned anywhere
   in the function body (params and module-level names are fine). Flow-free returns are
   resolved against the param-bound scope without a body walk; cap the count at 8 (a
   registry-of-getters ceiling, one DECISIONS line). Non-flow-free over-cap: `Top`
   as today.

`_K_RET` itself, memoisation rules, and all budgets are unchanged.

### 2.4 Attribution nuance (one function, `sinks/attribution.py`)

`attribute_model` on a multi-value set currently stamps `RUNG_CONSTANT`. Refine: if the
set contains any `Symbolic`, method is `RUNG_SYMBOLIC`; a mixed union containing `Top`
members stays `resolved=True` **iff** at least one member is `Str`/`Symbolic`/`Template`
(partial knowledge is knowledge; the union repr keeps the `{"unresolved": …}` member
visibly).

*Exit:* resolver unit cases: `a || b` env chain, `??`, ternary-lowered BoolOp, truthy
short-circuit, falsy drop, `and` union, not/compare folding, switch-getter with literal
arg (exact arm), flow-free 3-return getter, non-flow-free 3-return still Top. DECISIONS
W0/W2 lines corrected to describe reality. Full suite green; goldens re-blessed only for
value improvements.

---

## 3. X1 — Embeddings and chain-suffix recall (`sinks/`)

All data/threshold work in the existing engines; no new detection path.

- `_CHAIN_SHAPE_SUFFIXES` += `"embeddings.create"` (also add `"images.generate"` — same
  registry family, zero extra machinery).
- `_chain_shape_inputs` returns the matched suffix; `ShapeInputs` gains
  `chain_hint: str | None`. `score_shape`: a non-None chain hint scores **+3** with
  signal `chain:sdk-suffix` — the suffix is the path-fragment analogue for SDK-style
  calls (an options object with `model` + content keys on a `*.embeddings.create` chain
  is decisive by the same logic as a URL containing `/chat/completions`).
- Task inference: chain hint starting `embeddings` → `task: embedding`
  (`images` → `image`); `"/embeddings"` joins `PATH_FRAGMENTS` for the raw-HTTP case.
- The DI-typed-param case (`openai: OpenAI` as a function parameter) needs nothing more:
  root resolves `Top("unbound")`, chain shape carries it.

*Exit:* new fixture `ts_di_embeddings` (client as typed param + module-level
`process.env.X || literal` model — exercises X0+X1 together): golden asserts one model
usage, `task: embedding`, model = symbolic∪literal union. DocGen smoke assertion: ≥ 5
embeddings usages, `apps/api` no longer fact-free.

---

## 4. X2 — Bespoke ToolDef extraction (`frontends/bespoke/tools.py`, new, small)

The one new module of this spec. Input: classified sinks (payloads already resolved) +
resolver. Output: existing fact types only (`ToolDefF`, tool bindings to agents) — the
graph/report/derive layers consume them with **zero changes**.

- **Schema lift.** For each sink whose payload (or `tools=` kwarg) resolves to a
  `ListVal` of `DictVal`s shaped `{type: "function", function: {name, description?,
  parameters?}}` (also the flat `{name, description, input_schema}` Anthropic form):
  emit one `ToolDefF` per entry — name, description, declared parameter keys; evidence =
  the defining literal's span (the `ListVal` element's provenance, i.e. where the schema
  is written, not the call site). Dedupe by (name, content hash) across sinks — DocGen
  passes the same array to four agents; that is one tool set, four bindings.
- **Binding.** The tool binds to the agent whose anchor (or §5 closure) contains the
  sink. Sinks outside any agent: ToolDefs still emitted, surfaced by the existing
  unbound-tool machinery (`graph/queries.py`) — never dropped.
- **Implementation linking + side-effects.** Best-effort, same module set as the schema
  literal: a function whose body dispatches on a parameter against the tool's name —
  `case "generate_chart":` / `if (name === "generate_chart")` (the lowered forms are
  `IfS` chains over `CompareE` with a `StrE`, already in the IR) — links the branch's
  called `FuncRef` as the tool's implementation; classify it with the **existing**
  side-effect table (`sinks/side_effects.py`). No dispatcher found → side-effects
  `unknown`, honestly. No new side-effect vocabulary.

*Exit:* fixture `ts_bespoke_tools_dispatch` (tools array + switch dispatcher + one
`child_process` case): golden asserts 3 ToolDefs, one `code_exec` side-effect, bindings
to the fixture agent, blast radius non-empty. DocGen smoke: Tools = 3, capability flag
`executes_code` present on agents reaching `execute_python_analysis`.

---

## 5. X3 — Attribution closure (`frontends/bespoke/` + derive)

Agent membership becomes a **bounded call closure** instead of a lexical span:

- Seed: the agent's anchor function. Expand: callees that resolve to internal `FuncRef`s
  (same repo), depth ≤ 2 hops, skipping functions that are themselves agent anchors or
  classified wrappers (wrappers keep their own suppression semantics). Deterministic
  order, memoised per anchor.
- A sink inside the closure: `in_agent: true`, attributed to that agent (model/prompt
  bindings included, same emit path as span-internal sinks). A sink reachable from two
  agents' closures binds to **both** (over-approximation, ADG semantics, one DECISIONS
  line). `orphan_model_usage` fires only for sinks in no closure.

*Exit:* fixture `bespoke_helper_attribution` (agent loop calling a same-file helper that
makes the LLM call): helper's usage attributed, zero orphans. DocGen smoke:
`react-agent.ts` helper sites no longer orphaned.

---

## 6. X4 — Config-at-rest env binding (`ingest/env_defaults.py`, new, small)

Static join from `Symbolic("env", KEY)` to the repo's own declared configuration. Parse
(stdlib only, never executed): `.env`, `.env.*`, `*env.example` (dotenv `KEY=value`
lines), `docker-compose*.yml` `environment:` maps/lists, k8s manifest `env:`
name/value pairs under `**/k8s/**`/`**/infra/**`. Secret-shaped values are redacted by
the existing SPEC-1 §6.5 patterns at parse time.

- New fact `EnvDefaultF(key, value, source_file, form)` (`form: example | pinned` —
  `.env.example`/`env.example` are `example`, committed `.env`/compose/k8s are `pinned`).
  Emitted once per (key, value, file); facts.jsonl carries the evidence like everything
  else.
- **Presentation join only.** Report and record: wherever a model/endpoint value renders
  a `Symbolic(env, K)` with known defaults, attach the candidates:
  `env:MODEL_FAST → "gpt-4o-mini" (example — infra/env.example)`. `models_used` entries
  gain an optional `env_candidates` list. Detected values are never rewritten; derived
  indicators do not consume candidates (a candidate is not a detection).

*Exit:* fixture `env_defaults_join` (code reads `process.env.MODEL || "x"`, repo carries
`.env.example` pinning MODEL): record shows the candidate with provenance; report renders
it inline; scan without the file is byte-identical to today.

---

## 7. X5 — Surface honesty debt (report + BOM, all ≤ 10-line diffs)

- **BOM npm `used`** (SPEC-4 §9 completion): `used = imported in analysed TS/JS code`
  from the module graph; the "outside Python-only analysis" cell dies; **every**
  declaring manifest is listed (DocGen declares `openai` in four package.jsons, the
  report shows one).
- **Capability chips:** render only set flags; no flags → "none detected".
- **Unapproved endpoint:** three-state — `False` only when an org pack was loaded;
  otherwise `unknown (no org pack)`.
- **Union rendering:** `_val` renders `{"union": [...]}` compactly (`a ∪ b ∪ …`, members
  in existing single-value renderings) instead of raw JSON.
- **Resolver visibility:** the report's Scan-health section prints the
  `resolver.top` reason counts (the data already exists in scan_health).

*Exit:* report goldens re-blessed; a no-org-pack scan shows `unknown`, an org-pack scan
shows `True/False`.

---

## 8. Testing, gates, build order

### Fixture ladder (each: repo + golden record/graph; report golden where marked)

| Fixture | Must-hold |
|---|---|
| `ts_env_fallback_model` | `process.env.A \|\| process.env.B \|\| 'lit'` → union model, `attribution:config_symbolic`, zero unresolved findings |
| `switch_getter_model` | 3-arm getter + literal arg → exact arm's value (Python and TS twins) |
| `ts_di_embeddings` | typed-param client, `embeddings.create` → embedding usage, symbolic∪literal model |
| `ts_bespoke_tools_dispatch` (report golden) | 3 ToolDefs, dispatcher-linked `code_exec`, agent bindings, blast radius |
| `bespoke_helper_attribution` | helper sink attributed via closure, zero orphans |
| `env_defaults_join` (report golden) | env candidate with provenance; absent file ⇒ byte-identical output |

### Gates (every phase)

- Existing goldens: value-improvement diffs only, individually reviewed at bless time —
  a golden where a fact *disappears* or a confidence *drops* fails the phase.
- Determinism double-run (record, graph, facts, report) over all fixtures.
- `mypy --strict`, `ruff`, no-exec canary, injection corpus unchanged.
- DocGen + AgentTape smokes (`AISCAN_SMOKE=1`) with the §1.1 assertions.

### Build order (each green before the next)

**X0** resolver (§2) → **X1** embeddings/chain (§3) → **X2** tools (§4) → **X3**
closure (§5) → **X4** env binding (§6) → **X5** surface (§7) → **X6 hardening**: full
ladder, DocGen rescan asserting §1.1 end-to-end, README/DECISIONS sweep.

## 9. Ambiguity protocol

As SPEC-1 §12: simpler deterministic option, one DECISIONS line, no features beyond this
spec, no TODO-stubbed detection — a phase ships working behaviour or it isn't done.
