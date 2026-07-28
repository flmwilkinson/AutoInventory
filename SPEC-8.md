# SPEC-8.md — Incremental & Scalable Scanning (v8 Build Brief)

**Audience: Claude Code.** Build after SPEC-7. Standards unchanged (SPEC-1 §3): no
network at scan time beyond `git`, no repo-code execution, deterministic artifacts,
`mypy`/`ruff`/pytest/goldens gates, one DECISIONS line per item.

## Motivation

A full scan is whole-program abstract interpretation. On `openai-agents-python`
(835 files, 302k LOC) it takes ~12–15 min, essentially all of it in two stages:

| stage | time | work |
|---|---|---|
| frontends | ~754 s | agent/wrapper/shape analysis — model/prompt/tool attribution per construct |
| sinks | ~167 s | LLM call-site detection |
| parse | ~7 s | per-file IR |
| modules / emit / triage | <4 s each | cheap |

Two wins, both resting on the scanner's **per-commit determinism** (same commit ⇒
byte-identical `record.json`):

1. **Skip** re-scanning a commit already scanned with the same inputs.
2. **Incrementally** re-analyse only what a diff can change, reusing cached facts for
   the rest.

The hard constraint: an incremental scan must never produce a BOM that differs from a
full scan of the same tree. A missed component in a compliance record is far costlier
than a redundant scan, so every uncertainty **fails safe to a full scan**.

## Core soundness principle

Incremental analysis is sound **iff the re-analysed set is a superset of every module
whose facts could differ between base and head.** Guaranteed by three invariants:

1. **Resolution is always whole-program.** The `Resolver`/`ModuleGraph` are built over
   the full current file set, so any expression resolves across module boundaries
   correctly. `analyze_only` limits only which modules *emit* facts — never what the
   resolver can see.
2. **Affected = changed ∪ reverse-dependency-closure(changed).** A module N that imports
   or calls into a changed module M is re-analysed, so a changed callee *and its
   callers* ("how the diff is then called") are both covered. Edges come from the symbol
   tables' import bindings; the closure is taken over the reverse graph, with
   conservative widening for lazy re-exports and escalation to full for the rest.
3. **Aggregation is always global.** After merging cached + freshly-emitted facts, the
   graph build, `derive_record`, and emit run over the entire merged fact set every
   time. So `models_used` dedup, counts, the MCP list, capability flags, and the ADG are
   always exact — the connections are reassembled globally on every scan.

## I-items

- **I0 — Commit skip + clone reuse.** `ingest/git.py` reuses an existing clone
  (`git fetch` + `checkout --detach`, full clone not `--depth 1` so `git diff base..head`
  works) instead of erroring. A per-bundle manifest (`<out>/_cache/<bundle>.json`)
  records `last_scanned_commit`, `scanner_version`, `analysis_version`,
  `rulepack_versions`, `source_roots`, and content hashes of the dependency manifests /
  tsconfig / org pack. `run_scan` returns the prior output untouched when the commit and
  all of those match. A **dirty local working tree is never skipped or cached** (HEAD
  does not name its content). `--full` forces a from-scratch scan.
  Affects: `ingest/git.py`, `incremental/manifest.py`, `incremental/gate.py`, `cli.py`.

- **I1 — Incremental analysis.** On a diff, compute `changed` modules from
  `git diff --name-only base..head`, take `affected = changed ∪ reverse_closure(changed)`
  over the import-edge graph, and run the sink scan + framework/bespoke frontends with
  `analyze_only=affected` (emission scoped; resolution whole-program). Load the prior
  scan's `facts.jsonl` + `analysis_findings.jsonl`, keep the entries whose module is
  unaffected, merge with the fresh ones, and re-derive the record globally. The bespoke
  stages are driven by the (scoped) sink list, so they need no separate filter.
  Affects: `incremental/{depgraph,factcache,gate}.py`, `cli.py`, `sinks/engine.py`,
  `frontends/framework/engine.py`, `inventory/emit.py`.

- **I2 — Conservative widening & escalation (the safety of I1).** Escalate to a **full
  scan** whenever incrementality cannot be trusted: manifest absent; scanner/analysis/
  rulepack version mismatch; a changed dependency-manifest / lockfile / tsconfig / org
  pack / source-root layout (they alter resolution of *every* module); a **deleted source
  file** (a dangling cross-module reference cannot be reasoned about); or a base commit
  git cannot diff. Widen `affected` for **PEP-562 lazy re-exports** (a changed package
  whose `__init__` defines `__getattr__` — the `agents.mcp` case — invalidates all
  importers of its prefix, since those edges are invisible to static import analysis) and
  keep the edge for `from x import *`.
  Affects: `incremental/gate.py`, `incremental/depgraph.py`.

