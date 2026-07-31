# The live matrix, 2026-07-31 — every capability, with the flags on

*Point-in-time record. Accurate as of its date; deliberately not updated. See `docs/decisions/D-150`
for the decision and `docs/planning/BACKLOG.md` (DARK-1…DARK-10) for what was left open.*

## Method

The whole stack, natively, with **every off-by-default flag enabled** — the premise being that the
largest untested surface in this system is not the code that runs but the code that does not.

| | |
|---|---|
| Postgres 16 + pgvector 0.8.1 | 21 migrations, clean database |
| Temporal 1.4.1 dev server | 4 workers: `background-jobs`, `connector-calc`, `-bo`, `-qm` |
| Connectors | all six MCP bundles on one dev process; `qm` is jobs-only by design |
| Identity | `entra_required=true`, local JWKS over real HTTP, one signed `oid` per probe |
| Model | Haiku 4.5 for the scripted probes, Sonnet 5 for the judgement-heavy ones |
| Compute | real `xtb` 6.6.1 from apt; `tblite` 0.7.0 in-process |
| Knowledge | a dedicated checkout with its own bare remote, because the PR-gate `reset --hard`s what it is given |

Flags turned on that ship off: `session_store=postgres`, `entra_required`, `harness_enabled`
(per-profile), `verifier_enabled`, `mid_turn_resume_enabled`, `budget_enabled`,
`retrieval_mode=hybrid`, `data_sources` with `eln-ord`/`vector`/`lexical`/`vendored`,
`note_reindex_enabled`, `audit_verify_enabled`, `retention_enabled`, `digest_enabled`,
`calibration_enabled`, `eval_drift_enabled`, `connectors_required`, both artifact-eviction bounds.
Nine Temporal Schedules registered, against four with the defaults.

Two instruments: scripted capability probes asserting on **observables** — an event on the SSE
stream, a row in Postgres, a branch in the notes repo, a workflow in Temporal — never on the prose;
and open expert questions graded by hand. Three code reviews ran in parallel over disjoint areas,
each with a stated method (docstring-as-assertion, dark-code, seam-and-invariant).

## What the probes found

| Track | Probes | Result |
|---|---|---|
| T1 standard agent | 7 | all pass |
| T2 deep research | 4 | all pass |
| T4 every execution path | 11 | 10 pass; the template path was **dead** until fixed |
| T6 retrieval modes | 3 | all pass |
| T7 document generation | 2 | all pass |
| T8 generative chemistry | 3 | 2 pass; BO campaign **dead** until fixed |
| T9 calculation persistence | 8 | all pass (real xTB, cache, artifacts) |
| T10 access control | 5 | all pass after the assertions were corrected — see below |
| T3 harness/autonomy | bespoke | gate confirmed **broken**, DARK-1 |

## Eight defects, fixed

1. **Every step template unreachable from a conversation** — `cast(BaseModel, params)` is a static
   no-op and MAF passes a dict. D-138's defect in the sibling module.
2. **`start_optimization_campaign` dead on every call** — the declared precondition takes an `int`,
   the launcher passes it the `CampaignSpec`.
3. **The embedding cache evicts the batch it is about to return** — the note-index rebuild raises
   `KeyError` for any corpus above 2048 notes.
4. **A broken verifier reports `confidence=1.0`** — a down judge produced a stronger signal than a
   working one.
5. **Retention deletes undelivered push-back events** — including the tamper-evidence alerts, which
   are never consumed by construction.
6. **A detected audit-chain tampering shows a green schedule** — alert delivered to a channel with
   no eligible consumer and no log line.
7. **The digest advances its watermark after a swallowed delivery failure.**
8. **Artifact eviction registered, served, and started by nothing.**

Each fix carries a regression test demonstrated failing on the pre-fix tree. Two closed their *class*
rather than their instance: `make connector-validate` now checks a precondition against the params
object it will be handed, and the template launcher is exercised through the framework's own
dispatcher instead of through our idea of it.

## The tracks that needed orchestration

| Check | Result |
|---|---|
| A proposed note reaches the PR-gate as a branch | 9 branches minted across the run |
| An unreviewed note is **not** on the served tree | invisible before merge, as the gate intends |
| A merged note is readable in a **later** session | read back after merge, graph cache TTL 0 |
| ELN sync ingests and records a per-source cursor | 2 cursors, both adapters |
| A durable job survives its worker being killed | `report-c2ed042bb6c4fa82` completed after the background worker was killed mid-job and restarted |
| Every enabled schedule is registered | all 9 |
| `audit-verify`, `note-reindex`, `retention`, `digest` triggered | all four **Completed** |
| The audit hash chain verifies over the live run | 160 rows, chain intact |

State left behind by the run, as evidence that the paths executed rather than returned 200:
`audit_events` 160 · `calculation_results` 85 · `artifact_blobs` 63 · `note_index` 37 ·
`session_messages` 356 · `sync_cursors` 2 · `plan_approvals` 1 · `predictions` 2 ·
`subscriptions` 1 · `user_preferences` 1.

## Two corrections worth recording

**A failure I reported to myself was not a failure.** The RBAC probes searched the answer text for
`Refused` — the *internal* marker `surface_authorization_denials` sets — and reported that an
unprivileged caller had not been refused. The gate had held perfectly: `audit_events` carried
`AuthorizationError('visitor-001 is not authorized to use compute_dft_energy…')`, no workflow
started, and the model relayed it in good chemist-facing language ("your account does not have the
required access… contact your administrator"). The probe was asserting on prose, which is the exact
mistake the harness's own docstring warns against. Rewritten to assert on the denial row.

**My first offline baseline was invalid.** I sourced the live e2e configuration into the gate run
and got 77 failures — every one an artefact of tests that assert *default* behaviour being run
against a deployment configured for the opposite. Re-run clean: **2061 passed, 26 skipped, 87.2%
coverage**. The 26 skips are 19 Temporal (the test-server binary cannot be fetched here) and 7 CREST
(not packaged for this distribution) — the only two subsystems whose sole verification is CI.

## What could not be exercised, and why

- **CREST conformer search** — not packaged for Debian; 7 tests skip. Not faked.
- **Real HPC/DFT** — `hpc_launch_interface=mock`; the durable spine ran, the cluster did not.
- **A real Entra tenant** — every token was locally signed, as in every prior pass.
- **A real cluster** — no `helm install`, no image push.

These are the same four `DEFERRED.md` records as before: gated on external facts, not on effort.

## The finding behind the findings, for the third time

D-138 recorded it: *the test supplied the thing the system was supposed to supply*. It recurred
verbatim three more times here — the template tests checked the launcher's name and never called it;
the BO tests called the ceiling rule with a bare `int` while the launcher passes a spec; the
embedding-cache tests embed one text per call while production embeds the whole corpus in one batch.

It is not carelessness. It is what happens when a test constructs its own inputs instead of driving
the real entry point, and it is invisible to coverage — every one of these lines was covered. The
countermeasures that work are the two applied here: drive the framework's own dispatcher, and make a
validator compare the two declarations rather than trusting either.
