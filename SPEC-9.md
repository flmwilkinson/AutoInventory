# SPEC-9.md — Update-Resilient Agent Detection & Lean-Up (v9 Build Brief)

**Audience: Claude Code.** Build after SPEC-8. Standards unchanged (SPEC-1 §3): no execution,
deterministic byte-identical artifacts per commit, evidence-bearing facts, `mypy`/`ruff`/pytest/
goldens gates, incremental==full (SPEC-8), one DECISIONS line per item.

## Motivation

Framework packs (F1) match on **exact resolved identity** and extract from **named kwargs**. That
is precise but brittle: if a framework renames its agent class, moves its module, or renames a
kwarg (`model_client` → `model`), the pack silently matches nothing and agents **vanish from the
BOM with no error**. The user's requirement: the graph must survive framework updates. The
bespoke frontend (F2) can't help — it anchors on a *visible* LLM sink, and a framework agent's
call is inside the library.

The fix is an **agnostic fallback anchored on a resolved model, not on surface text** — added to
the loop that already resolves every call site, reusing existing extraction. No new pass, no new
module. This brief also lands a verified lean-up of existing code (an adversarial audit found 15
byte-safe simplifications, several in SPEC-8's own additions).

## Design principle

Two resilience mechanisms, layered under the precise packs — packs still win when present:

1. **Kwarg-drift** is absorbed by **data**: the F1 DSL already lets `extract.arg` be a list tried
   in order (SPEC-8 F1), so a renamed kwarg is a one-line YAML edit, no code.
2. **Class/module rename and unknown frameworks** are absorbed by **one generic fallback** in
   `FrameworkEngine._run_call_rules`, gated on `_extract_model` actually *resolving* a model plus
   an agent-shaped companion kwarg. The **resolved model is the invariant** — it is simultaneously
   the update-resilient signal (a renamed AutoGen still passes `model_client=OpenAIChatCompletion
   Client(...)`, which resolves) and the adversarial guard (the local `Agent("nightly-batch",
   schedule=[...])` has no resolvable model → never emitted, class name notwithstanding). The class
   name is **never** used as a gate — that is the "resolved identity, never surface text" premise
   the engine header (engine.py:1-6) is built on, and name-gating both false-positives (a benign
   `ReportAgent(model=SchemaV2, client=S3Client())`) and misses (`Crew`, `Swarm`, `AgentWorkflow`,
   Vercel `generateText`).

## Part A — Agnostic agent detection (the load-bearing change)

- **A1 — Widen pack arg-tuples (data only, zero code).** In the agent-ctor rules of
  `autogen.yaml`, `llama_index.yaml`, `semantic_kernel.yaml`, `pydantic_ai.yaml` (and review
  `openai_agents.yaml`, `crewai.yaml`), list kwarg alternates so a rename doesn't blank a field:
  `model: {arg: [1, model_client, model, client, llm]}`,
  `instructions: {arg: [system_message, system_prompt, instructions]}`,
  `tools: {arg: [tools, plugins, functions]}`. The class fq still matches; the drifted kwarg is
  recovered. Affects: `frontends/framework/packs/*.yaml`.

- **A2 — Generic resolved-model fallback in `_run_call_rules` (~30–40 lines, one method).** In the
  per-site loop of `FrameworkEngine._run_call_rules` (engine.py:373) the callee `fqs`, `scope`, and
  claimed-status are already in hand. Track whether any pack call-rule fired for the site; if none
  did **and** `fqs` is non-empty, call a new `_match_agent_ctor(site, scope, fqs, result)` that
  **reuses the existing extractors** (no new extraction code — the audit confirmed this):
  - `model = self._extract(ExtractSpec(arg=("model","llm","model_client","service","client",
    "chat_model"), resolve="model_ref"), site, scope, …)` — reuses `_extract_model` (Z10 adapter
    probe: `model/model_name/modelName/ai_model_id`, nested client endpoint, provider-prefixed
    strings). **Refuse bare positional-arg-0** as the model here (PydanticAI's positional model is
    only safe because it is fq-gated; a generic detector reading positional-0 would grab the
    adversarial's `"nightly-batch"` string).
  - **Gate 1 (mandatory):** the emitted `ModelRefF.method != RUNG_UNRESOLVED`
    (`sinks/attribution.py`) — i.e. a model actually *resolved*, not merely a kwarg named `model`
    is present.
  - `tools = self._extract(ExtractSpec(arg=("tools","plugins","functions"), resolve="ref_list"),…)`
    and `instructions = self._extract(ExtractSpec(arg=("system_message","system_prompt",
    "instructions"), resolve="prompt"), …)`.
  - **Gate 2 (mandatory):** at least one of `tools` (non-empty refs) or `instructions` (a resolved
    prompt) is present — a bare model call is a sink (F2/usages), not an agent.
  - On pass, emit `AgentDefF(confidence="medium", method="agent_shape:ctor", kind="bespoke",
    name=_assign_target_name(site) or <class-name fallback>, location/language from the path)` via
    `_agent_id(...)` (inherits id disambiguation + the SPEC-8 incremental id-seeding + route wiring
    for free), plus `BindModelF`/`BindPromptF`/`BindComponentsF` from the extracted facts.
  - Because it only runs on sites **no pack claimed**, that is the F1-exclusion for free (no
    consumed-span bookkeeping); F2 is disjoint (it keys on visible sinks, which a framework ctor
    has none), so there is no double-emit. Confidence `medium` marks it heuristic vs pack-precise
    `high` — honest for a reviewer.
  Affects: `frontends/framework/engine.py`.

- **A3 — No new pass, no new module, no triage signal, no scoring.** Explicitly rejected from the
  earlier plan: a `frontends/bespoke/agent_ctor_shape.py` module (would re-iterate/re-resolve every
  call site — the entrypoint-sweep mistake — and reinvent `_extract_model`/`_agent_id`); a `*Agent`
  name gate; a scoring/threshold system; and a generic `*Agent`-import triage signal (triage
  already fires `sdk_import` for AI packages and only gates skip-or-not — a name signal adds
  surface-text risk and detects nothing). The `ai_model_id` model-probe addition (SPEC-8) already
  covers the probe widening.

## Part B — Verified lean-up (audit `safe` findings only)

Each is behavior-preserving (byte-identical goldens) and adversarially verified; apply with the
stated guard, then run the full golden suite to confirm no re-bless is needed.

- **B1 (resolve) — delete dead `click_params`.** `_resolve_name` (resolve/engine.py:530-531) is
  unreachable: every click param already lands in `env` (1032-1035), so line 528 returns first.
  Delete the branch, the `Scope.click_params` field (144), and the `click_params=click` kwarg
  (1059); keep `click = _click_params(fn)` (1028) and the `elif p.name in click` binding. `Scope`
  is runtime-only (never serialized) → byte-identical.
- **B2 (sinks) — delete dead `_single_of`** (sinks/engine.py:1122): zero call sites; body is
  identical to the used `_single` in the framework engine.
- **B3 (sinks) — hoist `_ctor_kwarg_vals`** (sinks/engine.py:863) which is byte-identical to
  framework `_ctor_arg` (engine.py:1462). **Guard:** put the shared free function in the **sinks
  layer** (or on the IR `ClassInstance` node) — NOT `frontends/common` (framework imports sinks,
  not vice-versa; the reverse inverts the layer and cycles). Framework calls the sinks helper.
- **B4 (frontends) — consolidate leaf IR helpers** `_subtree`, `_root_name`, `_derived_names`
  duplicated between `wrappers.py` and `agent_shape.py` into `frontends/common.py` (list-form
  `_derived_names(fn, sink_calls: list[CallE])`; wrappers passes `[sink.site.call]`).
- **B5 (frontends) — unify `_value_fqs` (wrappers) and `_fqs_of` (engine)** into one
  `value_fqs(v, path)` in `common.py`; `_fqs_of` becomes a comprehension over it. Bonus: this is
  the reusable value→identity mapping A2 wants.
- **B6 (derive) — de-duplicate finding severity.** Severity is in both the `SEVERITY` map and
  inline `severity=` at `_derived_findings` (derive/engine.py). **Guard:** keep the 5 derived kinds
  in the map (they become the single source), remove only `unanalysed_language_code` and
  `declared_agent_artefact` (conditional severity, stay inline); grade over the combined set once.
- **B7 (derive) — `provider_class` recompute** at engine.py:447 already sits on the derived usage;
  use `u.provider_class.value if u.provider_class else _provider_class(u.endpoint, registry)`
  (1-line tidy, negligible).
- **B8 (incremental — SPEC-8's own cruft) — drop the dead `modules_by_name` param** threaded
  through `depgraph.build_dependency_edges`/`affected_modules`/`_lazy_reexport_packages` and the
  unused `ModuleIR` import; update the `cli.py` call sites. Covered by the incremental-equivalence
  tests.
- **B9 (report — SPEC-8's own cruft) — extract the location-grouping loop** duplicated verbatim
  between `_agents_section` and the `_tools_section` added in SPEC-8 into
  `_by_location(heading, items, render_item)`. **Guard:** preserve byte output — summary text
  `{esc(loc)} <noun> (n)`, the `<=20` open-threshold on the section's own count, and card order.
- **B10 (report) — extract the soft-qualifier-chip rendering** repeated 3× into one helper with
  the `isinstance(quals, list)` guard so the `_models_section` call keeps its `else ""`.
- **B11 (optional, cosmetic) —** `_IncrementalPlan` → `@dataclass(frozen=True, slots=True)`;
  `_named_arg`/`_kwarg` string branch → call the shared `_arg_expr` string case. Low priority.

**Explicitly left as-is (audit `risky` / `reject` — do NOT "simplify"):** the
`_new_function_scope` param loop is the fill-every-param safety net (soundness); the parallel
HTTP-call classifiers in `engine.py` vs `side_effects.py` (share only the `_HTTP_INSTANCE_ROOTS`
constant at most — the sets drift for real reasons); `_CHAT_MODEL_CLASSES` encodes per-class
api_style/provider/endpoint/deployment and must not collapse into the generic adapter branch; the
wrapper vs agent-shape loop predicate is order-sensitive (merging risks mis-classification); the
system-block `has_unapproved_endpoint`/`has_unresolved_models` scans are self-contained; `val` vs
`plain_value` intentionally separate annex vs jargon-guarded record vocabularies.

## Acceptance

1. **Adversarial guard first.** `tests/fixtures/adversarial_agent_name` stays undetected, plus a
   new negative fixture `ReportAgent(model=SchemaV2, client=S3Client())` (name + `*Client` +
   non-model `model=`) must NOT emit an agent — locks the resolved-model gate before A2 lands.
2. **Renamed-framework simulation.** Take the AutoGen fixture, rename the class + move the module +
   rename the kwargs, **delete the pack**, and assert the agent is still detected by A2 at
   `confidence=medium` with its model resolved. Repeat for a `Crew`/`Swarm`/`Workflow`-named class
   (proves name-independence).
3. **Packs still win.** On the four framework fixtures the pack emits `high` and A2 stays dormant
   (no duplicate agent).
4. **No regressions.** Full golden suite byte-unchanged after Part B (no re-bless); the four new
   pack tests + incremental-equivalence tests green.
5. **Scale.** Rescan `openai-agents-python`: A2 adds **zero** false agents (its own `Agent()` sites
   are pack-claimed), and the SPEC-8 incremental==full byte-equivalence still holds.
6. Gates: `mypy`, `ruff`, determinism, one DECISIONS line per item (A1–A3, B1–B11).

## Sequencing

Part B first (pure lean-up, shrinks surface, no behavior change) → then A1 (YAML) → then the two
adversarial/rename fixtures → then A2 (the fallback) → verify at scale. Doing B5 (unify value→fq)
before A2 gives the detector its identity helper for free.
