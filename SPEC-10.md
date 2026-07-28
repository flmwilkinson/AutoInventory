# SPEC-10 — Scalability seams: embeddable core, pluggable source & storage

**Status:** in progress · **Depends on:** SPEC-8 (incremental), SPEC-9 (agnostic detection)
**Goal:** turn the CLI-that-writes-`aiscan-out/` into an **embeddable core** that a future
GitHub-connected, PR-triggered, ephemeral-clone, multi-repo service can call as a library —
**without building that service**. No Docker, no DB server, no queue, no webhooks. The whole
target reduces to **two seams + one extraction**, each landing behind a byte-identical default
so today's CLI and the existing golden/incremental suite stay green.

## Design invariants (must survive every change)
- Static only, no code execution; **deterministic per commit** (same commit → byte-identical `record.json` after canonicalisation).
- **Whole-program resolution**: the Resolver/ModuleGraph always see the FULL current file set; incremental only limits which constructs *emit facts*, never what the resolver sees. A `SourceProvider` MUST materialise the complete head tree (file-count sanity check), never just a diff.
- **SPEC-8 soundness**: `affected = changed ∪ reverse-closure(changed)`; graph build / `derive_record` / `emit` always run GLOBALLY over the full merged fact set; byte-identical to a full scan; `ANALYSIS_VERSION` gate. Recommendations only relocate WHERE carried facts are fetched — never the aggregation.

## The three seams
1. **Source seam (front door)** — `SourceProvider` → `FetchedSource` exposing `.repo_root: Path` (a per-scan dir) + `changed_files(base, head)` + `cleanup()`. Implementations: `LocalDirProvider` (today, unchanged) and `EphemeralCloneProvider` (temp clone, scan, `rmtree`). Keeping `repo_root` a `Path` means the ~13 `repo_root/rel` reads, `hash_files`, and `git diff` are untouched — blast radius is `ingest()` + two `cli.py` call sites.
2. **Storage seam (back door)** — one `StateStore` keyed by `(repo, commit)`: `get/put_manifest`, `has_record`, `get_prior_facts(repo, base)`, `put_artifacts`. Default `LocalDirStore` = byte-for-byte today's layout. The pure SPEC-8 core (depgraph, gate, factcache partitioners) is already storage-agnostic — only ~5 I/O call sites move. **One seam, two projections**: the record blob and the relational index share the `(repo,commit)` key and one backend handle (LocalDir now → SQLite soon → Postgres later), not two parallel stores.
3. **Embeddable core (the pivot)** — `core.scan(ScanRequest) -> ScanResult` RETURNS the live `Record`/`Graph`/fact-lines it already builds instead of writing to disk and re-reading. `cli.py` keeps ingest/out-dir/manifest/log/dotenv setup and becomes a thin adapter that persists via `LocalDirStore` and renders HTML on demand.

## NOW — 5 changes (zero new infrastructure, all golden-validated)

**5a. Determinism-on-write (prerequisite).** `record.json` as written is NOT byte-stable —
`emit.py:360` stamps `scanned_at = datetime.now(UTC)`, embeds wall-clock `scan_health.stage_ms`,
and an absolute `org_pack` path; the normalisation lives ONLY in `tests/harness.py:normalize_record`.
Fix: extract that field-stripping into a production `aiscan/inventory/canonical.py::identity_record(data)`;
the `StateStore` hashes THAT for `(repo,commit)` change-detection; the harness imports it (kills the
prod/test drift). Keep human-facing `scanned_at` real (non-identity metadata). Write `org_pack` as a
basename ref, not an absolute path (removes a determinism break + a path leak). Goldens unaffected
(compare re-normalises idempotently → no re-bless).

