# SPEC.md — Agent Inventory Scanner, MVP Build Brief

**Audience: Claude Code.** This file is the authoritative spec for the MVP. Build exactly what
is in scope here, to the standards in §3, in the phase order in §11. A longer design document
exists for the full product (diffing, incremental analysis, governance workflow); where this
brief simplifies it, this brief wins for the MVP.

---

## 0. Context and mission

We are building the detection core of a bank-grade **AI/agent inventory scanner**. Given a
repository (local path or public git URL) at a commit, the scanner statically recovers every
LLM-using system in it — framework-built agents (OpenAI Agents SDK, LangChain/LangGraph,
CrewAI), **bespoke hand-rolled agents**, and plain LLM call sites — and binds each agent to its
model, system prompt, tools, MCP servers, memory and control policies. Output is (a) a typed
**Agent Dependency Graph (ADG)** and (b) a bank-inventory **record** with per-field evidence and
provenance.

The methodology follows the AgentFlow research paper (arXiv 2607.01640): framework frontends emit
normalised *agent facts*, an alias/def-use resolver binds components to the specific agents that
use them, and facts assemble into a typed graph from which the per-agent BOM is a query. We
extend it in two ways that are the commercial IP:

1. **Endpoint-agnostic detection.** An LLM call is recognised by the *shape* of the request
   (payload keys, path fragments, response access), not the URL host. A call to
   `https://gw.bank.internal/llm/v1/chat` with `{"model": "...", "messages": [...]}` must be
   detected, and the model name attributed, exactly as a call to `api.openai.com` would be.
2. **Bespoke agent recovery.** Hand-rolled agent loops over raw HTTP or in-house wrapper SDKs
   (e.g. `from bank_ai import LLMClient`) are detected structurally, with wrapper clients
   learned by a fixed-point algorithm rather than hardcoded signatures.

Detection quality is the product. Precision matters as much as recall: a bank auditor will read
these records, so **nothing is ever guessed** — unresolvable values are recorded as symbolic or
`unresolved` with a reason, never invented.

---

## 1. Scope

### In scope (MVP)

- Python codebases only.
- CLI: `aiscan scan <path|git-url> [--commit SHA] [--out DIR] [--org-pack FILE]` → clones
  (shallow, read-only), scans, writes artifacts. Must work end-to-end on real public repos.
- Full pipeline: ingest & triage → parse/IR → module graph → resolver → sink engine → frontend
  F1 (framework rules) + frontend F2 (bespoke) → fact merge → ADG → record emission.
- Deterministic core: byte-identical outputs for identical inputs.
- Test corpus of fixture mini-repos with golden expected outputs, plus a metrics harness.
- Optional, off-by-default LLM adjudication tier behind `--adjudicate` (Phase 7 only).

### Out of scope (do not build, do not stub)

Diff/materiality/incremental analysis; runtime observation; enrichment (LLM-drafted
descriptions); databases (filesystem JSON artifacts only); web service/webhooks; containers/CI
plumbing; CycloneDX export; JS/TS/other languages; any UI. The record schema keeps slots for
deferred features (they emit as `null` with a `source` marker) but no machinery behind them.

### Deliberate MVP simplifications vs the full design (do these, don't "upgrade" them)

- **Parser backend is Python stdlib `ast`**, wrapped behind a `Parser` interface that lowers to
  our own IR. tree-sitter is a v1 swap for multi-language and incrementality; the IR is the
  abstraction boundary, so nothing downstream may import `ast` directly.
- **No resolution-trace recording** (that exists for incremental invalidation, which is out of
  scope). Facts still carry `source_files` and evidence spans.
- **Persistence is filesystem JSON** (artifacts per scan; org wrapper registry as a JSON file).
- **Graph is our own small typed structure**, not networkx/rustworkx. Queries are neighbour
  lookups; canonical serialisation is ours to control.

---

## 2. Deliverable and acceptance criteria

`aiscan scan` on a target produces in `--out` (default `./aiscan-out/<repo>-<commit>/`):

```
record.json        # the inventory record (schema §5.4, example Appendix A)
graph.json         # canonical ADG serialisation (§6.8)
facts.jsonl        # every emitted fact with evidence + method (audit trail)
scan_health.json   # timings, parse errors, resolver Top/timeout counts, triage signals
scan.log           # structured log
```

**Definition of done (all required):**

1. All fixture goldens (§8) pass: emitted `record.json` and `graph.json` match expected files
   exactly (modulo the `scanned_at`/duration fields).
2. **Gateway attribution proof:** the `bespoke_gateway_loop` fixture yields an
   `AgentDef(kind=bespoke)` with `model = "internal-x1"` and
   `endpoint = "https://gw.internal.example/llm/v1/chat"` — no known host involved.
3. **Wrapper proof:** the `bespoke_wrapper` fixture classifies `bank_ai.LLMClient` as a wrapper
   via the fixed point (no signature for it exists anywhere) and recovers the agent behind it.
4. **Determinism test:** two consecutive scans of the same fixture produce byte-identical
   `graph.json` and identical `record.json` apart from timestamps.
5. **No-execution test:** the adversarial fixture containing side-effectful top-level code
   (writes a canary file) scans cleanly and the canary is never created.
6. Smoke: scans of 3 real public repos (pick current, agent-containing repos, e.g. the
   `openai/openai-agents-python` examples tree and one LangGraph and one CrewAI example repo)
   complete without crashing, within performance budget (§9), producing non-empty sensible
   records; parse failures, if any, appear in `scan_health`, not as crashes.
7. `mypy --strict` and `ruff check` clean; `pytest` green; README quickstart is accurate.

---

## 3. Engineering standards (non-negotiable)

This is the foundation of an industry tool, not a prototype. Concretely:

- Python 3.12. Full type annotations everywhere; `mypy --strict` must pass. `ruff` for lint +
  format. `pytest` for tests.
- **Pydantic v2** models for all facts, values, records, and config (frozen where practical).
  Model validators enforce invariants (e.g. every fact has ≥1 evidence span).
- **Module boundaries per §4 layout.** Dependencies point downward only (frontends → sinks →
  resolver → ir; nothing imports upward). No god modules, no circular imports.
