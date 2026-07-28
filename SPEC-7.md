# SPEC-7.md — Dataflow Completeness & Public-Repo Verification (v7 Build Brief)

**Audience: Claude Code.** Build after SPEC-6. Standards unchanged (SPEC-1 §3). The DocGen
deep review traced every residual gap to *general* dataflow mechanics, not repo quirks —
each Z-item below fixes a construct class that recurs in any enterprise codebase.

## Z-items (all general-purpose)

- **Z1 — Caller-argument binding for agent anchors.** An anchor whose prompt/tools/model
  arrive as *parameters* re-resolves them by binding arguments from its call sites
  (≤5 sites, union per param, existing budgets). Recovers the `orchestrator(prompt,
  tools)` idiom (DocGen `generateWithToolsNoStream`).
- **Z2 — Union-tolerant extraction + string `+=`.** (a) Resolver: `x += <str/template>`
  folds into Template concat (only list-append existed; string `+=` degraded to Top).
  (b) Sinks: extraction points that required single-value sets (`payload`, `messages`,
  prompt content) pick the best matching member of a union deterministically —
  SPEC-5 X0 made unions the common case; every consumer must eat them. A prompt chosen
  from a union is marked `dynamic`.
- **Z3 — Object-merge and member-access dataflow.** (a) Dict-literal **spreads merge**
  resolvable inner dicts (`{...defaults, ...overrides}` — later wins, unresolvable
  spread ⇒ `open`); (b) property access over a `DictVal` resolves (JS `cfg.model` is
  `AttrE`, not subscript); (c) sinks in class methods get a **def-site `this`/`self`
  instance** of their owning class, so ctor-assigned defaults resolve (supersedes the
  P2 "self unbound" trim; unbound ctor args stay honestly unknown).
- **Z4 — Known-unknown tools finding.** When an agent's payload carries `tools:` but no
  definitions resolved, emit `unresolved_tools` (medium) and the card says *"declared
  in model call — definitions not resolved from code"*, never "none detected".
- **Z5 — Qualifier chip dedupe** by (value, source) across env keys.
- **Z6 — Wrapper-registry invalidation** on scanner version change (stale classifications
  must not outlive detector improvements).
- **Z7 — `dormant_ai_wrapper` finding (info):** wrapper-shaped defs with sinks and zero
  in-repo call sites (an unused AI gateway library is inventory-relevant).

## Acceptance

1. DocGen: `ModelGateway` rows show `gpt-4o (code default, env-configurable) + unknown`;
   `draft` carries its template prompt (dynamic); `generatewithtoolsnostream` carries the
   3 tools + its caller-built prompt; no agent shows "none detected" where a payload
   declared tools; `packages/tools` surfaces as dormant.
2. **Public-repo verification sweep** (workflow, ≥5 famous agent repos — gpt-researcher,
   openai/swarm, babyagi, crewAI, OpenHands): each scan ground-truthed against its clone
   by independent reviewers; confirmed scanner defects fixed and re-scanned.
3. Gates: `mypy --strict`, `ruff`, full pytest, determinism, goldens value-improvement
   re-bless only, DECISIONS per Z-item.
