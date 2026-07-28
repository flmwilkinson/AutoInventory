# Agent Inventory Scanner — Technical Design v0.1

Static analysis scanner that, given a repository at a commit, recovers every agent system in it
(framework-built or bespoke), binds each agent to its model, prompt, tools, MCP servers, memory
and control policies, and emits a bank-grade inventory record under the D/E/G/X governance
schema. On subsequent commits it recomputes, produces a structural delta, and classifies the
delta's materiality.

Methodology is derived from AgentFlow (arXiv 2607.01640). Where this design extends or departs
from the paper, the section says so explicitly.

Companion docs: `bank-ai-inventory-schema.md` (the output schema), `agent-inventory-detection-oss-review.md`
(landscape). This doc is the build spec.

---

## 1. Scope and non-goals

**In scope (v0):** Python codebases; framework detection for OpenAI Agents SDK, LangChain/LangGraph,
CrewAI; bespoke detection (raw HTTP, internal wrappers, corporate gateways); MCP and prompt/config
files; full-scan + structural diff + materiality classification; native JSON record; CycloneDX export.

**Non-goals (v0):** runtime observation (everything here is *declared* posture — record it as such);
JS/TS/Java (v1+); executing any scanned code (never — parse only); resolving values that only exist
at runtime (recorded as symbolic, see §7.4); MCP server tool enumeration over the wire (server
identity and policy are static; its tool surface is runtime — flagged, not fetched).

**Over-approximation semantics.** Like AgentFlow, the graph is what the architecture *permits*,
not the single path that runs. Agent routing depends on model output, so any static result is an
over-approximation. Every fact carries evidence (`file:line`) and confidence; nothing is invented.

---

## 2. Correspondence to AgentFlow

| AgentFlow (paper) | This design | Status |
|---|---|---|
| Framework frontend: 143 construct rules, 5 frameworks | Frontend F1: declarative YAML rule packs, 3 frameworks at v0 | Replicated, made data-driven (§8) |
| Alias resolution on YASA (Ant-internal engine) | Demand-driven resolver on own IR (tree-sitter + Python `ast`), §7 | Replaced — YASA unavailable |
| Agent facts vocabulary | Same vocabulary + two extensions (`LLMCallSite`, `ModelRef.endpoint`), §5 | Replicated + extended |
| ADG: typed nodes A/I/M/C/S/G; ACDG/ACFG/ADFG edges | Identical | Replicated (§12) |
| Agent-BOM as ACDG query | Identical, plus bundle-level record emission | Replicated + extended (§13) |
| Prompt-to-tool taint (P2T) | Sink side-effect classes reused for tool risk labels; full taint deferred | Partial (v0.5) |
| — (not in paper) | Frontend F2: bespoke/sink-based detection | **New** (§10) |
| — | Shared sink engine, endpoint-agnostic | **New** (§9) |
| — | Wrapper fixed-point + org wrapper registry | **New** (§10.2) |
| — | Bounded LLM tiers (adjudication, enrichment) | **New** (§11) |
| — | Governance merge (D/E/G/X), stable IDs, structural diff + materiality | **New** (§13–14) |

The architectural change that matters: **two frontends, one graph.** F1 (framework rules) and F2
(bespoke) both emit the same normalised facts into the same ADG, and both sit on the same sink
engine and resolver. Downstream (graph, BOM query, record, diff) never knows or cares how an agent
was found — only the `detection` provenance on the fact does.

---

## 3. Pipeline overview

```
                       ┌─────────────────────────────────────────────────┐
 repo@commit ──▶ S0 Ingest & triage ──▶ S1 Parse & module graph ──▶ S2 Resolver (demand-driven)
                       │  (AI-signal gate)      (tree-sitter IR)      ▲        │
                       │                                              │ resolve queries
                       │                 ┌────────────────────────────┴──────────────┐
                       │                 │        Shared sink engine  (§9)           │
                       │                 └───────▲──────────────────────▲────────────┘
                       │                         │                      │
                       │            F1 Framework frontend (§8)   F2 Bespoke frontend (§10)
                       │                         │                      │
                       │                         └──── agent facts ─────┘
                       │                                   │
                       │                    S4 Fact merge & ADG assembly (§12)
                       │                                   │
                       │        S5 LLM tiers: adjudication (gated) / enrichment (async) (§11)
                       │                                   │
                       │            S6 Inventory emission & governance merge (§13)
                       │                                   │
                       └──────▶ S7 Diff & materiality (vs previous scan) (§14) ──▶ record + delta
```

Everything through S4 is deterministic. S5 is the only place a model is called, and it is bounded,
cached, and skippable.

---

## 4. Value domain (what "resolved" means)

The resolver returns sets of abstract values. This domain is used everywhere below.

```
Value := Str(s)                                # literal string
       | Template(parts[], holes[])           # f-string / concat with unresolved holes → dynamic=true
       | Num | Bool | NoneV
       | Symbolic(kind, key)                  # kind ∈ {env, config, cli, wrapper_default, external}
       |                                      #   e.g. Symbolic(env, "MODEL_NAME"), Symbolic(config, "settings.yaml:llm.model")
       | ClassInstance(class_fq, ctor_args)   # instance of resolved class, constructor args captured
       | FuncRef(fq, def_site) | ClassRef(fq, def_site)
       | ListVal(elems[], open=bool)          # open=true when appends/extends not fully tracked
       | DictVal(entries{k:Value}, open=bool)
       | Top(reason)                          # unknown; reason ∈ {depth, unbound, dynamic, timeout, star_import}
```