- **Determinism:** no reliance on set/dict iteration order — sort at every emission point;
  canonical JSON = `sort_keys=True`, entities sorted by stable id, no floats where ints do, no
  timestamps inside `graph.json`/`facts.jsonl` (timestamps live in `record.json` provenance and
  `scan_health.json` only). No global mutable state; explicit `ScanContext` object threaded
  through.
- **Never crash on weird code.** Per-file parse errors are recorded in `scan_health.parse_errors`
  and the scan continues. The resolver returns `Top(reason)`, it never raises through the
  pipeline. Unknown AST nodes lower to `Opaque` IR nodes.
- **Never execute scanned code.** No `import` of scanned modules, no `eval`/`exec`, no
  `subprocess` on repo content, no installing the repo's dependencies. The only subprocess use
  in the whole tool is `git` for cloning. The only network use is `git clone` (and the
  adjudication endpoint when `--adjudicate` is explicitly on).
- **Evidence or it didn't happen:** every fact and every populated record field carries
  `evidence: [file:line_start-line_end]`, `method` (rule id / detector id), and `confidence`.
- Logging via `logging` (structured, `--json-logs` option). No `print` outside the CLI layer.
- Dependencies are exactly: `pydantic`, `pyyaml`, `typer`, `pytest`, dev: `mypy`, `ruff`.
  (`anthropic` added in Phase 7 only, as an optional extra.) Adding any other dependency
  requires a written justification in `DECISIONS.md`.
- Docstrings on all public functions/classes. A `README.md` with install + quickstart + output
  explanation.

---

## 4. Repository layout and dependencies

```
aiscan/
  __init__.py
  cli.py                 # typer app; thin — orchestration only
  context.py             # ScanContext, Settings (pydantic-settings from YAML/env)
  ingest/
    git.py               # clone/checkout via subprocess git; local-path handling
    triage.py            # AI-signal gate (§6.1)
  ir/
    nodes.py             # IR dataclasses (§6.2)
    values.py            # value domain (§5.1)
  parse/
    base.py              # Parser interface
    py_ast.py            # stdlib-ast backend → IR lowering
  modules/
    graph.py             # module graph, import resolution (§6.3)
    symbols.py           # per-module symbol tables
  resolve/
    engine.py            # demand-driven resolver (§6.4)
    memo.py
  sinks/
    engine.py            # sink classification orchestrator (§6.5)
    sdk_registry.yaml    # SDK sink signatures (data)
    hosts.yaml           # provider host registry (data)
    shape.py             # HTTP-shape scorer
    attribution.py       # model/endpoint attribution ladder
    side_effects.py      # tool side-effect classifier
  facts/
    models.py            # fact vocabulary (§5.2)
  frontends/
    framework/
      engine.py          # YAML rule interpreter (§6.6)
      packs/
        openai_agents.yaml
        langgraph.yaml
        crewai.yaml
        config_files.yaml
    bespoke/
      wrappers.py        # fixed-point wrapper classifier (§6.7.1)
      agent_shape.py     # F1–F5 feature detectors + tiering (§6.7.2)
      call_sites.py      # LLMCallSite emission
  graph/
    model.py             # typed ADG (§5.3)
    build.py             # facts → graph, dedup/merge (§6.8)
    queries.py           # agent_bom, bundle_bom
    canonical.py         # canonical JSON serialisation
  inventory/
    schema.py            # record models (D/E/G/X slots) (§5.4)
    emit.py              # bundle_bom → record.json
  adjudicate/            # Phase 7 only, optional extra
    __init__.py
tests/
  fixtures/<name>/repo/          # each fixture is a tiny scannable repo
  fixtures/<name>/expected/      # golden record.json + graph.json
  harness.py                     # runs scanner over fixtures, compares, computes metrics
  test_*.py
DECISIONS.md
README.md
pyproject.toml
```

---

## 5. Data models

### 5.1 Value domain (`ir/values.py`)

The resolver returns **frozen sets of abstract values**. Everything downstream consumes these.

```python
Value =
  Str(s: str)
| Template(parts: list[str | HoleRef], dynamic=True)   # f-strings / partly-literal concat
| Num(v) | Bool(v) | NoneV
| Symbolic(kind: Literal["env","config","cli","wrapper_default","external"], key: str)
| ClassInstance(class_fq: str, ctor_args: BoundArgs)
| FuncRef(fq: str, def_site: Span) | ClassRef(fq: str, def_site: Span) | ModuleRef(name: str)
| ListVal(elems: list[frozenset[Value]], open: bool)   # open=True if appends not fully tracked
| DictVal(entries: dict[str, frozenset[Value]], open: bool)
| PackageRef(name: str, version: str | None)           # third-party import target
| Top(reason: Literal["depth","unbound","dynamic","timeout","star_import","opaque"])
```

Rules: `Symbolic` and `Template` are first-class results — record the symbol/template, never a
guess. `Top` never silently enters a record; it surfaces as `unresolved(reason)`.

### 5.2 Fact vocabulary (`facts/models.py`)

All frontends emit these. Common base: `{id, evidence: list[Span], confidence: "high"|"medium"|
"low", method: str, source_files: list[str]}`.

Entities:

```
AgentDef    {name, kind: "framework"|"bespoke", framework: str|None}
PromptDef   {content: ValueRepr, dynamic: bool,
             origin: "literal"|"file"|"config"|"constructed", file_ref: str|None}
ModelRef    {provider: str|None, model: ValueRepr, endpoint: ValueRepr|None,
             deployment: ValueRepr|None, api_style: "openai"|"anthropic"|"bedrock"|"vertex"|"unknown",
             task: "chat"|"completion"|"embedding"}
ToolDef     {name, kind: "function"|"mcp_tool"|"schema_declared"|"retriever"|"agent_as_tool",
             signature: {params: [...], returns: str|None}|None,
             side_effects: list["read"|"write"|"external_send"|"code_exec"|"admin_mutation"],
             external_target: ValueRepr|None, credential_ref: ValueRepr|None}
MCPDef      {server: ValueRepr, transport: "stdio"|"sse"|"http"|"ws",
             declared_tools: list[str]|"dynamic", approval_policy: ValueRepr|None}
StateDef    {kind: "messages"|"session"|"vectorstore"|"shared_object", backing: ValueRepr|None}
PolicyDef   {kind: "approval"|"guardrail"|"permission_check", params: ValueRepr}
LLMCallSite {model: ModelRef, prompt: PromptDef|None}      # model use with no agent shape
AgentCandidate {name, fn_ref: str}                          # F1 internal, promoted or dropped
```

