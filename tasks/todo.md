# Close the step boundary: make a computed geometry addressable

The review (published 2026-08-21) found that every agent-drivable path between two calculation
steps routes its data through the model's token stream, and that the three content-addressed
handles the system already derives — `structure_id`, `calc_ref`, `artifact_ref` — are all
**write-only** from the agent's side. This is the implementation.

## What the second dig changed (read this before the list)

Five things the review did not have, found by reading `Chemclaw3-mcp`'s calc server and by
measuring one more tool:

1. **The server already has the whole geometry-taking primitive set** — `relax_structure`,
   `compute_properties_at`, `compute_hessian`, `scan_point`, `search_conformer_ensemble`,
   `search_binding_modes`, `combine_structures`. Nothing needs building there for the xTB half.
2. **There is no `compute_fukui_at`.** So `predict_site_reactivity` cannot take a geometry, and
   pretending otherwise would be a cross-repo change I cannot verify. It is left out and recorded.
3. **`calculation_key` already returns `structure_id`** for geometry-keyed calculations, and
   `remote_key` drops it. That is the server's authoritative address, free on the hit path.
4. **The server's `Structure.structure_id` is a `computed_field`; ours is a plain `@property`** —
   so the authoritative address arrives on every payload and is silently discarded. Worse, the
   server rounds by `settings.xtb_geometry_decimals` (ENV-overridable) while we froze the constant
   at 4, so the "changing it is a cross-repository change" claim in `science/calc/models.py` holds
   on our side only. A divergence raises nowhere and every lookup misses forever.
5. **`find_calculations` is the worst instance of the payload problem, not `sample_conformers`.**
   Measured: one stored `xtb.conformers` row is 66,520 characters, and `calc_find_max_results`
   is 50 — **~830,000 tokens** for one read-only call available on two profiles. That is past
   every provider's context limit, where `compaction.py` says the failure is hard rather than
   graceful. The review missed it.

The QM/DFT half of the chain does **not** ship enabled: the Nextflow pipeline's params contract
(`params.smiles`) lives on a cluster this repository cannot reach, and a `geometry` param a
pipeline ignores would silently run DFT on a fresh embedding while telling a chemist it ran on
their conformer. It ships behind `hpc_pipeline_accepts_geometry`, default off, refusing loudly.

## The list

### A. The store and the projection (F1, F2, F13)
- [x] A1 `infra/sql/047_structures.sql` — content-addressed geometry store + grant row.
- [x] A2 `science/calc/structures.py` — `StructureStore` protocol, in-memory backend, `put`/`get`.
- [x] A3 `science/calc/postgres_structures.py` — the Postgres backend + `default_structure_store()`.
- [x] A4 `science/calc/geometry.py` — one walker: `structures_in()` (persist) and
      `without_geometry()` (the agent view). Generic over a payload, not per model.
- [x] A5 Persist at the five `compose.py` sites where a geometry comes back from the server.
- [x] A6 Project at the three sites that hand a stored payload to the model: `CalcJobWorkflow`'s
      envelope, `completed_job_status`/`_recorded_status`, and `find_calculations`.
- [x] A7 Bound `find_calculations`' per-record result with an honest `result_truncated` flag.
- [x] A8 The divergence check: compare the server's `structure_id` with ours, log + count.

### B. The handle as an argument (F1)
- [x] B1 `optimize_geometry(structure_id=…)`, `compute_thermochemistry(structure_id=…)`,
      `compute_electronic_properties(structure_id=…)` — mutually exclusive with `smiles`.
- [x] B2 `ScanJobSpec`, `EnsembleJobSpec`, `ComplexJobSpec` gain `structure_id` fields.
- [x] B3 `compose.scan_profile` / `conformer_ensemble` / `interaction` accept a resolved structure.
- [x] B4 `QmJobSpec.structure_id` + the `hpc_pipeline_accepts_geometry` gate + the launcher param.

### C. Finding what already exists (F4)
- [x] C1 `structure_id` on `calculation_results` (migration), on `StoredResult`, written from the
      server's own answer in `remote_key`.
- [x] C2 `find_calculations(structure_id=…)`, and the refusal message updated.