Rules of use: any field may hold `Symbolic` or `Template` — the record stores the symbol/template,
never a guess. `Top` never enters the record silently; it becomes `unresolved` with a reason, and
may enter the adjudication queue (§11.1).

---

## 5. Fact vocabulary (frontend output schema)

All frontends emit these. Pydantic models; every fact has `evidence: [FileSpan]`,
`confidence: {high, medium, low}`, `method` (which rule/detector produced it), `source_files: []`
(for diff invalidation, §15).

**Entities**

```
AgentDef   {id, name, kind: framework|bespoke, framework?: str}
PromptDef  {id, content: Value, dynamic: bool, origin: literal|file|config|constructed, file_ref?}
ModelRef   {id, provider?: str, model: Value, endpoint: Value?, deployment?: Value,   # §9.4
            api_style: openai|anthropic|bedrock|vertex|unknown}
ToolDef    {id, name, kind: function|mcp_tool|schema_declared|retriever|agent_as_tool,
            signature?: {params[], returns?}, side_effects: [SideEffect], external_target?: Value,
            credential_ref?: Value}
MCPDef     {id, server: Value, transport: stdio|sse|http|ws, declared_tools?: [str]|dynamic,
            approval_policy?: Value}
StateDef   {id, kind: messages|session|vectorstore|shared_object, backing?: Value}
PolicyDef  {id, kind: approval|guardrail|permission_check, params: DictVal}
LLMCallSite{id, model: ModelRef, prompt?: PromptDef, in_agent: bool=false}      # EXTENSION: plain
           # model use with no agent shape — banks must inventory these too. Maps to a bundle-level
           # "model usage" entry, not an Agent record.
```

**Bindings (ACDG):** `BindModel{agent, model}` `BindPrompt{agent, prompt}` `BindTool{agent, tool}`
`BindMCP{agent, mcp}` `BindState{agent, state}` `AttachPolicy{policy, target: agent|tool}`

**Control (ACFG):** `Call{agent, tool, guard?: policy}` `Transfer{from, to, kind: handoff|invoke|route,
guard?}` `BranchIf{policy, target}`

**Data (ADFG):** `ToolCallArg{agent, tool, exprs}` `ToolCallRet{tool, agent}`
`ReadState{agent, state}` `WriteState{agent, state}` `InterAgentMsg{from, to, via?: state}`

`SideEffect ∈ {read, write, external_send, code_exec, admin_mutation}` (multi-label), assigned per §9.6.

---

## 6. S0 — Ingestion and triage

Input: `(repo_url|path, commit)` for full scan; `(base, head, changed_files[])` for diff scan.

Checkout is read-only; no install, no build, no execution. Language census by extension.

**AI-signal triage gate** (recall-biased; regex/manifest level; goal is to skip the deep pass on
repos with no AI at all, not to be clever):

| Signal | Source |
|---|---|
| Known AI package in manifests | `requirements*.txt`, `pyproject.toml`, `poetry.lock`, `Pipfile`, `setup.cfg/py` vs package registry (openai, anthropic, langchain*, langgraph, crewai, llama-index, litellm, boto3+bedrock, google-genai, mistralai, semantic-kernel, autogen, mcp, …) |
| SDK import string | grep for import lines of the above |
| LLM host substring | any file contains a host from the provider registry (§9.3) |
| Shape tokens near HTTP | `"model"` within N=300 chars of `"messages"|"prompt"|"input"` AND a `requests.|httpx.|aiohttp.|urllib` token in the same file |
| Chat-path fragment | `/chat/completions`, `/v1/messages`, `:generateContent`, `/converse`, `/invoke` in any string |
| Prompt/agent artefacts | `prompts/` dir, `*.prompt`, `system*.md`, `SKILL.md`, `.mcp.json`, `mcp.json`, agent YAMLs |
| Org wrapper import | import of any package in the org **wrapper registry** (§10.2) — this is why triage stays valid in a bank with a house SDK |

Zero signals AND no existing record for this repo → emit `no_ai_detected` stub (with scan
provenance) and stop. Any signal → full pipeline. A previously-inventoried repo always gets the
full pipeline (so removals are detected).

---

## 7. S1–S2 — Parsing, module graph, resolver

### 7.1 Parse and IR

tree-sitter (`tree-sitter-python`) parses every `.py`; parse trees are lowered to a per-file IR
(our own dataclasses): imports, class defs (bases, body assigns, methods), function defs (params,
decorators, body statements), assignments, calls (callee expr, args, kwargs), string/f-string/dict/
list literals, `return`s, loops, branches, `with` blocks. Python's native `ast` is used as a
cross-check in CI (tree-sitter and `ast` disagreement → test failure), but tree-sitter is the
production path because its incrementality feeds §15. Per-file IR is cached keyed by content hash.

### 7.2 Module graph and symbol tables

Resolve imports to files: absolute, relative, `as` aliases; packages via `__init__.py`; namespace
packages; `from x import *` recorded as a low-confidence edge (`Top(star_import)` on lookups that
would need it). Third-party imports resolve to a *package identity* (name + version from lockfile),
not source — unless the package is org-internal and its source is available (vendored, monorepo,
or configured internal index checkout), in which case it joins the analysis set (needed for
wrapper classification of house SDKs, §10.2). Per-module symbol table: imports, top-level defs,
top-level assignments.

### 7.3 Demand-driven resolver

We do not compute whole-program points-to. The frontends issue **resolution queries** only for
expressions they care about (arguments of matched constructs, callee identities, HTTP payloads).

