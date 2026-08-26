# D-2026-08-26-semiempirical-is-the-whole-tier — the HPC/DFT capability is removed, not deferred

## Status

Accepted. Supersedes the deployment half of **D-010** (HPC/DFT deferred, lead with fast local
calculators) and all of **D-048** (F5: real HPC execution via a Nextflow launcher behind the QM
activities). Closes and deletes three `DEFERRED.md` rows: *HPC/DFT real integration*, *No converged
electronic structure kept* (STO-5), and *DFT on a chosen conformer* (D-2026-08-21).

## Context

The request was direct: remove every HPC-dependent workflow; the calculations this system runs are
semiempirical — GFN2-xTB via tblite, and CREST — and they run in their own pod on OpenShift or
Databricks rather than on a cluster.

Exactly one capability was HPC-dependent, and it was HPC-dependent all the way down. The `qm`
bundle's whole dependency closure was the cluster: a Seqera/Tower launcher (`hpc/nextflow.py`), an
artifact store on a second origin with a second credential, a poll budgeted at 24 hours, and
fourteen `hpc_*` settings whose only readers were that launcher and the validators guarding it. Its
one tool, `compute_dft_energy`, was the only thing in the tree that could not answer without a
cluster — and no deployment has one.

**What the tier was actually costing while it waited.** It is worth being precise, because "built
and awaiting a cluster" reads like a dormant asset:

- Fourteen settings, two derived properties and three cross-section validators existed to make a
  launcher nobody could reach fail correctly. Two of those validators had already been *fixed* for
  defects (`D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured`) found by reading rather
  than by running, because there was nothing to run them against.
- The shipped Helm values selected `hpc_launch_interface: nextflow` and named
  `https://tower.internal/api`, a host that resolves nowhere, plus a `hpcApiToken` secret key an
  operator had to provision for a capability that cannot work.
- `connector_job_timeout_seconds` defaulted to 90,000 s — 25 hours — because the DFT poll needed it.
  Every other connector job in the system inherited a ceiling sized for a tier that never ran.
- The four-repo live lane started a mock HPC launcher (`Chemclaw3_mock`'s `app/hpc/`) so the durable
  path had something to be durable *about*. The mock proved the pattern; the pattern is also proved
  by `calc`, which has real work behind it.
- The one geometry-handoff seam that could not be verified offline was this one, and it shipped
  refusing rather than running (D-2026-08-21). That was the right call and it is also the shape of
  the whole problem: a capability whose contract lives somewhere nobody can look.

## Decision

**Delete the tier.** `src/chemclaw/connectors/qm/` and `src/chemclaw/core/config/hpc.py` are gone,
with the mock launcher in `Chemclaw3_mock`, the `CHEMCLAW_HPC_*` block in `.env.example`, the
`connectors.qm` chart entry and its `hpcApiToken` secret, the live lane's launcher wiring, and the
UI's `compute_dft_energy` entries. `chemclaw3_ui`'s tool tables and the `Chemclaw3-mcp` catalogue
row go with them.

**The calculation tier is semiempirical and complete as it stands.** `connectors/calc` keeps every
tool, every durable job, the D-011 cache, the artifact store and the calibration ledger; the physics
answers from `Chemclaw3-mcp`'s `servers/calc` — its own pod, already addressed over HTTP by
`CHEMCLAW_CALC_SERVER_URL` — whose engines are xtb/tblite and CREST and contain no DFT.
`D-2026-08-16-the-physics-leaves-the-cache-stays` had already put that pod where a Databricks or
OpenShift deployment wants it, so this decision required no new deployment work at all: it is a
removal.

**Three things were kept deliberately, and each is the interesting part.**

1. **The `dft` projector stays** (`publish/project.py`). `calculation_results` is never pruned, so a
   deployment upgrading into this release still holds every `dft` row it ever wrote, and the
   backfill path resolves a stored row by `calc_type` prefix alone. That module's own rule already
   says so — "a retired calculator keeps its projector; a spelling that never existed does not get
   one" — and `xtb.scan` is the same shape from an earlier move. What did *not* survive is the
   `PAYLOAD_PROJECTORS["QMJobResult"]` entry beside it, because that half is keyed by a pydantic
   model name and no live payload can state one that no longer exists.

2. **The parent-ceiling invariant stays, rewritten against the activity that is actually longest.**
   `_the_job_ceiling_covers_the_poll_it_bounds` guarded a real defect — a parent execution timeout
   no larger than one child activity makes the retry policy unreachable and produces a bare
   `WorkflowExecutionTimedOut` naming neither setting. Deleting it with the poll would have left
   `calc` exposed to exactly that, since `xtb_job_timeout_seconds` (4 h) is now the longest thing
   under the ceiling. It is `_the_job_ceiling_covers_the_activity_it_bounds` now, and
   `connector_job_timeout_seconds` is re-derived from the search budget: 18,000 s, the same
   "budget plus an hour" reasoning applied to the job that exists.

3. **`job-result` moves back into core's `KNOWN_NOTE_TYPES`.** It lived in `qm`'s manifest under the
   rule that a type a *bundle mints* belongs to that bundle (D-118). No bundle mints one now — the
   type is written only through core's own PR-gate, about results the corpus in
   `knowledge/job-result/` already holds — so by that same rule it is core's vocabulary again.
   `bo-candidate` stays with `bo`, which still mints it.

**And one control was rewritten rather than dropped.** `test_the_bundle_has_no_way_to_write_the_note_itself`
asserted `not hasattr(qm_knowledge, "write_knowledge_node")` — a control named after one module,
which would have gone dark the moment that module was deleted while still reading as a guard. It is
now an AST walk over every bundle asserting that none imports `kg.pr_gate` or names `propose_note`,
which is strictly stronger and does not depend on which bundles exist. This is the
`map_to_hpc_identity` shape the repository already knows, caught before it happened rather than
after.

## What we are giving up, stated plainly

DFT accuracy. When a decision turns on a difference inside GFN2-xTB's error bar there is no longer a
tier to escalate to, and the honest move is to say so and propose an experiment rather than quote a
number the method cannot support. `docs/guides/xtb-use-cases.md` §4 used to describe an escalation
ladder and now describes that ceiling; the agent instructions say the same thing to the model.

Re-adding a heavy tier is a new decision, not a revert. What this ADR asserts is that carrying an
unreachable one is worse than not having it: the settings, the validators, the chart entry, the
mock and the 25-hour default were all real maintenance paid for a capability that has never
computed a number.

## Consequences

- `CHEMCLAW_QM_ACTIVITY_TIMEOUT_SECONDS` is renamed `CHEMCLAW_ACTIVITY_TIMEOUT_SECONDS` and moved to
  `TemporalSettings`. It was never a QM knob: its three readers are the memory-distillation job, the
  orchestrator's step activities and the session push-back. A deployment setting the old name must
  rename it — `extra="forbid"` makes that a startup error rather than a silent default.
- `CHEMCLAW_CONNECTOR_JOB_TIMEOUT_SECONDS` defaults to 18,000 (was 90,000). A deployment that set it
  explicitly is unaffected; one that did not gets a ceiling sized for the work it runs.
- Every `CHEMCLAW_HPC_*` variable is rejected at startup rather than ignored, for the same reason.
- `docs/reference/architektur.md` keeps its HPC/SLURM/Nextflow prose — it is a pre-implementation
  design document, not a description — with a note at §6 saying which parts this ADR retracted.