- **I3 — Cross-module post-passes.** Framework post-passes resolve by agent *name* across
  modules. An incremental run injects the carried cached facts into `F1Result` *before*
  promotion/routes/crews/entrypoints, and `_emit_routes`/`_emit_crews` resolve targets
  against the merged agent set. **Entrypoints** are special: the flag lives on the target
  agent's fact but is set by a `Runner.run` marker in *another* module, and the graph
  embeds it — a stale carried flag corrupts `graph.json`. They are emitted as
  `(framework, agent, source-module)` marks into `F1Result.entrypoint_marks`, persisted
  to `entrypoint_marks.jsonl`, and on incremental the unaffected modules' marks are
  carried from cache and merged with the fresh affected ones; `_apply_entrypoints`
  reset-then-applies the union. Marks and facts are persisted as the **full merged set**,
  so chained incrementals stay complete. (A first attempt re-swept entrypoint calls over
  all modules; `run` is too common a callee tail to prefilter and it cost a full-scan's
  worth of resolution — rejected.) The org wrapper registry is written by full scans
  only; incremental sees a subset of wrappers and reads the complete classification set
  from the registry the seeding scan wrote.
  Affects: `frontends/framework/engine.py`, `inventory/emit.py`,
  `incremental/factcache.py`, `cli.py`.

- **I4 — `ANALYSIS_VERSION` gate.** A single integer bumped on ANY change to analysis
  logic whose output a cached fact could encode (parsers, resolver, sink registry/shape,
  frontend packs/engines, side-effects, derive). A mismatch invalidates every cache and
  forces full scans until the next full scan re-seeds the manifest — a cached fact never
  outlives its detector (precedent: the `org_registry.json` schema int, SPEC-7 Z6).
  Affects: `incremental/gate.py`.

## Cache artifacts (per scan output dir)

- `facts.jsonl` — full merged fact set (already emitted; now also the incremental cache).
- `analysis_findings.jsonl` — raw analysis-stage findings, separate from the graded
  record (derive-generated and secret findings regenerate globally, so are not cached).
- `entrypoint_marks.jsonl` — `[framework, agent, module]` per line.
- `<out>/_cache/<bundle>.json` — the manifest.

## CLI / CI interface

- `--base <sha>` — commit to diff against (defaults to the manifest's last-scanned
  commit; CI passes the pre-push SHA).
- `--full` — force a full scan, ignore any cache.
- Manual runs auto-use the manifest; fleet mode (`--repos`) gets per-repo skip for free.

## Acceptance

1. **Equivalence harness (`tests/test_incremental.py`) — the load-bearing proof.** For a
   battery of diffs (change model, add tool, add agent, unrelated file, cross-module
   handoff, cross-module entrypoint move, prompt change, source deletion→escalate) an
   incremental `base→head` scan produces the **byte-identical record + graph** as a
   from-scratch `--full` scan of head (scan_health excluded — its sink/resolver counters
   reflect only the re-analysed slice; `facts.jsonl` not compared directly — the BOM is
   the record + graph). Plus a **chained** base→mid→head case (both hops incremental) and
   a guard asserting the incremental path actually ran.
2. **Gate units:** version / lockfile / global-signal / deletion changes escalate to
   full; an unchanged commit skips; a dirty tree never skips.
3. **Scale:** full scan of `openai-agents-python` seeds the cache; a one-file commit then
   scans incrementally in seconds (vs ~12 min) with a record matching a full scan of the
   same head.
4. Gates: `mypy`, `ruff`, full pytest, determinism, goldens re-blessed only for additive
   fields, one DECISIONS line per I-item (I0–I4 under `## SPEC-8`).

## Operational guidance & deferred work

- **Run a periodic full scan** even under CI-incremental (e.g. nightly), so any latent
  drift self-heals within a day — belt-and-suspenders for a compliance artifact.
- **Deferred:** multiprocessing across independent module clusters (an orthogonal ~5–6×
  win on multi-core, larger refactor of the shared resolver state — revisit only if a
  full/affected scan is still too slow after I0–I4); persisting the resolver memo across
  scans (span-keyed entries shift when files change — not worth the unsoundness risk).