```
def resolve(expr, ctx, depth=0) -> set[Value]:
    if depth > MAX_DEPTH (8): return {Top(depth)}
    memo[(file_hash, expr_span)] if present → return
    match expr:
      StrLit(s)            → {Str(s)}
      FString(parts)       → {Template(parts, holes=[resolve(h) for h in hole_exprs])}
      Name(n)              → lookup order: local scope → enclosing fn scopes → module globals
                             (union of reaching assignments*) → imports (follow chain across
                             modules, hop limit 4) → builtins → Top(unbound)
      Attr(base, a)        → for v in resolve(base):
                               ModuleRef  → member lookup in that module's table
                               ClassRef c → class-body assign or method a
                               ClassInstance c → class attr, or `self.a = …` in c.__init__ /
                                                 dataclass field (resolve RHS with ctor_args bound)
                               else Top
      Call(callee, args)   → for f in resolve(callee):
                               ClassRef c  → {ClassInstance(c, bind(args))}
                               FuncRef fn  → if fn has ≤K_RET(2) returns of resolvable exprs:
                                             union(resolve(ret_expr, scope=fn with args bound,
                                             depth+1)); else Top(dynamic)
      List/Tuple(elts)     → ListVal([resolve(e)…], open = any tracked .append/.extend on the
                             same name in this scope not fully resolved)
      Dict(entries)        → DictVal(literal-keyed entries resolved; open if **spread or
                             computed keys)  + fold in `d[k] = v` assigns on same name, same scope
      Subscript(b, StrLit) → lookup in DictVal
      BinOp(+, str, str)   → fold literals else Template
      os.environ[.get](K)  → {Symbolic(env, K)}
      json/yaml/toml load + literal key path → {Symbolic(config, "file:key.path")}
      argparse/click value → {Symbolic(cli, name)}
    record trace: every (file, symbol) hop appended to ctx.trace → reverse index for §15
    * flow handling: within a function, straight-line reassignment = last-wins; across branches =
      union. No SSA in v0; over-approximation is acceptable and matches the ADG semantics.
```

Bounds: `MAX_DEPTH=8`, cross-module hops ≤4, per-query wall clock 250 ms, per-scan resolver budget
60 s (over budget → remaining queries return `Top(timeout)`; counted in scan health metrics).
Memo table persists across scans keyed by content hashes, so unchanged files cost nothing on rescan.

Optional cross-check: Jedi name resolution on a sample of queries in CI to catch resolver
regressions. Not a production dependency.

---

## 8. Frontend F1 — framework construct registry

Declarative rules; the engine is generic. A rule matches on **resolved identity**, never surface
text: `Agent(...)` fires only if `Agent` resolves to `agents.Agent` (openai-agents) — a user's own
`Agent` class does not match. This single decision removes the dominant false-positive class of
AST-grep tools.

### 8.1 Rule schema (YAML)

```yaml
- id: <framework>.<construct>            # stable rule id (goes into fact.method)
  framework: openai-agents               # + version_range when constructs shift
  match:
    kind: call | decorator | class_base | config_file
    callee_fqname: agents.Agent          # for call/decorator; resolved fq name
    # or: base_fqname / file_glob for the other kinds
  extract:                               # arg → resolution directive
    name:         {arg: name,         resolve: string, required: true}
    model:        {arg: model,        resolve: model_ref}       # §9.4 ladder
    instructions: {arg: instructions, resolve: prompt}
    tools:        {arg: tools,        resolve: ref_list}
    handoffs:     {arg: handoffs,     resolve: ref_list}
    mcp_servers:  {arg: mcp_servers,  resolve: ref_list}
  emit:
    - AgentDef{name: $name, kind: framework, framework: openai-agents}
    - BindModel{model: $model}
    - BindPrompt{prompt: $instructions}
    - foreach $tools:       BindTool
    - foreach $handoffs:    Transfer{kind: handoff}
    - foreach $mcp_servers: BindMCP
```

Resolution directives: `string` (Str/Template/Symbolic accepted), `model_ref` (§9.4),
`prompt` (string + origin detection: file-loaded if value traces to `open/ Path.read_text/
importlib.resources/ jinja get_template` → link file, hash content), `ref` / `ref_list`
(FuncRef/ClassInstance targets → entity facts for each), `policy` (e.g. `require_approval`).

### 8.2 v0 rule packs (the constructs that matter)

**openai-agents:** `agents.Agent(...)` (above); `@agents.function_tool` → ToolDef(kind=function,
signature from def, side-effects §9.6); `agents.HostedMCPTool / MCPServerStdio / MCPServerSse /
MCPServerStreamableHttp` → MCPDef (+ `AttachPolicy(approval)` from `require_approval`);
`agents.Runner.run(agent, …)` → entrypoint marker; `handoffs=[…]` handled in ctor rule;
`agents.ModelSettings` merged into ModelRef.

**langchain / langgraph:** chat-model ctors (`langchain_openai.ChatOpenAI`,
`langchain_anthropic.ChatAnthropic`, `init_chat_model`, …) → ModelRef — **capture `base_url` /
`azure_endpoint` / `api_base` into `ModelRef.endpoint`**: the internal-gateway case appears inside
frameworks too, not just raw HTTP; `@tool` / `StructuredTool.from_function` / `Tool(...)` →
ToolDef; `model.bind_tools(tools)` → association consumed when a node calls that model;
`StateGraph.add_node(name, fn)` → *AgentCandidate* (not AgentDef): promoted to
`AgentDef(kind=framework)` iff `fn` reaches an LLM sink (§9) or is a prebuilt
(`create_react_agent`, `ToolNode` peers) — LangGraph nodes are not all agents, and this
sink-composition is how F1 and F2 share machinery; `add_edge / add_conditional_edges` →
`Transfer{kind: route, guard: condition FuncRef}`; `MemorySaver / checkpointers /
InMemoryStore` → StateDef; `langchain_mcp_adapters` → MCPDef.