Bindings: `BindModel{agent, model}` · `BindPrompt{agent, prompt}` · `BindTool{agent, tool}` ·
`BindMCP{agent, mcp}` · `BindState{agent, state}` · `AttachPolicy{policy, target}`.
Control: `Call{agent, tool, guard?}` · `Transfer{from, to, kind: "handoff"|"invoke"|"route",
guard?}`. Data (MVP-minimal): `InterAgentMsg{from, to, via?}`.

`ValueRepr` is the JSON-serialisable projection of a resolved value set (literal string, or
`{"symbolic": "env:MODEL_NAME"}`, or `{"template": "...{hole}...", "dynamic": true}`, or
`{"unresolved": reason}`).

### 5.3 ADG (`graph/model.py`)

Typed property graph. Node types `A, I, M, C, S, G, L` (L = LLMCallSite). Edge families:
`ACDG` (bindings, undirected), `ACFG` (control, directed). Implement as:

```python
class Graph:
    nodes: dict[NodeId, Fact]
    edges: list[Edge]                    # Edge{family, type, src, dst, attrs}
    def neighbors(self, node, family=None, type=None) -> list[NodeId]: ...
```

`agent_bom(a)` = ACDG neighbours of `a` grouped by node type. `bundle_bom` = every agent's BOM +
orphan `L` nodes + unbound MCP/prompt artefacts.

### 5.4 Record (`inventory/schema.py`)

Field sourcing classes: **[D]** detected (scanner-owned), **[E]** enriched draft (deferred —
emit `{"value": null, "source": "enriched"}`), **[G]** governance-owned (never written — emit
`{"value": null, "source": "governance"}`), **[X]** external linkage (emit ref if detected,
resolution null). Structure: bundle header (id, name, repo_url, scanned_commit, description[E],
owner[G] with `candidate` from CODEOWNERS/git if cheaply available, risk/purpose/etc [G] slots),
`agents[]` (detection provenance, model, system_prompt, tools, mcp, state, policies, handoffs,
description[E]), `tools[]` (kind, signature, side_effects, external_target, credential_ref,
declared_authorisation[D] as "<method+target>" when derivable, effective_entitlement[X] ref),
`mcp_servers[]`, `model_usages[]` (from LLMCallSites), `findings[]` (secret-literal findings,
suspected-but-unconfirmed sinks), `scan_health` summary, `inventory_provenance`. Appendix A is
the golden shape — implement to match it.

---

## 6. Pipeline stages

### 6.1 Ingest and triage (`ingest/`)

Git: `git clone --depth 1 <url> <tmp>`; if `--commit` given, `git fetch --depth 1 origin <sha>
&& git checkout <sha>` (best-effort; record actual HEAD sha either way). Local paths used as-is
(record `git rev-parse HEAD` if a repo). Read-only always.

Triage gate (recall-biased, regex/manifest level — its only job is to skip repos with no AI
signal at all): scan manifests (`requirements*.txt`, `pyproject.toml`, `poetry.lock`, `Pipfile`,
`setup.py/cfg`) for known AI packages (`openai, anthropic, langchain*, langgraph, crewai,
llama-index, litellm, google-genai, google-generativeai, mistralai, cohere, groq, together,
boto3, semantic-kernel, autogen, mcp`); grep all files for SDK import lines, provider hosts
(§6.5 hosts.yaml + org-pack hosts), chat-path fragments (`/chat/completions`, `/v1/messages`,
`:generateContent`, `/converse`), `"model"` within 300 chars of `"messages"|"prompt"|"input"`
in a file that also references an HTTP client, prompt/agent artefacts (`prompts/` dir,
`*.prompt`, `system*.md`, `SKILL.md`, `.mcp.json`, `mcp.json`), and imports of org-pack
`known_wrapper_packages`. Zero signals → emit `no_ai_detected` record stub with the signals
table in `scan_health` and stop. Any signal → full pipeline. Note `boto3` alone is a weak
signal; it passes triage but only becomes a sink via §6.5.

### 6.2 Parser and IR (`parse/`, `ir/nodes.py`)

`Parser` interface: `parse(path, source) -> ModuleIR | ParseError`. MVP backend: stdlib `ast`.
Lower to IR:

- `ModuleIR{path, imports, defs, classes, assigns, body}`;
  `ImportIR{kind: "import"|"from", module, names: [(name, asname)], level, star: bool}`;
  `ClassIR{name, bases: [Expr], decorators, body_assigns, methods: [FuncIR], span}`;
  `FuncIR{name, params: [Param{name, default?, kind}], decorators, body: [Stmt], is_async, span}`.
- Statements: `AssignS{targets, value}`, `AugAssignS`, `ExprS{expr}`, `ReturnS{value?}`,
  `IfS{test, body, orelse}`, `WhileS{test, body}`, `ForS{target, iter, body}`,
  `WithS{items, body}`, `TryS{body, handlers, final}`, `MatchS` (lower to `IfS` chain
  semantics), `OpaqueS{span}` for anything else.
- Expressions: `NameE`, `AttrE{base, attr}`, `CallE{callee, args, kwargs}`,
  `SubscriptE{base, index}`, `StrE`, `FStrE{parts}`, `NumE/BoolE/NoneE`, `ListE`, `TupleE`,
  `DictE{entries}`, `BinOpE{op,l,r}`, `CompareE`, `LambdaE` (→ dynamic), `AwaitE` (unwrap),
  `OpaqueE{span}`.
- Every node carries `Span{file, line_start, line_end}`.

CI cross-check: a test asserts IR node counts/shapes on fixtures so a future tree-sitter backend
can be verified equivalent.

### 6.3 Module graph and symbols (`modules/`)

