# Remove every HPC-dependent workflow

**Ask.** "Remove completely any hpc dependent workflow. For now all I want to have in this repo
are semi empirical calculations using xtb tblite or crest. They will not run on a hpc but rather
in a own pod on databricks or openshift."

**Reading.** Exactly one capability in this family is HPC-dependent: the `qm` bundle
(`compute_dft_energy`), whose whole dependency closure is the Nextflow/Seqera launcher, the HPC
artifact store and a 24 h poll. The semiempirical tier — xTB/tblite via `servers/calc` in
`Chemclaw3-mcp`, CREST conformer/complex searches, the D-011 cache and the calibration ledger —
already runs as its own pod addressed over HTTP, so nothing there needs building; it needs the
DFT tier taken out from under it and every knob, secret, queue and sentence that only existed to
reach a cluster removed with it.

## Chemclaw3 (core)

- [x] Delete `src/chemclaw/connectors/qm/` whole (activities, cache, knowledge, specs, worker,
      workflows, `hpc/nextflow.py`, `connector.yaml`, `skills/qm-job-submission/`).
- [x] Delete `src/chemclaw/core/config/hpc.py`; rehome the one knob that is not about HPC
      (`qm_activity_timeout_seconds`, read by three core workflows) onto `TemporalSettings` under
      an honest name; delete every `hpc_*` field and both derived properties.
- [x] Drop the `HpcSettings` mixin and the `_the_job_ceiling_covers_the_poll_it_bounds`
      cross-section validator from `core/config/__init__.py`.
- [x] `publish/project.py`: remove the `QMJobResult` projector.
- [x] `agent/authz.py`: drop `compute_dft_energy` from the write-tool gates.
- [x] `agent/chemclaw_agent.py` + `connectors/calc/connector.yaml`: remove the prose pointing at
      the DFT escalation (prose-contract rules 1-2 would fail on a tool that no longer exists).
- [x] Helm chart: the `connectors.qm` entry, `CHEMCLAW_HPC_*` env, the `hpcApiToken` secret row.
- [x] `.env.example`, `README.md`, `deploy/README.md`, `SECURITY.md`, `ARCHITECTURE.md`,
      `docs/guides/runbook.md`, `docs/guides/xtb-use-cases.md`, `docs/reference/architektur.md`,
      `CLAUDE.md`, `docs/planning/{BACKLOG,DEFERRED}.md`.
- [x] `infra/live/e2e-full-stack/`: drop the HPC launcher env and rename the mock process.
- [x] Tests: delete `test_nextflow_adapter.py`, `test_qm_workflow.py`, `test_qm_persistence.py`;
      fix every other test that names `qm`, `QMJob*`, `compute_dft_energy` or a `hpc_*` setting.
- [x] ADR recording the removal and what re-adding one would cost.

## Chemclaw3_mock

- [x] Delete `app/hpc/` (the Nextflow-shaped launcher + artifact store), its router mount, its
      five settings, its tests and its `start.sh` env.

## Chemclaw3_ui

- [x] Remove `compute_dft_energy` from the tool tables (`shared/events.ts`, `src/chem/provenance.ts`),
      the `qm` capability-loss line, and the DFT step of the full-stack e2e.

## Chemclaw3-mcp

- [x] `CLAUDE.md`: the "DFT via Nextflow/HPC | `qm`" row of the never-duplicate table.

## Verification

- [x] `make lint type test` green in Chemclaw3 with Docker up (Postgres tests must not skip).
- [x] `make check` in Chemclaw3-mcp; `pytest` in Chemclaw3_mock; `npm test` in Chemclaw3_ui.
- [x] `grep -ri "hpc\|nextflow"` over `src/`, `deploy/`, `tests/`, live docs returns nothing live.

## Review

Done, across all four repos. Notes worth carrying:

**Three things had to be kept rather than deleted with the tier, and each was a judgement call the
grep did not make for me.**

1. The `dft` **projector** stays (`publish/project.py`). `calculation_results` is never pruned, so a
   deployment upgrading into this release still holds every `dft` row it wrote; the backfill path
   resolves a row by `calc_type` prefix alone. That module already states the rule — "a retired
   calculator keeps its projector" — and `xtb.scan` is the same shape from an earlier move. The
   `PAYLOAD_PROJECTORS["QMJobResult"]` half *did* go, because it is keyed by a model name no live
   payload can state. A test now asserts the retired-row case directly.
2. The **parent-ceiling invariant** stays. `_the_job_ceiling_covers_the_poll_it_bounds` guarded a real
   defect (a ceiling no larger than one child activity makes the retry budget unreachable and fails
   with a message naming neither setting). Deleting it with the poll would have left `calc` exposed
   to exactly that, since `xtb_job_timeout_seconds` (4 h) is the longest activity now. Rewritten as
   `_the_job_ceiling_covers_the_activity_it_bounds`, with `connector_job_timeout_seconds` re-derived
   from 90,000 (24 h DFT poll + 1 h) to 18,000 (4 h search + 1 h).
3. `job-result` moves **back into core's `KNOWN_NOTE_TYPES`**. It sat in `qm`'s manifest under the
   rule that a type a bundle *mints* belongs to that bundle. No bundle mints one now, so by that same
   rule it is core's vocabulary again — and `knowledge/job-result/` holds three notes that would
   otherwise fail `kg-validate`. `bo-candidate` stays with `bo`.

**One control was rewritten rather than dropped, and it is the finding worth repeating.**
`test_the_bundle_has_no_way_to_write_the_note_itself` asserted `not hasattr(qm_knowledge,
"write_knowledge_node")` — a guard named after a single module, which would have gone *dark* the
moment that module was deleted while still reading, in review, as a control. That is the
`map_to_hpc_identity` shape this repo already has a name for. It is now an AST walk over every
bundle asserting none imports `kg.pr_gate` or names `propose_note`: strictly stronger, and it does
not depend on which bundles exist.

**Two pieces of genuinely dead code fell out of the removal**, both kept alive only by tests that
called them directly: `Structure.as_xyz` (its one caller was the launcher) and its test.

**What the validators caught that grep did not**: `make prose-validate` and `make skill-validate`
found five live claims left over — two skills still declaring `compute_dft_energy` in their
frontmatter, and three backticked paths naming deleted files. Worth running the whole validator set,
not just `lint type test`.

**Scope note.** `docs/reference/architektur.md` keeps its HPC/SLURM/Nextflow prose deliberately: it
is a pre-implementation design document, not a description of the system, and CLAUDE.md already
frames it that way. §6 carries a note saying which parts this change retracted.