**crewai:** `crewai.Agent(role, goal, backstory, llm, tools, …)` → AgentDef (name=role;
PromptDef constructed from goal+backstory parts, origin=constructed); `crewai.Task(agent=…,
tools=…)` → task-level BindTool onto that agent; `crewai.Crew(agents, tasks, process=…)` →
Transfers: `sequential` → chain in task order; `hierarchical` → manager→worker star (manager
agent from `manager_llm/manager_agent`); `crewai.tools.BaseTool` subclasses / `@tool` → ToolDef;
`memory=True` / embedder config → StateDef.

**Config-side rules (framework-agnostic):** `.mcp.json` / `mcp.json` / Claude-style config →
MCPDef per server `{command|url, transport}`, `declared_tools` if listed else `dynamic`;
`prompts/**`, `*.prompt`, `system*.md` → PromptDef candidates, bound when any resolved string
equals/endswith the path; `SKILL.md` frontmatter → ToolDef(kind=schema_declared).

Registry maintenance is an ongoing cost (AgentFlow names it as such). Keeping rules in YAML with
`version_range` means new framework versions are rule-pack releases, not engine changes.

---

## 9. Shared sink engine (the endpoint-agnostic core)

A **sink** is a call site classified as invoking an LLM. Both frontends consume sinks. Four sink
kinds, checked in this order; first match wins, others recorded as corroboration.

### 9.1 SDK sinks

Callee resolves to a registered `(package, method-chain)`:

```
openai:    OpenAI().chat.completions.create | .responses.create | AzureOpenAI().…
anthropic: Anthropic().messages.create
boto3:     client("bedrock-runtime").invoke_model | .converse
google:    genai.GenerativeModel().generate_content | google.genai Client.models.generate_content
mistralai, cohere, groq, together, fireworks, openrouter (openai-compatible), litellm.completion,
instructor-wrapped clients (resolve through to base)
```

Method-chain matching works on the resolver's `ClassInstance`/module attribution, so
`client = OpenAI(base_url=GW); client.chat.completions.create(...)` attributes provider=openai
*and* endpoint=GW.

### 9.2 HTTP-shape sinks — host-agnostic by construction

Callee is a generic HTTP call (`requests.(post|request)`, `httpx.(Client|AsyncClient).(post|
request|stream)`, `aiohttp.ClientSession.post`, `urllib.request.urlopen`, `http.client`) **and**
the resolved request scores as LLM-shaped. The URL host is deliberately *not* required — this is
what makes `https://gw.bank.internal/llm/v1/chat` detectable.

Payload = resolved `json=`/`data=`/`body` arg (DictVal, possibly assembled across statements and
one call-return hop, §7.3). Score over known keys; `open=true` DictVal lowers confidence one step,
never the score.

| Signal | Weight |
|---|---|
| URL path contains `/chat/completions` \| `/completions` \| `/v1/messages` \| `:generateContent` \| `/converse` \| `/generate` \| `/invoke` | +3 |
| payload has `model` AND `messages` | +3 |
| payload has `model` AND (`prompt` \| `input` \| `contents`) | +2 |
| response handling accesses `choices[0](.message)` \| `content[0].text` \| `candidates[0]` \| `stop_reason` \| `finish_reason` | +2 |
| auth header `Authorization: Bearer` \| `x-api-key` \| `api-key` \| `anthropic-version` | +1 |
| payload has `temperature` / `max_tokens` / `max_completion_tokens` / `top_p` (cap) | +1 (max +2) |
| `stream: true` or SSE handling | +1 |

**≥5 → sink (confidence high if ≥7, else medium). 3–4 → candidate → adjudication queue (§11.1)
if budget allows, else recorded as `suspected_llm_call` finding. ≤2 → not a sink.**

### 9.3 Known-host sinks

