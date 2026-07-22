# Phase 6 — Duplication & Dead Code Audit

Forensic pass over `/home/user/Chemclaw3` looking for near-duplicate solutions (likely
from independently-merged branches), unreachable / unused code, unused dependencies, and
dead feature flags. **No files were modified.**

`vulture` is **not installed** in this environment (`uv run vulture` → "Failed to spawn"),
and it is not a declared dev dependency, so the dead-code findings below are from manual
cross-referencing with `grep`/`ripgrep`, not an automated reachability tool. `ruff` (with
`F`/F401) already passes, so single-module unused imports are not repeated here; the focus
is cross-module unused *exports* and unused *dependencies*, which ruff does not catch.

---

## 1. Verdict summary

| Suspect pair | Verdict |
|---|---|
| `eln/registry.py` **vs** `sources/registry.py` | **GENUINE DUPLICATION** — incomplete F7 migration; two live registries of the same ELN adapters, with a real behavioral divergence. **Primary finding.** |
| `calc/store.py` vs `calc/postgres_store.py` | Not duplication — interface + in-memory backend vs Postgres backend of the *same* `ResultStore`. Clean split. |
| ELN adapters vs `sources/` seam DTOs/protocols | Not duplication — `sources/base.py` *re-exports* `ElnAdapter`/`SourceRetriever` verbatim; deliberate composition. |
| `chemclaw/ids.py` vs `memory/ids.py` | Not duplication — `memory/ids.py` is a 1-line wrapper that *calls* `chemclaw.ids.stable_hash`. Good DRY. |
| retrievers (`report/retrievers.py`, `memory/similarity.py`, `mcp_servers/fpstore.py`, `molfp/search.py`, `rxnfp/search.py`) | Not duplication — one generic `FingerprintStore` (Rule-of-Three extraction) with thin domain wrappers; `memory/similarity.py` reuses `tanimoto`. Textbook shared interface. |
| `evals/metric.py` vs `evals/metrics.py` | Not duplication — singular = interface+registry, plural = concrete registered functions. Documented split; the naming is the only footgun. |
| `agents/audit.py` vs `agents/audit_store.py` | Not duplication — hot-path middleware (no DB dep) vs optional Postgres sink backend of one `AuditSink`. Deliberate split. |
| `calc/xtb.py` vs `calc/xtb_engine.py` | Not duplication — calculator entry point vs shared geometry/energy primitives (also used by `calc/pka.py`). |
| `eln/note.py` vs `kg/note.py` | Not duplication — `kg/note.py` is the `Note` schema/parser; `eln/note.py` is a mapper that *builds* a `Note`. |
| Config loaders | Single `chemclaw/config.py` (`BaseSettings`). No duplicate config. |

**Dead / unused, confirmed:**
- `make_eln_adapter()` — used only by tests (production caller removed by the F7 migration).
- `agents/identity/obo.py::exchange_obo()` — zero non-test callers; **intentionally dormant** (documented deferral), has tests.
- `settings.eln_sync_adapter` — semantically stale: now used only as a cursor *label*, no longer selects an adapter, and its docstring is wrong.

**Dependency hygiene:**
- `httpx` is **imported by 4 first-party modules but not declared** in `pyproject.toml` (relies on a transitive pin).
- All *declared* runtime deps are actually used (incl. `mcp`, `uvicorn` — see §2).

---

## 2. Near-duplicate: the two ELN registries (PRIMARY FINDING)

The F7 "generic `DataSource` seam" (`sources/registry.py`) was built to **generalize**
`eln/registry.py` (per `docs/implementation-tickets.md:586` and `DECISIONS.md`). The
migration was **left half-done**: the ELN *sync* moved onto the new seam, but the *memory
jobs* were not migrated, so **both registries are live at once** and each enumerates the
same two adapters through a different key namespace.

Two parallel registries of the same objects:

- `eln/registry.py:20` — `ELN_ADAPTERS = {"json": JsonExportAdapter, "ord": OrdJsonAdapter}`
- `sources/registry.py:21` — `DATA_SOURCES = {"graph": …, "eln-json": JsonExportAdapter, "eln-ord": OrdJsonAdapter}`

Live consumers, split across the two:

- `workflows/eln_sync.py:24,59` → `active_ingest_sources()` (new seam, config-driven).
- `agents/research_tools.py:23,36` → `active_retrieve_sources()` (new seam).
- `workflows/memory_jobs.py:18,41` → `all_eln_adapters()` (**old** registry, **not** config-driven).

### 2a. This is not merely cosmetic — it causes a real behavioral divergence

With the **default** config `data_sources = "graph,eln-json"` (`chemclaw/config.py`,
`.env.example:216`):

- `active_ingest_sources()` (the durable sync, `workflows/eln_sync.py:59`) resolves to
  **`[JsonExportAdapter]` only** — so the knowledge graph is fed **JSON exports only**.