Map repo-internal imports to files: absolute and relative imports, `as` aliases, packages via
`__init__.py`, namespace packages. `from x import *` → record edge, lookups through it return
`Top(star_import)`. Imports not resolvable inside the repo → `PackageRef(name, version)` with
version from lockfiles when present. Per-module symbol table: imports, top-level defs/classes,
top-level assignments. `sys.path`-style source roots: repo root plus any `src/` layout
(detect `src/<pkg>/__init__.py`).

### 6.4 Resolver (`resolve/engine.py`)

Demand-driven: frontends and the sink engine issue `resolve(expr, ctx)` queries; there is no
whole-program pass.

```
def resolve(expr, ctx, depth=0) -> frozenset[Value]:
    if depth > 8: return {Top("depth")}
    key = (ctx.module.content_hash, expr.span);  memo hit → return
    match expr:
      StrE(s)        -> {Str(s)}
      FStrE(parts)   -> {Template(parts, holes resolved best-effort)}
      NameE(n)       -> local scope → enclosing fn scopes → module globals (union of reaching
                        assignments*) → imports (follow chain across modules, ≤4 hops) →
                        builtins → Top("unbound")
      AttrE(b, a)    -> for v in resolve(b):
                          ModuleRef      → member lookup in that module's symbol table
                          ClassRef c     → class-body assign or method a → FuncRef/value
                          ClassInstance  → class attr; or `self.a = expr` in __init__
                                           (resolve expr with ctor_args bound to __init__ params);
                          PackageRef     → keep as attribute chain string for §6.5 matching
                          else Top("dynamic")
      CallE(f, args) -> for c in resolve(f):
                          ClassRef k  → {ClassInstance(k, bind(args))}
                          FuncRef fn  → if fn body has ≤2 ReturnS with resolvable exprs:
                                        union(resolve(ret, scope=fn args bound, depth+1))
                                        else Top("dynamic")
      ListE/TupleE   -> ListVal(elem sets; open=True if any tracked `.append/.extend` on the
                        same name in this scope was not fully resolved)
      DictE          -> DictVal(literal-keyed entries; open for ** / computed keys)
                        + fold subsequent `d[k]=v` assigns to the same name in the same scope
      SubscriptE     -> literal-key lookup into DictVal/ListVal else Top
      BinOpE(+ , strs)-> fold literals else Template
      os.environ[...] / os.environ.get(K)          -> {Symbolic("env", K)}
      json/yaml/toml load + literal key path        -> {Symbolic("config", "<file>:<key.path>")}
      argparse/click option value                   -> {Symbolic("cli", name)}
    record memo; return
```

*Flow handling: walk a function body in order maintaining an environment; straight-line
reassignment = last wins; at `IfS`/`MatchS` evaluate branches from the pre-state and union
post-states; loop bodies evaluated once (over-approximation is fine and matches the ADG
semantics). No SSA.

Bounds (enforced, surfaced in `scan_health`): depth ≤ 8, cross-module hops ≤ 4, 250 ms per
query, 60 s resolver budget per scan; on breach return `Top("timeout")`.

**Method-chain support for sinks:** the sink engine needs to match e.g.
`client.chat.completions.create` where `client` resolves to `ClassInstance("openai.OpenAI")`.
Implement `chain_root(expr) -> (base_values, attr_path: tuple[str, ...])` in the resolver:
peel `AttrE` layers to the base, resolve the base, return both. The registry matches
`(class_fq | module_fq, attr_path)`.

### 6.5 Sink engine (`sinks/`)

A **sink** is a call site classified as invoking an LLM. Checked in order; first hit
classifies, later hits recorded as corroboration.

**(a) SDK sinks** — `chain_root` base resolves to a registered class/module and the attr path
matches. `sdk_registry.yaml` entries (root → chains → api_style, arg mapping):

```yaml
- root: openai.OpenAI | openai.AsyncOpenAI | openai.AzureOpenAI
  chains: [chat.completions.create, responses.create, completions.create, embeddings.create]
  api_style: openai
  args: {model: model, messages: messages, input: input, system: null}
  ctor_endpoint_kwargs: [base_url, azure_endpoint]        # OpenAI(base_url=...) → endpoint
- root: anthropic.Anthropic | anthropic.AsyncAnthropic
  chains: [messages.create, messages.stream]
  api_style: anthropic
  args: {model: model, messages: messages, system: system}
- root: litellm            # module-level
  chains: [completion, acompletion]
  api_style: openai
- root: boto3.bedrock-runtime-client     # special: boto3.client("bedrock-runtime") →
  chains: [invoke_model, converse]       # ClassInstance("boto3.bedrock-runtime-client")
  api_style: bedrock
  args: {model: modelId}
- root: google.genai.Client
  chains: [models.generate_content]
  api_style: vertex
  args: {model: model}
- root: google.generativeai.GenerativeModel     # model in ctor arg 0
  chains: [generate_content]
  api_style: vertex
- root: mistralai.Mistral      | chains: [chat.complete]              | api_style: openai
- root: groq.Groq              | chains: [chat.completions.create]    | api_style: openai
- root: cohere.ClientV2        | chains: [chat]                        | api_style: openai
- root: together.Together      | chains: [chat.completions.create]    | api_style: openai
```

`embeddings.create` → sink with `task: embedding`; embedding sinks are inventoried
(`LLMCallSite`) but excluded from agent-shape anchoring.

**(b) HTTP-shape sinks — the endpoint-agnostic core.** Callee chain roots at
`requests.(post|request)`, `httpx.(post|Client().post|AsyncClient().post|request|stream)`,
`aiohttp.ClientSession().post`, `urllib.request.urlopen`. Resolve URL (first positional or
`url=`) and payload (`json=` | `data=` | `content=`); payload resolves to `DictVal` (folded per
§6.4, plus one call-return hop into a builder function). Score over known keys; `open=True`
lowers **confidence** one step, never the score:

| Signal | Weight |
|---|---|
| URL path contains `/chat/completions`\|`/completions`\|`/v1/messages`\|`:generateContent`\|`/converse`\|`/generate`\|`/invoke` | +3 |
| payload has `model` AND `messages` | +3 |
| payload has `model` AND (`prompt`\|`input`\|`contents`) | +2 |
| result variable's uses include `choices[0]`(`.message`)\|`content[0].text`\|`candidates[0]`\|`.stop_reason`\|`.finish_reason` (def-use within the function) | +2 |
| auth header `Authorization: Bearer`\|`x-api-key`\|`api-key`\|`anthropic-version` | +1 |
| payload has `temperature`\|`max_tokens`\|`max_completion_tokens`\|`top_p` | +1 (cap +2) |
| `stream: true` or SSE handling | +1 |

**Score ≥5 → sink** (confidence high if ≥7 else medium). **3–4 → `suspected_llm_call` finding**
(adjudication queue when Phase 7 enabled). **≤2 → not a sink.** `api_style` inferred from which
key/response signals fired.

**(c) Known-host sinks** — resolved URL host ∈ `hosts.yaml` (`api.openai.com`,
`api.anthropic.com`, `*.openai.azure.com`, `bedrock-runtime.*.amazonaws.com`,
`generativelanguage.googleapis.com`, `*-aiplatform.googleapis.com`, `api.mistral.ai`,
`api.groq.com`, `api.together.xyz`, `openrouter.ai`, `api.cohere.com`) **∪ org-pack
`gateway_hosts`**. Catches payloads too dynamic to shape-score. Template URLs match on their
literal prefix parts.

**(d) Wrapper sinks** — callee resolves to a definition classified by §6.7.1.

**Model/endpoint attribution ladder** (`sinks/attribution.py`) — for every sink, first rung
yielding non-Top wins; the rung is recorded in provenance:

1. literal at call site (`model="gpt-4o"` kwarg / payload key)
2. constant (resolves through assignments: `MODEL = "bank-gpt4-prod"`)
3. config/env symbolic (`Symbolic(env,"LLM_MODEL")` / `Symbolic(config,"settings.yaml:llm.model")`)
4. wrapper default (`Symbolic(wrapper_default, wrapper_fq)`)
5. `unresolved(reason)`

**Azure/deployment split:** URL path `/openai/deployments/{name}/...` or `deployment_name=` →
`ModelRef.deployment = name` and `model = Symbolic("external", "azure:deployment:<name>")` —
the foundation model behind a deployment or gateway alias is not in the code; it is an [X]
linkage, never a guess.

**Prompt binding at sinks:** `system=` param / `system` payload key / first message with
`role=="system"` / `system_instruction` → `PromptDef` with origin detection: value traces to
`open(...).read()` / `Path.read_text()` / `importlib.resources` / jinja `get_template` → origin
`file` + `file_ref` + hash file content; `Template` → `dynamic=true`.

**Side-effect classification** (`sinks/side_effects.py`) for any ToolDef with a resolvable body
(multi-label): HTTP GET / DB select / file read → `read`; DB insert/update, file write → `write`;
HTTP POST/PUT/DELETE to non-LLM host, `smtplib`, SES/SQS/SNS, kafka, slack SDK, org-pack
`sensitive_hosts` → `external_send`; `subprocess`, `exec`/`eval`, container/K8s SDK →
`code_exec`; IAM/user-management SDK calls → `admin_mutation`. Also extract
`external_target` (resolved host/service) and `credential_ref` (resolved auth source — the
*reference*: `Symbolic(env,...)`, vault path literal; never a value).

**Secret redaction:** at IR lowering, string literals matching key shapes (`sk-[A-Za-z0-9]{20,}`,
AWS `AKIA...`, PEM headers, `ghp_...`) are replaced with `"<REDACTED:kind>"` before any cache,
artifact, or (Phase 7) LLM slice — and emitted as `findings[]` entries with span.

### 6.6 Frontend F1 — framework rules (`frontends/framework/`)

Generic YAML rule interpreter. **Matching is on resolved identity, never surface text**: a rule
for `agents.Agent` fires only when the callee resolves to that fq name — a user's own class
named `Agent` must not match (this is an adversarial fixture).

Rule schema:

```yaml
- id: openai_agents.agent_ctor
  match: {kind: call, callee_fqname: agents.Agent}
  extract:
    name:         {arg: name,         resolve: string, required: true}
    model:        {arg: model,        resolve: model_ref}
    instructions: {arg: instructions, resolve: prompt}
    tools:        {arg: tools,        resolve: ref_list}
    handoffs:     {arg: handoffs,     resolve: ref_list}
    mcp_servers:  {arg: mcp_servers,  resolve: ref_list}
  emit:
    - AgentDef{name: $name, kind: framework, framework: openai-agents}
    - BindModel{model: $model}
    - BindPrompt{prompt: $instructions}
    - foreach $tools:       BindTool{tool: $item}
    - foreach $handoffs:    Transfer{to: $item, kind: handoff}
    - foreach $mcp_servers: BindMCP{mcp: $item}
```

`match.kind ∈ {call, decorator, class_base, config_file}` (config_file: `glob` + parser
json/yaml/md-frontmatter). Extract directives: `string`, `model_ref` (runs the §6.5 ladder),
`prompt` (origin detection as above), `ref`/`ref_list` (targets become entity facts:
FuncRef → ToolDef with signature from the def + side-effects; ClassInstance of a known MCP
class → MCPDef; ClassInstance of a known agent class → agent_as_tool), `policy`, `value`.
Emit interpreter supports fact type + field map + `foreach $list:`. Keep the DSL exactly this
small.

**Pack contents (constructs → facts):**

`openai_agents.yaml`: `agents.Agent(...)` (above) · `@agents.function_tool` → ToolDef(function,
signature, side-effects) · `agents.HostedMCPTool` / `MCPServerStdio` / `MCPServerSse` /
`MCPServerStreamableHttp` → MCPDef (+ `AttachPolicy(approval)` from `require_approval`) ·
`agents.ModelSettings(...)` merged into the bound ModelRef · `agents.Runner.run(agent, ...)` →
entrypoint marker attr on that agent.