### D. The deterministic path (F5, F8)
- [x] D1 `${steps.id.result.a.b}` — dotted path in `templates/resolve.py` + `manifest.py`.
- [x] D2 `_text()` dumps a pydantic model as JSON instead of falling through to its repr.
- [x] D3 `_job_results_message` renders JSON, not a Python repr.

### E. Provenance and identity (F6, F10)
- [x] E1 `ConnectorJobResult.calc_refs`, filled by the calc job from the keys it touched.
- [x] E2 `core/ids.canonical_text`, used by both `_report_id` and `campaign_id_for`.
- [x] E3 Canonicalise the campaign key; `cli/rekey_campaigns.py` backfills existing rows.
- [x] E4 `ExperimentSuggestion.opened_new_campaign` — say so when a fork is likely accidental.

### F. Context recovery (F9)
- [x] F1 A compacted turn resets the repeat guard: an identical call after a clearing is a
      re-read, not a repeat. (The review proposed a tool over `tool_result_blobs`;
      `tests/test_layering.py` forbids `agent → api`, and this is the smaller correct fix.)

### G. The record
- [x] G1 ADR `D-2026-08-21-a-geometry-is-an-address-not-a-payload.md` + ledger row.
- [x] G2 `BACKLOG.md` / `DEFERRED.md` rows: the DFT geometry param, `compute_fukui_at`.
- [x] G3 Package READMEs, `.env.example`, `ARCHITECTURE.md` if a directory moved (it does not).
- [x] G4 Tests for every one of the above.
- [x] G5 `make lint type test` green, with what it skipped stated.

## Review

**Done, and measured on the same molecule the review used** (celecoxib, 40 atoms, 20 members):

| | before | after |
| --- | --- | --- |
| mid-turn resume message | 29,634 ch (~7,408 tokens) | **5,583 ch (~1,395)** |
| distinct numeric values in it (cap 512) | 2,400 | **38** |
| one stored `xtb.conformers` row | 66,523 ch | **10,000 ch** |
| `find_calculations` at `limit=50` | ~831,000 tokens | **bounded at ~50,000** |
| campaign id under re-casing / padding / float noise | forks silently | **same campaign** (8/8 perturbations) |
| the conformer → refine chain | inexpressible in all four modes | one hop in three of them; DFT gated |

**Five things the plan did not anticipate, found while building it.**

1. `find_calculations` needed a bound *and* a projection, and even after the projection a
   47-member row is 10,000 characters — so the per-record ceiling is load-bearing, not belt-and-braces.
2. `structure_id` had to mean **the geometry a calculation ran on**, not the one it produced.
   The first test written asked the other question and failed; the server's own `calculation_key`
   answers the input, and the input is what a chemist holding conformer #3 is asking about.
3. Chaining through a template needed `lowest_structure_id` hoisted onto `ConformerEnsemble` as a
   `computed_field`. Reaching `conformers[0]` would have meant list indexing in the resolver, which
   is the first step toward the expression language `templates/manifest.py` refuses to grow.
4. `tests/test_database_privileges.py` caught the re-key CLI asking for DELETE on `bo_campaigns`
   and UPDATE on `bo_suggestions` — verbs the grant withholds on purpose. The answer was to name
   the module as operator-run, not to widen the runtime role.
5. The projection restated `charge: 0, multiplicity: 1` on every member of every ensemble. A field
   whose value is the default is a field the model reads past; omitting the pair took another 11%.

**One thing deliberately not built.** The review proposed an agent tool over `tool_result_blobs`
for the compaction dead end. `tests/test_layering.py` forbids `agent -> api`, and the smaller fix is
better anyway: a reduction clears the repeat counters at the one place that can see one happen.

**What ships off, and why.** DFT on a chosen conformer is built and refused behind
`hpc_pipeline_accepts_geometry`; Fukui at a chosen geometry is not built at all. Both are in
`DEFERRED.md` with their triggers, and both are blocked on a contract outside this repository —
a Nextflow pipeline's params, and a `compute_fukui_at` primitive `Chemclaw3-mcp` does not expose.

**Operator note:** `make db-migrate` then `python -m chemclaw.cli.rekey_campaigns` on any deployment
holding recorded BO campaigns. `--dry-run` reports what would move.