- `all_eln_adapters()` (memory synthesis, `workflows/memory_jobs.py:41`) hardcodes
  **both `JsonExportAdapter` and `OrdJsonAdapter`** (`eln/registry.py:39-45`), ignoring
  `data_sources` entirely.

So the corpus the memory jobs reason over (json **+** ord) **disagrees with** the corpus
written to the graph (json only). Turning a source on/off via `CHEMCLAW_DATA_SOURCES`
silently does **not** affect memory synthesis — the exact "edit config, not code" property
the F7 seam was supposed to guarantee (`docs/foundation-plan.md:294`) is broken for one of
the two consumers.

### 2b. Recommendation — canonical: `sources/registry.py`

Finish the migration and delete the old registry:

1. Point `workflows/memory_jobs.py::_all_reactions` at `active_ingest_sources()` (the
   ingest halves) instead of `all_eln_adapters()`. If memory intentionally wants *all*
   registered sources regardless of the active-set config, make that explicit with a
   `all_ingest_sources()` helper on the **new** registry — do not keep a second registry
   alive to express it.
2. Then `eln/registry.py` (`ELN_ADAPTERS`, `make_eln_adapter`, `all_eln_adapters`) has no
   production caller and should be removed; migrate `tests/test_eln.py:503-511` to the
   `sources.registry` equivalents.

Why `sources/registry.py` is canonical: it is the newer, deliberately-generalized seam
(`sources/base.py` composes the *existing* protocols verbatim — no reinvention), it is the
one two of the three consumers already use, and it is the one the config (`data_sources`)
and docs (F7) treat as the source of truth. `eln/registry.py` is the superseded ancestor.

---

## 3. Dead / abandoned config: `eln_sync_adapter`

`chemclaw/config.py:440` `eln_sync_adapter: str = "json"`.

