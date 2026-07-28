# SPEC-4.md — Multi-Language Detection: the TypeScript/JavaScript Frontend (v4 Build Brief)

**Audience: Claude Code.** Build this **after** SPEC-3 (all of V0–V7 green — they are, at time
of writing). Same engineering standards as SPEC-1 §3 (`mypy --strict`, Pydantic v2,
determinism, never execute scanned code, evidence-or-it-didn't-happen). This brief adds a
**second language frontend** behind the existing `Parser` interface; it re-runs SPEC-1's
proven ladder (parse → modules → resolver → sinks → F1 → F2) for TypeScript/JavaScript and
merges both languages into one record. It is SPEC-1-scale work; treat each phase gate
accordingly.

---

## 0. Context and mission

The DocGen case is the motivating incident: a TypeScript monorepo with real agent loops
(`evidence-agent.ts`, `chart-react-agent.ts`), an `openai@^4.28.0` dependency, and a
`packages/prompts/agents/**` roster — and aiscan could only say "AI signals, partial
coverage". SPEC-3's honesty machinery made the gap *visible*; SPEC-4 closes it. Agent
frameworks live overwhelmingly in two ecosystems — Python and TypeScript/JavaScript — so
TS/JS support converts most "partial coverage" estates into full detections.

Design bet already placed by SPEC-1 (§1 "Deliberate MVP simplifications"): the parser is an
interface, the IR is the abstraction boundary, and **everything downstream of parsing is
language-neutral** — the value domain, resolver algorithm, sink scoring, wrapper fixed
point, agent-shape features F1–F5, graph, record, report. SPEC-4 must not fork any of those;
it supplies a new `Parser` backend, a TS module graph, registry/pack *data*, and small
resolver additions. Where a downstream change is unavoidable it must be language-parametric,
never `if language == "typescript"` special-casing in detection logic.

### Non-goals and permanent trims

- **Other languages** (Java, Go, C#, Ruby, Rust…): census-only, as today. Each is its own
  future frontend behind the same interface. The coverage banner keeps telling the truth.
- **No Node/npm execution.** No `tsc`, no `node`, no installing the repo's packages. `git`
  remains the only subprocess. tree-sitter parses text; that is all.
- **No type analysis.** TypeScript type annotations are stripped at lowering. Detection is
  value-flow-based; types would add complexity and no recall.
- **No JSX semantics.** JSX elements lower to `Opaque` expressions (their *children* —
  calls, props expressions — still lower normally). An agent defined inside a React
  component is found via its calls, not its markup.
- **No promise-chain dataflow.** `await` unwraps; `.then(cb)`/`.catch` chains lower the
  callback body normally but the *chain value* is `Top(dynamic)`. An honest miss — the sink
  still fires; only shape features spanning a `.then` boundary are lost.
- **No incremental parsing** (tree-sitter supports it; we do not use it yet).
- **Framework packs**: exactly `@openai/agents`, `@langchain/langgraph` (+ `@langchain/*`
  chat models), and Vercel `ai`. Others are data additions later.

---

## 1. Deliverable and acceptance (definition of done)

1. `aiscan <repo>` analyses `.py` **and** `.ts/.tsx/.js/.jsx/.mjs/.cjs` in one scan, one
   record, one graph, one report. Agents carry a `language` tag; the census/coverage banner
   no longer lists TS/JS as unanalysed.
2. **DocGen proof (the headline):** scanning the DocGen clone yields `ai_verdict:
   ai_detected`; the bespoke loop in `evidence-agent.ts` is recovered as an agent (or, where
   framework-shaped, via F1) with model + endpoint attributed; the `openai` npm BOM row
   shows `used: true`; the prompt roster under `packages/prompts/agents/**` is bound or
   surfaced as declared candidates; no partial-coverage banner for TS/JS.
3. **TS gateway attribution proof:** the `ts_bespoke_gateway_loop` fixture (a `fetch` loop
   against `https://gw.internal.example/llm/v1/chat`) yields a bespoke AgentDef with
   `model = "internal-x1"` and the gateway endpoint — no known host involved. The
   endpoint-agnostic property holds in TS.
4. **Barrel proof:** a TS monorepo re-exporting `Agent` through `index.ts` barrels still
   matches F1 rules (the TS twin of the Python re-export canonicalisation).
5. All new fixture goldens pass; all existing Python goldens **unchanged** (byte-stable —
   adding a language must not perturb Python detection); determinism double-run holds with
   pinned grammar versions; `mypy --strict`, `ruff`, full pytest green.
6. Performance budgets (§10) hold on DocGen and one large public TS repo smoke.

---

## 2. Parser backend (W0 — go/no-go gate)

- Dependencies (justify in DECISIONS, pin exact versions): `tree-sitter==0.26.0`,
  `tree-sitter-typescript==0.23.2`, `tree-sitter-javascript==0.25.0`. Wheel availability
  for win/linux/mac py3.12 is **verified** (resolved against this project's venv
  2026-07-24). Grammar + core versions are recorded in `inventory_provenance` (a grammar
  upgrade can shift spans, hence goldens — upgrades are deliberate, blessed events).
- New backend `parse/ts_tree_sitter.py` implementing the existing
  `Parser.parse(path, source) -> ModuleIR | ParseErrorInfo`. `.ts/.tsx` use the typescript
  grammar (tsx dialect for `.tsx`); `.js/.jsx/.mjs/.cjs` the javascript grammar. Nothing
  outside `parse/` may import `tree_sitter`.
- A `parse/registry.py` maps extension → parser backend and owns `ANALYSED_EXTS`; the CLI
  file-discovery, census banner, and BOM `used` logic all consume it (single source of
  truth for "what we analyse").

### 2.1 Lowering to the existing IR (the complete required list)

| TS/JS construct | IR |
|---|---|
| `import x from 'm'` / `import {a as b}` / `import * as ns` / `require('m')` | `ImportIR` (default import → name `default` aliased; namespace → ModuleRef semantics) |
| `export` / `export default` / `export {a} from 'm'` (re-export) | symbol-table exports (§3); re-exports feed barrel canonicalisation |
| `const/let/var x = e` | `AssignS` (last-wins flow, as Python) |
| `function f(a, b) {}` / arrow `(a) => e` / async fns | `FuncIR` (arrow with expression body → `ReturnS(e)`; `async` flag; `await e` unwraps to `e`) |
| `class C { constructor(){} m(){} }` | `ClassIR` (+ `this.x = e` in constructor ≙ `self.x`) |
| `new C(args)` | `CallE` on the class ref (resolver yields `ClassInstance`, as Python ctor calls) |
| member chains `a.b.c`, optional `a?.b` | `AttrE` layers (optional-chaining treated as plain access) |
| `{k: v, ...rest}` | `DictE` (spread ⇒ `open=True`); shorthand `{model}` ⇒ entry `model: NameE(model)` |
| `[a, b]` | `ListE` |
| template literal `` `x ${e}` `` | `FStrE` (holes best-effort, as Python) |
| `if/else`, ternary `c ? a : b` | `IfS` (ternary as expression-level IfS chain per the `match` precedent) |
| `while`, `for`, `for..of/in` | `WhileS`/`ForS` |
| `try/catch/finally` | `TryS` |
| `const {choices} = resp` / `const [a] = xs` | **required**: lower to member/subscript assigns (`choices = resp.choices`) — F2 dispatch detection depends on it |
| `switch` | `IfS` chain (as Python `match`) |
| type annotations, `as` casts, generics, interfaces, decorators | stripped / `Opaque` |
| JSX elements | `OpaqueE` (children expressions still lowered) |
| everything else | `OpaqueS`/`OpaqueE` — never crash |

Secret redaction (SPEC-1 §6.5 patterns) applies identically at TS lowering time.

*W0 exit (go/no-go):* wheels install in CI matrix; the backend parses the full DocGen clone
and a 500-file public TS repo with zero crashes (parse errors recorded, not raised); IR
shape tests for every row above; no downstream module imports `tree_sitter`. **If wheels or
grammar quality fail here, stop and re-plan — do not hand-roll a parser.**

---

## 3. Identity and the TS module graph (W1)

**Cross-language identity convention** (threads through registry, packs, wrapper registry,
merge — fixed here): `"<specifier>:<export>"`.
Examples: `openai:default`, `@openai/agents:Agent`, `@langchain/langgraph:StateGraph`,
internal `apps/api/src/llm.ts:createClient`. Python fq names are unchanged. Rule packs and
the sdk_registry may use either form; the engines treat both as opaque keys.

`modules/ts_graph.py` (parallel to `modules/graph.py`, both behind one lookup facade):

- Relative specifiers: `./x` → `x.ts`, `x.tsx`, `x.js`, `x/index.ts`, `x/index.js` (that
  resolution order); extensions in specifiers honoured.
- `tsconfig.json` `baseUrl`/`paths` aliases (string-prefix mapping only, no glob
  semantics beyond `*`); nearest tsconfig wins; `extends` followed one level.
- Workspaces: `pnpm-workspace.yaml` / package.json `workspaces` map internal package names
  (`@docgen/prompts`) to workspace directories — internal imports resolve to source, not
  `PackageRef`.
- **Barrel canonicalisation:** `export {Agent} from './agent'` chains resolve to the
  defining module (reuse the `canonicalize_fq` approach; F1 matches
  `{original} ∪ {canonical}` — the SPEC-1 §7.2 property, now in TS).
- External specifiers → `PackageRef(name, version)` with versions from `package.json` /
  `pnpm-lock.yaml` / `package-lock.json` / `yarn.lock` (first found wins; parse minimally,
  stdlib only).
- Per-module symbol tables: imports, top-level consts/functions/classes, exports (incl.
  default). Star re-exports → `Top(star_import)`, as Python.

*Exit:* module-graph unit suite incl. barrels, aliases, workspaces; DocGen's `apps/*` ↔
`packages/*` imports resolve.

---

## 4. Resolver additions (W2 — additions, not forks)

The resolver algorithm (env-walk, memo, bounds, `chain_root`) is untouched. Additions, all
data/pattern-level:

- `process.env.X` / `process.env["X"]` / `import.meta.env.X` → `Symbolic("env", X)`.
- `JSON.stringify(e)` resolves to `e` (payload unwrap for §5); `JSON.parse` → `Top(dynamic)`.
- JSON module imports (`import cfg from './config.json'`) → `DictVal` from the file
  (repo-local, static data — the Python config-file precedent).
- `new C(...)` where `C` is external → `ClassInstance("<specifier>:<export>", bound_args)`
  (the existing external-callable rule, TS identity).
- Identity-preserving chains: `client.beta`, `withOptions(...)`-style — extend the
  `_IDENTITY_METHODS` mechanism with a TS list (data).
- `await e` already unwrapped at lowering; `.then` chain values are `Top(dynamic)` (§0).

*Exit:* resolver unit cases per addition; `chain_root` resolves
`new OpenAI().chat.completions.create` and `fetch` callees.

---

## 5. Sink engine in TS (W3)

- **SDK sinks** — `sdk_registry.yaml` entries (data only):

```yaml
- root: "openai:default | openai:OpenAI | openai:AzureOpenAI"
  chains: [chat.completions.create, responses.create, embeddings.create]
  api_style: openai
  ctor_endpoint_kwargs: [baseURL, endpoint]          # new OpenAI({baseURL}) / AzureOpenAI({endpoint})
- root: "@anthropic-ai/sdk:default"
  chains: [messages.create, messages.stream]
  api_style: anthropic
- root: "@azure/openai:OpenAIClient"                  # + deployment handling per SPEC-1 §6.5
- root: "@aws-sdk/client-bedrock-runtime:BedrockRuntimeClient"   # .send(InvokeModelCommand)
- root: "@google/generative-ai:GoogleGenerativeAI"
- root: "ai:generateText | ai:streamText | ai:generateObject"    # module-level, model arg
```

- **HTTP-shape sinks** — callee roots `fetch`, `globalThis.fetch`, `axios.post|request`,
  `got.post`, `undici.request`, `node-fetch:default`. URL = first arg; payload = `body:`
  (through the `JSON.stringify` unwrap) or axios `data:`/second positional. **The scoring
  table is SPEC-1 §6.5's, unchanged** — same weights, same thresholds; response-access
  signals (`choices[0]`, `.content[0].text`, `finish_reason`, `stop_reason`) are identical
  strings in TS. Headers from `headers: {...}` DictVal.
- Attribution ladder, Azure deployment split, prompt binding at sinks (`system` payload
  key / first `role: "system"` message), credential-ref extraction: unchanged engines
  consuming TS-resolved values.
- Side-effect classification for TS tool functions: HTTP verbs to non-LLM hosts →
  read/external_send per the existing table; `child_process`/`eval` → `code_exec`
  (registry data).

*Exit:* `ts_bespoke_gateway_loop` and `ts_azure_deployment` yield correct sinks +
attribution at fact level (the acceptance-item-3 proof).

---

## 6. F1 framework packs — JS (W4)

`packs/openai_agents_js.yaml`: `@openai/agents:Agent` ctor (name/instructions/model/tools/
handoffs) → AgentDef + bindings · `@openai/agents:tool` → ToolDef · `run(agent, ...)` →
entrypoint. `packs/langgraph_js.yaml`: `@langchain/langgraph:StateGraph` `addNode`/
`addEdge`/`addConditionalEdges` → AgentCandidate + route Transfers (same promotion rule) ·
`createReactAgent` → AgentDef · `@langchain/openai:ChatOpenAI` etc. → ModelRef (capture
`configuration.baseURL`). `packs/vercel_ai.yaml`: `ai:generateText`/`streamText` with
`tools:` → AgentDef(kind=framework, framework=vercel-ai) when tools present else
LLMCallSite; `ai:tool({description, parameters, execute})` → ToolDef (side-effects from the
`execute` FuncRef body).

Rule schema, emit DSL, promotion post-pass: **unchanged** (rules are data; `callee_fqname`
uses §3 identities; the engine's `_fqset` canonicalisation covers barrels).

*Exit:* three JS framework fixtures golden; `ts_adversarial_agent_name` (a local class
named `Agent`, no AI imports) stays empty.

---

## 7. F2 bespoke in TS (W5)

Wrapper fixed point and agent-shape F1–F5 run on TS IR **as-is** — they are IR-level. Work
here is verification + gap-closing, not new algorithms:

- Wrapper registry entries key on §3 identities + content hash; one registry serves both
  languages (a TS wrapper in repo A is reused when repo B imports the same workspace
  package). `is_test_path`/`path_location` extended with TS conventions
  (`__tests__/`, `*.spec.ts`, `*.test.ts`, `.storybook/`, `stories/` → test;
  `examples|samples|demo` as today).
- Agent-shape verification targets (fixtures): async `while(true)` loop with
  `finish_reason` dispatch (the DocGen shape) · dict-of-callables tool dispatch
  (`TOOLS[call.function.name]`) · message-array accumulation via `.push` (extend the
  ListVal append tracking to `.push`) · recursion-based loops (`return agentStep(...)`).

*Exit:* TS bespoke fixture ladder green; `evidence-agent.ts`-shaped fixture recovers
F1/F2/F4/F5 with high confidence.

---

## 8. Declared-agent artefacts (W6a — language-neutral, tamed)

DocGen's `packages/prompts/agents/<name>/system.md` names its agent roster. Rule (additive
to `config_files.yaml`): a directory matching `**/prompts/agents/<name>/` containing
`system*.md|txt` emits an **AgentCandidate** named `<name>` with a PromptDef
(origin=file, hashed).

- **Promoted** to the corresponding AgentDef's prompt binding when a code-detected agent
  matches by name (slug-normalised) or when resolved code references the prompt path.
- **Never promoted to a standalone AgentDef** — a markdown tree is not detection. Unbound
  candidates surface as finding `declared_agent_artefact` (severity info; medium when the
  repo verdict is `ai_signals_only` — declared agents with no analysable code is exactly
  the DocGen-before-SPEC-4 story) and a `declared_agents` list in scan_health.

*Exit:* fixture with a prompt roster: bound case attaches prompts to agents; unbound case
yields the finding, never a fake agent.

---

## 9. Polyglot record, report, BOM (W6b)

- `AgentDefF`/`AgentRecord` gain `language: "python" | "typescript" | "javascript"`
  (additive; from the defining file's extension). Report shows a language chip per agent
  and per-language counts in the summary strip; graph nodes unaffected (ids already
  path-based, no collisions).
- Census/coverage: `ANALYSED_EXTS` from the parser registry drives the banner — TS/JS
  drop out of "not analysed" automatically; other languages keep the SPEC-3 behaviour.
- **AI-BOM:** npm rows' `used` becomes real — `used = imported in analysed TS/JS code`
  (module graph knows); the report's "outside Python-only analysis" cell is replaced by
  the normal yes/dormant logic for npm. `ecosystem` field stays.
- Enrichment/adjudication: slicer already reads raw text — works on `.ts` unchanged;
  verify the injection-inert test with a TS payload.
- Dataset: `agents` table gains a `language` column; queries untouched.

*Exit:* a mixed fixture (Python agent + TS agent in one repo) yields one record with both,
correctly tagged; goldens for the polyglot report.

---

## 10. Testing, security, performance

### Fixture ladder (each: repo + golden record/graph; report goldens where marked)

| Fixture | Must-hold |
|---|---|
| `ts_fw_openai_agents` | 2 agents + handoff via `@openai/agents` through a **barrel** re-export → F1 + canonicalisation proof |
| `ts_langgraph_nodes` | StateGraph addNode ×3, one node calls the model → exactly 1 promoted agent, route Transfers |
| `ts_vercel_ai_tools` | `generateText({tools})` + `tool()` defs → AgentDef + ToolDefs with side-effects |
| `ts_bespoke_gateway_loop` (report golden) | **headline**: `fetch` loop to internal gateway, `JSON.stringify` payload, `choices[0]` dispatch → bespoke AgentDef, model `internal-x1`, gateway endpoint |
| `ts_bespoke_axios_wrapper` | axios wrapped in a class SDK → wrapper fixed point classifies, agent recovered |
| `ts_azure_deployment` | AzureOpenAI deployment → `deployment` set, model = external alias |
| `ts_adversarial_agent_name` | local `class Agent`, zero AI imports → empty record |
| `ts_tsx_embedded` | agent loop inside a React component file → detected via calls; JSX opaque |
| `ts_destructured_dispatch` | `const {choices} = resp` + switch dispatch → F2 fires through destructuring |
| `polyglot_mixed` (report golden) | one Python + one TS agent → single record, language tags, no coverage banner |
| `ts_prompt_roster` | prompts/agents tree: bound + unbound cases per §8 |

### Gates

- All **existing Python goldens byte-unchanged** (regression gate: the new frontend must
  not perturb Python detection or record output — except the census no longer listing ts).
- Determinism double-run over TS fixtures (record, graph, report).
- No-exec canary in a TS fixture (`postinstall` script + top-level side-effect code —
  never executed, canary absent).
- Real-repo smokes (`AISCAN_SMOKE=1`): DocGen clone (acceptance §1.2 assertions) + one
  large public TS agent repo (e.g. the Vercel AI SDK examples or LangGraph.js examples)
  within budget.
- `mypy --strict`, `ruff`, injection corpus (now incl. a TS injection fixture).

### Security & performance

- tree-sitter parses text only; no execution paths added; secret redaction identical.
- Budgets: unchanged p50 ≤ 30 s / p95 ≤ 3 min now measured on 10k–100k LOC *combined*;
  tree-sitter parse throughput is far above stdlib-ast so parse is not the bottleneck;
  resolver bounds unchanged and shared across languages.

## 11. Build order (each phase green before the next; W0 is a go/no-go)

- **W0 — parser spike.** Deps pinned, backend + registry, full lowering table, census
  integration. *Exit: §2.1 exit; go/no-go decision recorded in DECISIONS.*
- **W1 — TS module graph.** Identities, resolution, barrels, workspaces, versions.
- **W2 — resolver additions.** §4 list + unit cases.
- **W3 — sinks.** Registry data + shape scoring via fetch/axios; gateway + azure fixtures.
- **W4 — F1 packs.** Three packs + adversarial fixture.
- **W5 — F2.** Wrapper + shape verification ladder; `.push` tracking; test-path conventions.
- **W6 — artefacts + polyglot.** §8 + §9; mixed fixture; BOM `used`; report/dataset chips.
- **W7 — hardening.** Full fixture table, determinism, no-exec TS canary, DocGen +
  public-repo smokes, README, DECISIONS sweep. *Exit: §1 definition of done, complete.*

## 12. Ambiguity protocol

As SPEC-1 §12: simpler deterministic option, one DECISIONS line, no features beyond this
spec, no TODO-stubbed detection — a phase ships working detectors or it isn't done.