`langgraph.yaml`: chat-model ctors `langchain_openai.ChatOpenAI` / `langchain_anthropic.
ChatAnthropic` / `langchain.chat_models.init_chat_model` → ModelRef — **capture `base_url` /
`azure_endpoint` / `api_base` kwargs into `endpoint`** (the internal-gateway case appears inside
frameworks too) · `@langchain_core.tools.tool` / `StructuredTool.from_function` / `Tool(...)` →
ToolDef · `<model>.bind_tools(tools)` → association attr on that ModelRef value ·
`StateGraph.add_node(name, fn)` → **AgentCandidate** · `create_react_agent(model, tools, ...)`
→ AgentDef directly with bindings · `add_edge(a,b)` / `add_conditional_edges(a, cond, ...)` →
`Transfer{kind: route, guard: cond FuncRef}` · `MemorySaver` / checkpointer / `InMemoryStore` →
StateDef · `langchain_mcp_adapters.*` → MCPDef.
**Candidate promotion post-pass:** AgentCandidate → AgentDef(kind=framework) iff its `fn_ref`
body contains a sink (§6.5) or resolves to a prebuilt agent; then BindModel from the model value
the fn invokes, BindTool from that model's `bind_tools` association. Otherwise the candidate is
dropped (plain graph nodes are not agents).

`crewai.yaml`: `crewai.Agent(role, goal, backstory, llm, tools)` → AgentDef(name=role) +
PromptDef(origin=constructed from goal+backstory) + BindModel + BindTool ·
`crewai.Task(agent=..., tools=...)` → additional BindTool onto that agent ·
`crewai.Crew(agents, tasks, process)` → Transfers: `sequential` → chain in task order;
`hierarchical` → manager→worker star · `crewai.tools.BaseTool` subclass / `@tool` → ToolDef ·
`memory=True` → StateDef.

`config_files.yaml`: `.mcp.json` / `mcp.json` → MCPDef per server `{command|url → transport}`,
`declared_tools` if listed else `"dynamic"` · `prompts/**`, `*.prompt`, `system*.md` → PromptDef
candidates, bound when any resolved string equals/endswith the path · `SKILL.md` frontmatter →
ToolDef(schema_declared).

### 6.7 Frontend F2 — bespoke (`frontends/bespoke/`)

#### 6.7.1 Wrapper fixed point (`wrappers.py`)

Makes `from bank_ai import LLMClient` a first-class sink with no pre-written signature.

```
classified: dict[def_fq -> WrapperInfo] = load(org_registry.json) + org_pack.known_wrapper_packages
worklist = { enclosing_def(s) for s in all sinks so far }
while worklist:
    d = worklist.pop()
    if d in classified: continue
    if wrapper_score(d) >= 3:
        classified[d] = WrapperInfo(attribution =
            "fixed"        if model/endpoint constant inside d
          | "passthrough"  if a model param is forwarded to the inner sink   # resolve per call site
          | "default")     # ctor/config-held → Symbolic(wrapper_default, d)
        for cs in call sites resolving to d (or to methods of d if d is a class):
            classify cs as wrapper sink; derive ModelRef per attribution mode
            worklist.add(enclosing_def(cs))          # wrappers of wrappers

wrapper_score(d):
  +2  body (any method body, for classes) contains a currently-classified sink call
  +1  params ∩ {prompt, messages, model, system, input, text, query, temperature} ≠ ∅
      or **kwargs forwarded into the sink call
  +1  returns the sink result (directly or after field extraction, e.g. .choices[0]...)
  +1  (classes) method names ∩ {chat, complete, completion, generate, invoke, ask, query, call}
  -2  sink appears only under `if __name__ == "__main__":`, in tests/**, or docstrings
```

Monotone and finite → terminates. Persist classifications to
`<out>/../org_registry.json` (configurable path) keyed `(fq, content_hash)` so later scans and
other repos reuse them; entries carry evidence and are auditable. Wrapper defined in an
installed package whose source is absent → mark `external_opaque` (adjudication in Phase 7;
until then, a finding).

#### 6.7.2 Agent-shape reconstruction (`agent_shape.py`)

Anchor = smallest enclosing callable region R containing ≥1 non-embedding sink. Feature
detectors over R's IR:

```
F1 iteration    : sink inside WhileS/ForS body, or R recurses (a call in R resolves to R)
F2 dispatch     : after the sink, an IfS/MatchS test referencing sink-result fields
                  ({tool_calls, function_call, finish_reason, stop_reason, name, content} via
                  def-use on the variable bound to the sink result) guarding a CallE whose
                  callee resolves via direct FuncRef, dict-of-callables SubscriptE, or getattr;
                  OR an unguarded CallE whose callee is a SubscriptE into a DictVal of FuncRefs
                  keyed from sink-result content
F3 feedback     : the dispatched call's result name flows into a later sink payload
                  (appears in messages.append(...) / payload DictVal)
F4 message state: a ListVal-tracked name accumulating DictE entries containing key "role";
                  or the sink payload contains "tools"/"functions" schemas
F5 termination  : WhileS test or a break-guard references sink-result fields or a loop counter
```

| Evidence | Result |
|---|---|
| F1 ∧ F2 ∧ (F3 ∨ F4) | `AgentDef(kind=bespoke)`, confidence high |
| F2 ∧ F3 (single-step tool use) — or — F1 ∧ F4 (loop with tools in payload) | `AgentDef(kind=bespoke)`, confidence medium |
| ≥2 features, unclear | finding `ambiguous_agent_shape` (adjudication in Phase 7) |
| sink, <2 features | `LLMCallSite` |

Bespoke bindings: tools = F2 dispatch targets (ToolDef(function) with signature + side-effects)
∪ payload `tools`/`functions` schema entries (ToolDef(schema_declared); matched to a dispatch
target of the same name when present → single merged ToolDef); prompt per §6.5; model/endpoint
per the ladder; state = the F4 accumulator (StateDef(messages)) or session/vector objects in R;
`PolicyDef(approval, confidence=medium)` when an `input()`/confirmation gate dominates a
dispatch branch. Agent name: enclosing function/class name (`run_agent` → `run_agent`).

Multi-agent: `Transfer{kind: invoke}` when R₁'s sink-result flows into a call resolving to
anchor R₂; `InterAgentMsg{via: state}` when two anchors read/write the same resolved state
object. Framework↔bespoke edges compose naturally since both are facts.

#### 6.7.3 `call_sites.py`