Its docstring (`config.py:436-439`) still claims it selects *"Which registered ELN adapter
the durable sync ingests from (a key of `eln.registry`'s `ELN_ADAPTERS`)"*. That is **no
longer true**. Its only reader is `workflows/eln_sync.py:90`, where it is passed to
`load_sync_cursor`/`store_sync_cursor` purely as the **cursor key string** — the actual
ingest source list now comes from `active_ingest_sources()` (i.e. `data_sources`).

Consequence: with `data_sources="graph,eln-ord"` but `eln_sync_adapter="json"` (defaults),
the sync ingests **ORD** entries while persisting its high-water mark under the cursor
label **`"json"`** — a confusing but not corrupting mismatch. This is abandoned-flag drift
from the same incomplete F7 merge.

**Recommendation:** either derive the cursor key from the active ingest set, or rename the
knob to something like `eln_sync_cursor_key` and rewrite its docstring. Do not leave a
config field whose documented meaning contradicts its use.

---

## 4. Dead / dormant code

### 4a. `make_eln_adapter()` — dead in production
`eln/registry.py:26`. Only callers are `tests/test_eln.py:503-506`. Its production role
(selecting the sync's adapter from `eln_sync_adapter`) was removed by the F7 migration.
Falls out with the §2 cleanup.

### 4b. `exchange_obo()` — dormant, intentional
`agents/identity/obo.py:27`. **Zero** non-docstring callers anywhere
(`grep exchange_obo` hits only its own def and two doc comments). This is **not a defect**:
the module docstring and `chemclaw/config.py:342` document it as consciously dormant ("A
source opts in later by calling `exchange_obo`"), it is gated behind
`entra_obo_enabled` (default `False`, `config.py:343`), and it has 3 tests
(`tests/test_obo.py`). Flagging for completeness — matches the "wired, dormant" OBO the
CLAUDE.md/docs describe. **Keep**, but it is genuinely unreachable until the deferred
Snowflake connector lands; track it in `DEFERRED.md` so it is not mistaken for live code.

### 4c. No `if False` / impossible branches found
No literal `if False:`, no config flag that is set nowhere and read in a branch (the OBO
flag in 4b is the closest, and it is legitimately env-overridable).

---

## 5. Dependencies (`pyproject.toml` vs actual imports)

Cross-checked every declared runtime dependency against real imports:

| Dependency | Imported? | Notes |
|---|---|---|
| `agent-framework-*` | ✅ 16 files | `agent_framework` |
| `bofire[optimization,cheminfo]` | ✅ | `bo/engine.py` only — single caller, but it is the whole BO engine. |
| `drfp` | ✅ 4 files | |
| `fastapi` | ✅ 4 files | |
| `mcp` | ✅ | `from mcp.server.fastmcp import FastMCP` in `mcp_servers/{molfp,rxnfp}/server.py`. (An earlier grep that excluded the `mcp_servers/` path falsely showed zero — the SDK **is** used.) |
| `networkx` | ✅ 3 files | |
| `numpy` / `pandas` | ✅ | |
| `psycopg[binary]` | ✅ 7 files | |
| `pydantic-settings` | ✅ | `chemclaw/config.py` |
| `pyjwt[crypto]` | ✅ | `import jwt`, 2 files |
| `python-frontmatter` | ✅ | `import frontmatter`, 4 files |
| `pyyaml` | ✅ | |
| `rdkit` | ✅ 7 files | |
| `scikit-learn` | ✅ | `bo/benchmarks/reizman_suzuki.py:19` (`RandomForestRegressor`) — single caller. |
| `sse-starlette` | ✅ | |
| `tblite` | ✅ | `calc/xtb_engine.py` |
| `temporalio[pydantic]` | ✅ 28 files | |
| `uvicorn` | ✅ (runtime, not import) | Launched as `uvicorn service.app:create_app --factory` in `deploy/entrypoint.sh:13`, `README.md:30`, `deploy/README.md:10`. Correctly declared even though never `import`ed. |

**No unused declared dependencies.**

### 5a. Undeclared dependency: `httpx`
`httpx` is imported by first-party production code —
`agents/identity/obo.py`, `agents/identity/workload.py`, `agents/llm_provider.py`,
`workflows/hpc/nextflow.py` (plus 3 tests) — but is **not** listed in `pyproject.toml`
`dependencies`. It currently resolves only as a transitive dependency (of
`fastapi`/`agent-framework`/`temporalio`). This is a latent reproducibility hazard: a future
bump that drops the transitive `httpx` would break identity, the LLM provider, and the
Nextflow adapter. **Recommend adding `httpx` as an explicit runtime dependency.**

---

## 6. Confirmed NON-duplicates (legitimate shared-interface patterns)

Documenting these so a later pass does not "de-duplicate" good DRY:

- **`mcp_servers/fpstore.py`** is the single generic `FingerprintStore` (Tanimoto ranking +
  in-memory & Postgres backends). `molfp/search.py` and `rxnfp/search.py` are thin domain
  wrappers that only supply the fingerprint function; `memory/similarity.py` reuses the
  shared `tanimoto` for clustering (a *different* operation than top-k search);
  `report/retrievers.py` consumes the store behind the `SourceRetriever` interface. This is
  the intended one-retriever-interface pattern from CLAUDE.md — **not** duplication.
- **`chemclaw/ids.py` ↔ `memory/ids.py`** — the latter (`stable_id`) is a documented
  wrapper over the former (`stable_hash`); the docstring in `chemclaw/ids.py` explicitly
  records that four near-identical hashers were already consolidated here. Good.
- **`calc/store.py` ↔ `calc/postgres_store.py`** — one `ResultStore` protocol +
  `InMemoryStore` in the first, `PostgresStore` backend in the second. Note a minor
  *stylistic* inconsistency with `fpstore.py`, which keeps both backends in one file; both
  choices are fine, neither is duplication.
- **`sources/base.py`** re-exports `ElnAdapter`/`RawEntry`/`SourceRetriever`/`EvidenceChunk`
  from `eln.adapter` and `report.evidence` verbatim (`sources/base.py:16-25`) — composition
  of existing contracts, the opposite of reinventing them.
- **`eln/json_adapter.py` ↔ `eln/ord_adapter.py`** — two genuinely different ELN formats
  (free-text prose vs structured ORD). They share only the `ElnAdapter` contract, not code;
  this is one-adapter-per-source by design (`eln/adapter.py` docstring, `DEFERRED.md`).
- **`evals/{metric,metrics}.py`, `evals/{ab,harness}.py`** — interface/registry vs concrete
  metrics vs A/B comparison vs runner: four distinct responsibilities. The only risk is the
  singular/plural `metric`/`metrics` filename collision, which both module docstrings call
  out — a readability hazard, not duplication.

---

## 7. Ambiguities flagged (not guessed)

- **memory jobs intent (§2a):** it is unclear whether memory synthesis is *supposed* to read
  every registered source regardless of `data_sources`, or whether feeding it json+ord while
  the graph gets json-only is an accidental artifact of the un-migrated merge. The code and
  docs disagree, so this needs an owner decision before the §2 cleanup picks a behavior. I
  did not assume one.
- **`fpstore.py` two-backends-in-one-file vs `calc`'s split-file** convention: harmless, but
  if the repo wants one convention, that is a style call for the maintainers, not a defect.

---

## 8. Prioritized actions

1. **P1** Finish the F7 migration: repoint `workflows/memory_jobs.py` at the `sources`
   registry, resolve the json-vs-json+ord divergence (§2a), then delete `eln/registry.py`
   and migrate its tests. *(Correctness bug, not just tidiness.)*
2. **P2** Fix or rename `eln_sync_adapter` and correct its docstring (§3).
3. **P2** Add explicit `httpx` to `pyproject.toml` dependencies (§5a).
4. **P3** Record `exchange_obo`/`make_eln_adapter` status so dormant vs dead is unambiguous
   to the next reader (§4).
5. **P3 (tooling)** `vulture` could not be run here — add it to the `dev` group so this class
   of finding is caught in CI rather than by hand.