**5b. Canonical `(repo,commit)` identity.** Key by `owner/repo` slug, not URL basename
(`git.py:55` — else `org-a/api` and `org-b/api` collide in cache/output). The wall-clock `scanned_at`
in the dataset scan-id hash (`flatten.py:36`) is deferred to **SOON-K**, where the write-through upsert
actually needs an idempotent `(bundle_id, commit)` key: dropping it now would collapse the 4-fixture
dataset corpus (all `repo:repo|unversioned`, distinguished today only by wall-clock) — in the current
drop+rebuild model scan-id-with-wall-clock is still stable per on-disk record, so there is no
determinism bug to fix here. The record.json blob determinism (the critique's real concern) is 5a.

**1. Extract `core.scan(ScanRequest) -> ScanResult`.** Move `_run_pipeline`/`_plan_incremental`
(`cli.py:446-729`) into `aiscan/core.py`; change only the terminal step — return
`ScanResult(record, graph, fact_lines, analysis_finding_lines, entrypoint_marks, health)` (all objects
already live at `cli.py:658-721`) instead of `write_artifacts`. The two in-pipeline LLM caches
(`adjudication_cache.json` `cli.py:632`, `enrichment_cache.json` `cli.py:698`) become request-provided
handles defaulting to in-memory. Split HTML rendering out of `write_record_artifacts` so the core
persists nothing. `cli.py` becomes the adapter. No analysis logic changes → byte-identity untouched,
and it becomes testable in-memory.

**2. Ephemeral `SourceProvider`.** Ship `LocalDirProvider` (unchanged) + `EphemeralCloneProvider`:
`git init` a temp dir, **clone blobless (`--filter=blob:none`, full history — NOT `--depth`, which
truncates the `git log` history `owner.py:35,70` derives provenance from)**, fetch BOTH base and head,
checkout head, `changed_files` reused verbatim, scan, `rmtree` in a `finally`. Delete `_clones/`
persistence + reuse. Assert the provider materialises the full head tree.

**3. `StateStore` seam.** Route manifest read/write, fact-cache read, and artifact write through one
protocol keyed by `(repo, commit)`. Split `load_cached_analysis` (`factcache.py:114`) into a pure
`jsonl→CachedAnalysis` parser + a store fetch. Drop the absolute `scan_out_dir` from `Manifest` —
prior facts addressed by `(repo, base_commit)`. `LocalDirStore` reproduces today's layout byte-for-byte.

**4. No process-global state on the core path.** Credentials come in on `ScanRequest` (core never reads
`os.environ`; the `call_fn` injection seam `cli.py:127` already exists). Per-scan logger instead of the
shared `getLogger('aiscan')` singleton (two concurrent scans rip out each other's handlers + cross-write
`scan.log`). `load_dotenv`/`Settings` env reads stay in the CLI adapter only.

## SOON — 6 changes (still no hosting; SQLite is stdlib)
- **G. Typed derived core + `schema_version` + one `JsonRepr` decoder.** Add `schema_version` to `Record`; emit `record.schema.json` from `Record.model_json_schema()`. Promote `models_used`/derived indicators from `JsonRepr`/`list[object]` to real sub-models; unify the four independent `JsonRepr` parsers (`report/inventory.py`, `derive/inventory.py`, `report/graph_svg.py`) into one documented typed decoder. Persist the two view-only derivations so HTML is a dumb view.
- **H. Source VFS conversion.** Behind the provider, a narrow `Source` read interface (`list_files()` sorted, `read_text/exists/size`); convert the ~13 `repo_root/rel` reads + ~8 dir-walks (`cli.py:96`, `ingest/triage.py`, `modules/graph.py`, `modules/ts_graph.py`, `incremental/gate.py:43`) to it; delete the duplicated skip-dir sets. Analysis core needs zero changes (parser takes `source: str`). Land AFTER the provider proves byte-identical.
- **I. PR-native base selection.** `base = merge-base(base_branch_tip, head)`; fetch prior facts by `(repo, base_sha)`. Consider three-dot `base...head` so changes already on main aren't re-flagged.
- **J. Org wrapper registry into the store at ORG scope.** Replace `org_registry.json` (`wrappers.py:161`) with a store-backed org-scoped table, versioned upsert + read-snapshot. **This is the sleeper hazard:** it's cross-scan state in the incremental READ path that does NOT fail safe (a torn registry changes attribution → facts → byte-identity) and is the sole reason the fleet is sequential. Must be transactional BEFORE any concurrency.
- **K. Write-through `(repo,commit)` SQLite dataset.** Replace `dataset/store.py`'s drop+rebuild-by-disk-walk (`store.py:89`) with `INSERT OR REPLACE` into a persistent single-file SQLite (WAL), record.json as a JSON blob column (the byte-identity anchor) + flatten rows as the index. Add `mcp_servers`/`model_usages` tables + a `diff(base, head)` query. Keep `rebuild_dataset` as recovery.
- **L. Deterministic per-scan scaling guards.** Per-repo size budget (max files/LOC/bytes) that degrades with a `scan_health` flag instead of OOMing; cap abstract-value union cardinality + `ListVal`/`DictVal` width/depth (collapse to Top deterministically); cap/evict the resolver memo; extend the `scan_budget_s` deadline past `resolve()` to cover parse/wrapper-fixpoint/graph-build. All behaviour changes gated under `ANALYSIS_VERSION`.

## LATER — needs actual hosting (out of scope here)
`GitHubApiProvider` (tree/blob + App tokens); Postgres + object-store backends; org enumeration +
concurrent workers (blocked on J); CycloneDX 1.6 export as a view; the webhook/API service + any container.

## Verification
Every NOW/SOON change is acceptance-gated on the existing byte-identical golden suite + SPEC-8
incremental-equivalence harness (no re-bless expected — a re-bless means a real regression). The
`EphemeralCloneProvider` gets a byte-identity acceptance test that includes `ai_commit_range` +
`contributor_candidates` (the blobless-vs-shallow trap). `ruff`/`mypy` clean throughout.