Sinks belonging to no AgentDef → `LLMCallSite` facts → `model_usages[]` in the record. Banks
need "this repo calls gpt-4o in a batch job" even with no agent.

### 6.8 Merge and ADG assembly (`graph/`)

Entity identity: AgentDef `(kind, name)` + anchor span tiebreak for duplicates; ToolDef =
function fq / `(server, tool)` / schema name; ModelRef = `(api_style, model_repr,
endpoint_repr)`; PromptDef = content hash (template-shape hash when dynamic). F1 and F2 finding
the same agent (e.g. a LangGraph node that is also a detected loop) → one node,
`union(evidence)`, `max(confidence)`, `method` lists both. Build the typed graph; **canonical
serialisation**: nodes and edges sorted by stable id, `sort_keys=True`, no timestamps.

### 6.9 Record emission (`inventory/emit.py`)

`bundle_bom` → record per §5.4 and Appendix A. Every [D] field carries
`{value, source: "detected", method, confidence, evidence}`. [E]/[G] slots null with source
markers. `declared_authorisation` on a tool = `"<HTTP method or verb> <external_target>"` when
derivable from side-effect analysis, else null. `owner.candidate` from `CODEOWNERS` /
`git log --format=%ae -20 | most common domain-local part` (best-effort, cheap, clearly marked
`candidate`). `scan_health` includes: file/LOC counts, parse_errors, triage signals, resolver
stats (queries, memo hits, Top counts by reason, timeouts), sink counts by kind, wrapper
classifications, unpromoted candidates, findings count, wall-clock per stage.

### 6.10 Adjudication (Phase 7, optional extra, `--adjudicate`)

Off by default; scanner must be fully useful without it. Admits only: `suspected_llm_call`
(shape 3–4), `ambiguous_agent_shape`, `external_opaque` wrappers, `unresolved` model on an
otherwise-confirmed agent. Budget ≤20 calls/scan, priority to candidates near
`external_send`/`admin_mutation`. Context = slice (anchor region + resolved dispatch-target
signatures + sink call + ≤40 surrounding lines + module imports; ≤6k tokens). Anthropic API,
temperature 0, structured JSON `{is_agent, confidence, agents:[{name, model_expr, prompt_ref,
tools}], wrapper:{is_llm_wrapper, attribution}|null, abstain, rationale}`. Accept only
confidence ≥0.7 else UNRESOLVED — **never guess**. Cache by slice hash in the artifact dir.
Facts created carry `method: llm_adjudicated`, confidence capped `medium`, may only add where
deterministic layers abstained, never override, never touch [G]/[X]. Adjudicator output is
schema-validated; scanned code (incl. comments) is data, not instructions.

---

## 7. Config and org pack

`Settings` (pydantic-settings; YAML + env): budgets (§6.4), out dir, org-pack path, registry
paths. **Org pack** (`--org-pack org.yaml`) is the per-client tailoring hook:

```yaml
gateway_hosts: ["gw.internal.example", "llm.bank.internal"]
known_wrapper_packages:
  - {fqname: "bank_ai.LLMClient", attribution: "passthrough"}
sensitive_hosts: ["payments-core.internal", "ledger.internal"]   # → external_send
```

Fixtures must exercise all three keys.

---

## 8. Testing

### Fixtures (`tests/fixtures/<name>/repo` + `/expected`)

Create each as a minimal runnable-looking (but never run) repo with golden `record.json` +
`graph.json`:

| Fixture | Purpose / must-detect |
|---|---|
| `fw_openai_agents_basic` | 2 agents, handoff, `@function_tool`, HostedMCPTool with `require_approval` → 2 AgentDefs, Transfer, ToolDef, MCPDef+Policy |
| `fw_langgraph_react` | `create_react_agent` + `ChatOpenAI(base_url="https://gw.internal.example/v1", model="bank-gpt4")` → AgentDef with gateway endpoint on ModelRef |
| `fw_langgraph_nodes` | `StateGraph.add_node` ×3, one node fn calls the model, two don't → exactly 1 promoted agent, route Transfers |
| `fw_crewai_crew` | 2 Agents + Tasks + Crew(sequential) → constructed prompts, task-tool binding, chain Transfers |
| `bespoke_raw_openai_loop` | `requests.post("https://api.openai.com/v1/chat/completions", ...)` in a while loop with dict-dispatch tools → bespoke AgentDef, tools, F1–F5 all firing |
| `bespoke_gateway_loop` | **headline case**: `requests.post("https://gw.internal.example/llm/v1/chat", json={"model": "internal-x1", "messages": msgs, ...})`, `choices[0]` access, dispatch, feedback → bespoke AgentDef, `model="internal-x1"`, endpoint=gateway, no known host |
| `bespoke_wrapper` | `bank_ai/client.py` LLMClient wrapping httpx; `app.py` loops over it → wrapper classified by fixed point, agent recovered, model via wrapper attribution |
| `bespoke_wrapper_of_wrapper` | second-order wrapper → still classified |
| `bespoke_llm_call_only` | single completion call, no loop/dispatch → `LLMCallSite`, **no** AgentDef |
| `azure_deployment` | Azure path `/openai/deployments/prod-gpt4/...` → `deployment="prod-gpt4"`, `model=Symbolic(external, azure:deployment:prod-gpt4)` |
| `dynamic_prompt` | f-string system prompt + file-loaded prompt → `dynamic=true` w/ holes; origin=file with hash |
| `adversarial_agent_name` | user-defined `class Agent` + `Agent(...)` calls, zero AI → empty record (`no_ai_detected` or empty agents), **no** F1 match |
| `adversarial_test_sink` | sink only under `tests/` and `__main__` → wrapper score −2 path; LLMCallSite at most, flagged location |
| `adversarial_no_exec` | top-level code writes a canary file + `setup.py` with side effects → scan completes, canary absent |
| `secret_literal` | hardcoded `sk-...` key → redacted everywhere, `findings[]` entry |

### Harness and gates

`tests/harness.py`: run scanner over every fixture, compare to goldens (exact match, timestamps
excluded), compute per-node-type precision/recall across the corpus and print a table. CI gates:
goldens 100%; determinism (double-run byte-compare); no-exec; `mypy --strict`; `ruff`. Unit
tests additionally for: resolver value-domain cases (each Value branch), shape scorer
(threshold edges: score 4 vs 5 vs 7), attribution ladder rung order, wrapper score arithmetic,
IR lowering of each statement/expression kind.