URL host (resolved; Template hosts match on literal prefix parts) in the provider registry
(`api.openai.com`, `api.anthropic.com`, `*.openai.azure.com`, `bedrock-runtime.*.amazonaws.com`,
`generativelanguage.googleapis.com`, `*-aiplatform.googleapis.com`, `api.mistral.ai`,
`api.groq.com`, `api.together.xyz`, `openrouter.ai`, `api.cohere.com`, …, **plus a per-org
extension list — the bank's own gateway hosts, configured once per client**). Catches payloads
assembled too dynamically to shape-score.

### 9.4 Model / endpoint attribution ladder (the "model name without a known URL" requirement)

For every sink, `ModelRef.model` and `.endpoint` resolve through this ladder; the first rung that
yields a non-Top value wins, and the rung is recorded in provenance:

1. **Literal at the call site** — `model="gpt-4o"` kwarg or payload key.
2. **Constant** — resolves through assignments/constants (`MODEL = "bank-gpt4-prod"`).
3. **Config/env symbolic** — `Symbolic(env, "LLM_MODEL")` / `Symbolic(config, "settings.yaml:llm.model")`.
   Recorded as the symbol. If the deployment supplies a config bundle (optional scanner input:
   resolved env/config for the target environment), the symbol is dereferenced and the value
   recorded with provenance `config_resolved`.
4. **Wrapper default** — model fixed inside a classified wrapper (§10.2): attributed from the
   wrapper's ctor/constants; call sites inherit it as `Symbolic(wrapper_default, wrapper_fq)`
   unless they pass their own.
5. **Unresolved** — enters adjudication queue if it blocks a required field; else recorded
   `unresolved(reason)`.

**Azure/deployment split:** path `.../openai/deployments/{name}/chat/completions` or
`deployment_name=` kwarg → `ModelRef.deployment = name`; the underlying foundation model is *not
in the code* — recorded as `model: Symbolic(external, "azure:deployment:{name}")`, which maps to
an **[X]** field in the record (resolved against the client's Azure config out-of-band). Same
treatment for gateway-side model aliasing (`model="team-alias-1"` → recorded as the alias; alias→
foundation-model mapping is an [X] linkage to the gateway's routing table). The scanner records
what the code declares; it does not guess what an alias resolves to.

`api_style` is inferred from shape (openai-style `messages/choices` vs anthropic-style
`messages/content+stop_reason` vs bedrock/vertex payloads) — useful because internal gateways are
overwhelmingly openai-compatible, and style tells the enrichment tier how to read the payload.

### 9.5 Prompt binding at sinks

`system` key / first `role:"system"` message / anthropic `system=` param / vertex
`system_instruction` → PromptDef via the `prompt` directive (origin detection incl. file loads;
Template → `dynamic=true` with holes listed). Messages assembled in a variable are resolved as
ListVal; `dynamic=true` when open.

### 9.6 Tool side-effect classification

For every ToolDef with a resolvable body (function tools, dispatch targets), classify by sinks in
the body (multi-label):

| Evidence in body | SideEffect |
|---|---|
| HTTP GET / DB select / file read | read |
| DB insert/update, file write, cache set | write |
| HTTP POST/PUT/DELETE to non-LLM host, smtplib/SES/SQS/Kafka/Slack SDK, payment/ledger internal hosts (org-configurable list) | external_send |
| `subprocess`, `exec/eval`, container/K8s SDK | code_exec |
| IAM/user-management/entitlement SDK calls (boto3 iam, msgraph directory, …) | admin_mutation |

`external_target` = resolved host/service; `credential_ref` = resolved auth source
(`Symbolic(env, "PAYMENTS_TOKEN")`, vault path literals, boto3 profile) — the *reference*, never a
value; secret-looking literals are redacted at parse time (§17) and flagged as findings.

---

## 10. Frontend F2 — bespoke detection

### 10.1 Structure

Three layers over the sink engine: wrapper fixed-point (10.2) extends the sink set; agent-shape
reconstruction (10.3) turns sink neighbourhoods into AgentDefs; residual ambiguity goes to
adjudication (§11.1). Sinks that end up in no agent are emitted as `LLMCallSite` facts —
inventoried model usage without an agent.

### 10.2 Wrapper fixed-point (org-adaptive)

Goal: `from bank.ai import LLMClient` becomes a first-class sink without anyone writing a
signature for it.

```
classified: dict[def_id → WrapperInfo] = org_wrapper_registry.load()   # persisted across scans/repos
worklist   = enclosing_defs(all sinks from §9)                          # seeds
while worklist:
    d = worklist.pop()
    if d in classified: continue
    if wrapper_score(d) >= 3:
        classified[d] = WrapperInfo(
            attribution = fixed        if model/endpoint constant inside d
                        | passthrough  if model param forwarded to the sink   # resolve per call site
                        | default      (ctor/config-held; Symbolic(wrapper_default, d))
        )
        for cs in callsites_resolving_to(d):        # via resolver, whole analysis set
            mark cs as wrapper sink (inherits/derives ModelRef per attribution mode)
            worklist.add(enclosing_def(cs))          # wrappers of wrappers

wrapper_score(d):
    +2  body contains a call currently classified as a sink (any kind)
    +1  params ∩ {prompt, messages, model, system, input, text, query, temperature} ≠ ∅
        or **kwargs forwarded into the sink call
    +1  returns the sink result (directly or after field extraction like .choices[0]…)
    +1  (classes) method names ∩ {chat, complete, completion, generate, invoke, ask, query, call}
    −2  sink appears only under `if __name__ == "__main__"`, tests/, or docstrings
```

Monotone (classifications only added), finite defs → terminates. **Org wrapper registry**:
classifications persisted keyed by `(package_fq, content_hash)` with scope=org. Repo B importing
`bank.ai.LLMClient` gets the classification at triage time without re-derivation. Wrapper defined
in an installed internal package: classified from package source when the org's internal index
checkout is configured (§7.2); otherwise marked `external_opaque` → one-time adjudication (§11.1),
result cached in the registry. Registry entries are themselves evidence-carrying facts
(auditable, revocable).

### 10.3 Agent-shape reconstruction

Anchor = smallest enclosing callable region R containing ≥1 sink. Features over R's IR (loop/
branch structure + resolver):

```
F1 iteration     : sink inside while/for, or recursion on R, or explicit turn-budget counter
F2 dispatch      : branch/match on sink-result fields (tool_calls, function_call, name,
                   stop_reason, finish_reason) OR mapping-lookup dispatch (registry dict of
                   callables / getattr / if-elif chain keyed on response content), followed by a
                   call whose callee resolves from the dispatch
F3 feedback      : dispatched call's return value flows into a subsequent sink payload
                   (append to messages / tool-result message)
F4 message state : list accumulating role-tagged dicts across iterations, or payload
                   carries "tools"/"functions" schemas
F5 termination   : predicate on response (absence of tool_calls, stop_reason==end_turn,
                   max-iteration guard)
```

Deterministic tiering (no ML):

| Evidence | Classification |
|---|---|
| F1 ∧ F2 ∧ (F3 ∨ F4) | **AgentDef(kind=bespoke)**, confidence high |
| F2 ∧ F3 (single-step tool use) — or — F1 ∧ F4 (conversational loop, tools in payload) | AgentDef(kind=bespoke), confidence medium |
| ≥2 features, pattern unclear | AMBIGUOUS → adjudication queue |
| sink, <2 features | **LLMCallSite** (not an agent) |

Bindings for bespoke agents: tools = F2 dispatch targets (each a ToolDef with signature +
§9.6 side-effects) ∪ payload `tools`/`functions` schema entries (ToolDef kind=schema_declared —
declared to the model, implementation matched by name when a dispatch target of that name
exists); prompt per §9.5; model/endpoint per §9.4; state = the F4 accumulator (StateDef
kind=messages) or session/vector objects touched in R; policies = human-approval patterns
(`input()`/confirm gates dominating a dispatch branch) → PolicyDef(kind=approval, confidence
medium).

Multi-agent (bespoke): `Transfer{kind: invoke}` when R₁'s sink output flows into an invocation of
anchor R₂; `InterAgentMsg{via: state}` when two anchors read/write the same resolved state object.
Framework↔bespoke edges compose naturally (e.g. a CrewAI agent whose tool wraps a bespoke loop)
because both sides are just facts.

---

## 11. LLM tiers (bounded, cached, never load-bearing)

Two uses only. Everything else is deterministic. Expected common case: **zero adjudication calls
per scan** (framework repos and clean bespoke shapes never reach the gate).

### 11.1 Adjudication (sync, gated)

Admitted: AMBIGUOUS agent-shapes; shape-score 3–4 sink candidates; `external_opaque` wrappers;
`unresolved` model values blocking a required field. Budget: `N=20` calls/scan (config), priority
= candidates nearest write-capable side effects first; over-budget items recorded
`unadjudicated` (surfaced in scan health, retried next scan).

Context = a **slice**, never whole files: anchor region source + signatures of resolved dispatch
targets + the sink call + ≤40 lines around each + module imports. Cap ~6k tokens in / 500 out.
Temperature 0, structured output:

```json
{"is_agent": bool, "confidence": 0.0-1.0,
 "agents": [{"name": str, "model_expr": str|null, "prompt_ref": str|null, "tools": [str]}],
 "wrapper": {"is_llm_wrapper": bool, "attribution": "fixed|passthrough|default"}|null,
 "abstain": bool, "rationale": "≤30 words"}
```

Controls: cache by slice content hash (unchanged code never re-adjudicated); model + version
pinned and recorded in provenance; accept only `confidence ≥ 0.7`, else UNRESOLVED — the scanner
**never guesses**; optional two-vote mode for `external_send/admin_mutation`-adjacent candidates
(disagree → UNRESOLVED). Facts created here carry `method: llm_adjudicated` and confidence
capped at `medium`; they may only **add** where deterministic layers abstained, never override a
deterministic fact, and never touch [G]/[X] fields. The adjudicator has no tools; its output is
schema-validated JSON; scanned code (including comments) is data, not instructions (§17).

### 11.2 Enrichment (async, non-blocking)

Drafts the [E] fields (bundle/agent/tool descriptions, autonomy-level suggestion) after record
emission. Batched; cached by node content hash (only changed nodes re-drafted); output written to
`{value, source: enriched, confirmed_by: null}` slots only. A scan is complete without it.

---

## 12. S4 — Fact merge and ADG assembly

**Entity identity:** `AgentDef` = `(bundle, kind, name)` with anchor `file:line` as tiebreak for
duplicate names; `ToolDef` = resolved fq name (functions) / `(server, tool)` (MCP) / schema name
(declared); `ModelRef` = `(provider|api_style, model_value, endpoint_value)`; `PromptDef` =
content hash (or template-shape hash when dynamic).

**Merge:** F1 and F2 finding the same agent (e.g. a LangGraph node that is also a detected loop)
→ one node, `union(evidence)`, `max(confidence)`, `detection.method` lists both. Same for a
ModelRef seen via SDK sink and shape sink.

**Graph:** typed property graph (`rustworkx`; `networkx` acceptable v0), nodes carry fact
payloads, edges labelled with family (ACDG/ACFG/ADFG) + type. **Canonical serialisation** —
sorted node/edge order by stable id, stable field order — so the graph JSON is byte-diffable and
hashable (§14).

**Queries:** `agent_bom(a)` = ACDG neighbours of `a` grouped by node type (this *is* the paper's
BOM query); `bundle_bom` = all agents' BOMs + orphan `LLMCallSite`s + unbound MCP/prompt
artefacts; `reach(prompt→tool)` over ADFG for the risk view (v0.5).

---

## 13. S6 — Inventory emission and governance merge

Maps `bundle_bom` onto the D/E/G/X schema (companion doc). Merge semantics against the previous
record:

| Class | On rescan |
|---|---|
| [D] | Overwritten by current scan, always. Per-field provenance `{method, evidence, confidence, commit, rulepack_version}`. |
| [D, method=llm_adjudicated] | Overwritten by any deterministic result; by a new adjudication only if confidence ≥ previous. |
| [E] | `draft` refreshed (only when the underlying node's content hash changed); `confirmed` value untouched; drafts kept in `draft_history`. If a confirmed [E] field's underlying node changed **materially** (§14), set `reconfirm_required=true` — flag, never edit. |
| [G] | Never written. If a material delta contradicts a [G] decision's basis (e.g. risk tier set when the agent had no external_send tool, and one just appeared), raise `review_flag` on that field. |
| [X] | Linkage refs updated if the code-side ref changed (new `credential_ref`); resolution values untouched. |

**Stable agent IDs across scans** (what keeps [G]/[E] fields attached through refactors):

```
fingerprint(a) = sha256(model_id | prompt_hash | sorted(tool_names) | sorted(mcp_servers))
match(old_set, new_set):
  1. exact (kind, name) matches paired first
  2. remaining: similarity s = 0.5·Jaccard(tools) + 0.2·[model equal] + 0.2·simhash_sim(prompt)
                             + 0.1·name_ratio(Levenshtein)
     Hungarian assignment; pairs with s ≥ τ=0.6 → same agent (renamed → record rename event)
  3. unmatched old → AgentRemoved; unmatched new → AgentAdded
```

**Dual record views** (the bank pattern that resolves "materiality-gated updates" correctly):
the record holds `detected` (always the current scan's truth — it must never lag the code) and
`approved` (the last governance-signed state). Materiality does **not** gate the update of
`detected`; it gates whether `detected → approved` requires re-approval. Immaterial deltas
auto-promote (configurable); material deltas leave `approved` intact and open a review flag with
the delta attached. The published/consumed view is a client policy choice (`detected`,
`approved`, or `approved+pending-delta`).

---

## 14. S7 — Diff and materiality

**Trigger file set** (webhook on push/merge to default branch): `*.py`, prompt files
(`prompts/**`, `*.prompt`, `system*.md`, any file a PromptDef links), MCP/agent config
(`.mcp.json`, agent YAMLs, `SKILL.md`), dependency manifests, scanner config. Changes outside
this set → record touch only (`last_seen_commit`), no scan.

**v0 strategy: full rescan + structural diff.** AgentFlow-class analysis is seconds per repo
(median 14 s in the paper; our resolver is bounded the same way), and full-rescan has no
invalidation bugs by construction. The incremental path (§15) is a v1 optimisation for monorepos,
switched by repo-size threshold — exactly the "fresh run and compare, diff as optimisation"
position, made concrete.

**Structural diff:** canonical graph JSON (old, new) → node/edge set difference keyed by stable
IDs (§13) → typed delta ops:

```
AgentAdded/Removed/Renamed · ModelChanged(agent, old→new) · EndpointChanged(agent|site, old→new)
PromptChanged(agent, old_hash→new_hash, dynamic_flag_change) · ToolBound/ToolUnbound(agent, tool)
ToolSideEffectChanged(tool, added[], removed[]) · ToolTargetChanged(tool, old→new)
CredentialRefChanged(tool, old→new) · MCPAdded/Removed(server) · MCPPolicyChanged(server, old→new)
PolicyAdded/Removed/Changed(target, kind, old→new) · TransferAdded/Removed(from, to)
StateAdded/Removed(agent, kind) · LLMCallSiteAdded/Removed · WrapperClassified(pkg)
```

**Materiality pack** — data-driven YAML (risk teams tune it without code), first-match-wins per
op, evaluated per delta then aggregated (bundle materiality = max):

```yaml
- op: ToolBound
  when: {tool.side_effects ∩ [external_send, admin_mutation, code_exec] != []}
  class: material_high
- op: ToolBound                     # read-only tool
  class: material
- op: PolicyChanged
  when: {direction: weakened}       # e.g. require_approval always→never
  class: material_high
- op: ModelChanged | EndpointChanged | MCPAdded | TransferAdded | CredentialRefChanged
  class: material
- op: PromptChanged
  when: {content_changed: true}     # bank-safe default: any prompt content change is material
  class: material
- op: PromptChanged
  when: {whitespace_or_comment_only: true}
  class: cosmetic
- op: AgentRenamed
  class: immaterial
- op: LLMCallSiteAdded
  class: material                   # new model usage is inventory-relevant even with no agent
- default: immaterial
```

Output per diff scan: updated record (per §13 semantics), machine-readable delta report
`{ops[], materiality, review_flags[]}`, and scan-health block (resolver timeouts, unadjudicated
count, Top rates).

---

## 15. Incremental path (v1)

Every fact carries `source_files[]`; every resolution stores its trace (§7.3) → reverse index
`file → dependent facts/queries`. On change set F: re-parse F (tree-sitter incremental),
invalidate facts sourced in F **and** facts whose resolution traversed F, re-run frontends on F,
re-issue invalidated queries, rebuild affected subgraph, then the same structural diff as §14.
Sound because invalidation follows recorded traces, not guesses. Ship only after the v0
full-rescan path is the tested oracle (CI asserts incremental ≡ full on the corpus).

---

## 16. Performance and cost budgets

| Budget | Target |
|---|---|
| Full scan p50, 10k–100k LOC Python | ≤ 30 s |
| Full scan p95 | ≤ 3 min |
| Diff scan (v0 = full rescan, warm caches) p50 | ≤ 10 s |
| LLM adjudication | ≤ N=20 calls/scan, ~6k tok in / 500 out each; common case 0 |
| Enrichment | async, batched, cached; never blocks |

Parallelism: per-file parse and per-query resolution parallel over a shared memo table; frontends
independent. All caches (parse IR, resolver memo, adjudication, wrapper registry) content-hash
keyed and persistent, so rescans of mostly-unchanged repos are dominated by the diff, not the scan.

---

## 17. Security and data handling (bank posture)

Never execute scanned code; parse only; no dynamic import; no network from the analysis sandbox
except the LLM endpoint (client-approved deployment: in-tenant Azure OpenAI / in-VPC Bedrock /
on-prem). Secret-looking literals (key-shaped strings, `sk-`, PEM blocks) are redacted at IR
construction — before any cache write or LLM slice — and reported as findings. Every adjudication
slice is logged (what was sent, where, model+version). Adjudicator output is schema-validated
JSON; it has no tools; prompt-injection in scanned code (comments/docstrings/prompts instructing
the analyser) is inert by construction and a corpus test case. Full provenance on every field:
evidence, method, commit, scanner version, rule-pack versions, model version where applicable.

---

## 18. Storage

Content-addressed artifact store per scan (IR cache, facts, canonical graph JSON, record, delta)
+ SQLite (v0; Postgres later):

```
scans(scan_id, bundle_id, commit, started, finished, scanner_ver, rulepack_vers, health_json)
bundles(bundle_id, repo_url, default_branch, record_json_current, record_json_approved)
agents(bundle_id, agent_id, fingerprint, first_seen, last_seen, renamed_from?)
facts_provenance(scan_id, fact_id, type, method, confidence, evidence_json, source_files)
wrapper_registry(pkg_fq, content_hash, info_json, scope, classified_by, first_seen)
adjudication_cache(slice_hash, model_ver, response_json, ts)
deltas(scan_id, base_commit, ops_json, materiality, flags_json)
```

---

## 19. Tech stack and repo layout

Python 3.12 · tree-sitter + tree-sitter-python · own IR/resolver (Pydantic v2 models) ·
rustworkx · PyYAML rule/materiality packs · SQLite · typer CLI · pytest + corpus harness ·
`cyclonedx-python-lib` for export only.

```
scanner/
  ingest/        # checkout, triage, trigger handling
  parse/         # tree-sitter driver, IR lowering, ast cross-check
  ir/            # IR + value-domain dataclasses
  resolve/       # demand-driven resolver, memo, traces
  sinks/         # §9: sdk registry, shape scorer, host registry, attribution ladder
  frontends/
    framework/   # rule engine + packs/{openai_agents,langgraph,crewai,config}.yaml
    bespoke/     # wrappers.py (fixed point), agent_shape.py, llm_call_sites.py
  adjudicate/    # slicer, schema, cache, budget
  graph/         # model.py, build.py, queries.py, canonical.py
  diff/          # structural.py, materiality.py, packs/materiality_v1.yaml
  inventory/     # schema.py (D/E/G/X), merge.py, ids.py, export_cyclonedx.py
  enrich/        # async description drafting
  store/         # artifact store + sqlite
  cli.py         # aiscan scan|diff|record|eval
eval/
  corpus/        # see §20
  harness.py
```

CLI: `aiscan scan <path|url> [--commit C] [--config-bundle env.json] [--out DIR]` ·
`aiscan diff <base> <head>` · `aiscan record show|export --format cyclonedx` ·
`aiscan eval eval/corpus`. The webhook service is a thin wrapper around `scan`/`diff`.

---

## 20. Evaluation

**Corpus (ground truth, hand-labelled per-agent bundles):** ~20 public framework repos across the
three frameworks; ~15 synthetic bespoke repos (raw requests/httpx loops, internal-wrapper style,
gateway URLs, dynamic prompts, dict-dispatch tools, Azure deployment paths); ~5 adversarial
(user classes named `Agent`, dead code, vendored SDKs, prompt-injection comments, sinks in tests).

**Named metrics:** entity precision/recall per node type; **binding** precision/recall (the
dimension where AgentFlow beats AST tools — ours to match); **gateway model attribution rate**
(% of sinks with non-registry endpoints where `model` resolves to a value or correct symbolic —
the requirement this design exists for); wrapper recall on the synthetic house-SDK repos;
false-agent rate on adversarial; diff correctness via **mutation testing** (scripted mutations —
swap model, add tool, weaken policy, whitespace-only prompt edit — must yield exactly the
expected delta op and materiality class); scan-health regression (Top rates, timeouts).

CI runs the full corpus on every engine or rule-pack change.

---

## 21. Milestones

**M0 — walking skeleton (2–3 wks):** parse+IR+module graph, resolver core with bounds/memo,
sink engine §9.1–9.4, openai-agents pack, ADG build + BOM query, record emission (D fields),
corpus harness with 5 repos. *Exit: correct per-agent BOM on the openai-agents corpus repos and
model attribution on one synthetic gateway repo.*

**M1 — the differentiators:** langgraph+crewai packs, wrapper fixed-point + org registry,
agent-shape F2, LLMCallSite, stable IDs, full-rescan structural diff + materiality pack v1,
governance merge with dual views. *Exit: bespoke corpus recall ≥ target; mutation tests green.*

**M2 — hardening:** adjudication tier + enrichment, config-bundle deref, CycloneDX export,
adversarial corpus, secret redaction, webhook service, incremental prototype behind a flag.

---

## 22. Risks

| Risk | Mitigation |
|---|---|
| Resolver correctness (the moat is also the hard part) | Demand-driven scope; bounds everywhere; `ast` cross-check; corpus in CI; Jedi sampling |
| Dynamic prompts/tools irreducible statically | Template/Symbolic recording + `dynamic` flags; honest `unresolved`; adjudication as bounded fallback; declared-vs-observed deferred to runtime layer later |
| Wrapper fixed-point false positives (utility fns misread as wrappers) | Score threshold + negative signals; registry entries auditable/revocable; adjudication double-check for `external_opaque` only |
| Framework churn | Data-driven rule packs, versioned; registry maintenance is a budgeted recurring task |
| LLM nondeterminism/cost creep | Hard budget, cache, temp 0, confidence floor, abstain-don't-guess, capped confidence, additive-only |
| Shape-score drift (new API styles) | Signals in config; gateway host list per org; corpus adversarial cases |
```