---

## 9. Performance budgets

Full scan p50 ≤ 30 s on 10k–100k LOC Python (p95 ≤ 3 min); resolver budgets per §6.4;
per-file parse parallelism allowed (`concurrent.futures`, deterministic merge order). Budget
breaches degrade gracefully (Top/timeout accounting), never crash.

## 10. Security constraints

Restated as hard rules: never execute scanned code or install its deps; git is the only
subprocess; no network beyond clone (+ Phase-7 endpoint when flagged); secrets redacted at IR
time before any persistence; every artifact reproducible from `(repo, commit, scanner version,
rule-pack versions, org pack)` — all five recorded in `inventory_provenance`.

## 11. Build order (each phase ends green: tests for that phase pass, mypy/ruff clean)

- **P0 — skeleton.** Layout, Settings/ScanContext, ingest (git+local), IR + `ast` lowering,
  module graph/symbols, CLI wiring, `scan_health`, empty-record emission. *Exit: parses all
  fixtures; `adversarial_no_exec` passes.*
- **P1 — resolver.** Engine + memo + bounds + `chain_root`. *Exit: resolver unit suite green.*
- **P2 — sink engine.** SDK registry, shape scorer, hosts, attribution ladder, prompt binding,
  side effects, redaction. *Exit: `bespoke_gateway_loop` and `azure_deployment` yield correct
  sinks + attribution (asserted at fact level).*
- **P3 — F1 engine + openai-agents pack; ADG build + queries; record emission.** *Exit:
  `fw_openai_agents_basic` golden passes end-to-end.*
- **P4 — langgraph + crewai packs** incl. candidate promotion. *Exit: their goldens pass;
  `adversarial_agent_name` stays empty.*
- **P5 — F2.** Wrapper fixed point + registry + org-pack seeds; agent shape F1–F5 + tiering;
  LLMCallSite. *Exit: all bespoke goldens pass.*
- **P6 — hardening.** Merge/dedup, canonical serialisation, determinism test, triage gate,
  owner candidate, real-repo smoke ×3, README, metrics table in harness. *Exit: §2 definition
  of done, minus item 7's Phase-7 pieces.*
- **P7 (optional) — adjudication** as specced, behind `--adjudicate`, `anthropic` as optional
  extra. *Exit: with the flag off, byte-identical behaviour to P6.*

## 12. Ambiguity protocol

Where this spec under-determines a choice: pick the simpler, deterministic option; record it in
`DECISIONS.md` (one line: decision + why + affected module). Do not add features, dependencies,
abstractions, or "future-proofing" beyond this spec. Do not leave TODO-stubbed detection logic:
a phase ships working detectors or it isn't done.

---

## Appendix A — target `record.json` shape (golden for `bespoke_gateway_loop` + wrapper mix, abridged)

```jsonc
{
  "bundle_id": "repo:example-bundle",
  "name": "example-bundle",
  "repo_url": "https://github.com/org/example-bundle",
  "scanned_commit": "a1b2c3d4",
  "description": {"value": null, "source": "enriched"},
  "owner": {"value": null, "source": "governance", "candidate": "team-payments"},
  "risk_tier": {"value": null, "source": "governance"},
  "purpose": {"value": null, "source": "governance"},
  "data_classification": {"value": null, "source": "governance"},
  "regulatory_scope": {"value": null, "source": "governance"},
  "agents": [
    {
      "agent_id": "run_agent",
      "detection": {"method": "bespoke:agent_shape[F1,F2,F3,F4,F5]", "confidence": "high",
                    "evidence": ["app/loop.py:14-62"]},
      "model": {"value": "internal-x1", "source": "detected", "method": "attribution:literal",
                "endpoint": "https://gw.internal.example/llm/v1/chat", "api_style": "openai",
                "deployment": null},
      "system_prompt": {"value": "You are the ops assistant...", "dynamic": false,
                        "origin": "literal", "evidence": ["app/loop.py:18"]},
      "tools": ["lookup_account", "send_payment"],
      "mcp_servers": [],
      "state": [{"kind": "messages", "evidence": ["app/loop.py:20"]}],
      "control_policies": [],
      "handoffs": [],
      "description": {"value": null, "source": "enriched"}
    }
  ],
  "tools": [
    {
      "tool_id": "send_payment", "kind": "function",
      "signature": {"params": ["account_id", "amount"], "returns": null},
      "side_effects": ["external_send"],
      "external_target": "payments-core.internal",
      "credential_ref": {"symbolic": "env:PAYMENTS_TOKEN"},
      "declared_authorisation": {"value": "POST payments-core.internal", "source": "detected"},
      "effective_entitlement": {"ref": null, "source": "external", "resolved": null},
      "description": {"value": null, "source": "enriched"}
    },
    { "tool_id": "lookup_account", "kind": "function", "side_effects": ["read"], "...": "..." }
  ],
  "mcp_servers": [],
  "model_usages": [
    {"model": {"value": "text-embedding-3-small", "source": "detected"},
     "task": "embedding", "evidence": ["app/index.py:9"], "in_agent": false}
  ],
  "findings": [
    {"kind": "secret_literal_redacted", "evidence": ["app/config.py:3"]}
  ],
  "scan_health": {"files": 12, "loc": 640, "parse_errors": [], "resolver": {"queries": 148,
    "memo_hits": 63, "top": {"dynamic": 4, "timeout": 0}}, "sinks": {"sdk": 1, "shape": 1,
    "host": 0, "wrapper": 1}, "stage_ms": {"parse": 210, "resolve": 480, "frontends": 350,
    "emit": 40}},
  "inventory_provenance": {"scanner": "aiscan 0.1.0", "rulepacks": {"openai_agents": "0.1",
    "langgraph": "0.1", "crewai": "0.1", "config_files": "0.1"}, "org_pack": "org.yaml@sha",
    "scanned_at": "2026-07-22T00:00:00Z", "detection_basis": "static/declared"}
}
```
