# Task: durable job records — "what was run, with what data, and *why*" (D-157)

Requested 2026-07-31. Branch: `claude/bo-campaign-storage-docs-x87dmj`.

(The previous occupant of this file, the restructure-consistency pass, is merged; its record is
D-156. The agentic-system review before it is D-145/D-151…D-153.)

## The problem, as found

A multi-round BO campaign is durable *while it runs* — `BoCampaignWorkflow` carries the observation
history as workflow state, so it resumes across worker restarts — and effectively **undurable once
it finishes**:

1. The finished `CampaignResult` (best point **plus every intermediate observation**) exists only as
   the Temporal workflow result. No Postgres row, no file. Nothing configures namespace retention,
   so the entire history expires with the server's default.
2. A later session cannot find a past run at all: no listing tool, and the job id lives only in the
   launching session's transcript. `get_durable_job_status` needs that id and asks Temporal, which
   by then may have forgotten it.
3. **Nothing records why a job was run** — not BO, not QM, not any connector job. `Note` has no
   intent field and result notes are output-neutral by design (D-005).
4. The `bo-candidate` note carries the best point alone: no decision space, no objective direction,
   no link to the run that produced it.
5. Publishing that note was *doubly* opt-in — `CampaignSpec.publish_to_graph` (model-authored,
   default `False`) **and** the manifest flag — so a campaign whose spec omitted it left no
   permanent trace whatsoever.

Root cause of all five: **the connector-job seam keeps no durable record of a run.** The fix
therefore belongs in core (`durable/connector_job.py`, `connectors/jobs.py`), not in the BO bundle
— every connector job gets it at once, which is what the report asked for ("relevant for all other
tools and models").

## Plan

- [x] Reserve the ADR number in `docs/decisions/README.md`, in the first commit. It was
      D-155 and became **D-157**: two branches merged ahead of this one while it was in review, so
      the branch merging second renumbers (CLAUDE.md's rule 3) — the file moved, its heading and
      every citation with it.
- [x] **1. A launch states its reason.** `ConnectorJobInput.rationale`; the generated job tool takes
      `rationale` beside its params and refuses a blank one (`require_actor`'s polarity — a
      forgotten reason must not silently downgrade the record). Deliberately **not** in `payload`,
      so the idempotency hash is untouched. A template `job` step passes its declared `purpose`.
- [x] **2. One durable record per finished job.** `infra/sql/023_job_records.sql` +
      `durable/job_record.py`: `JobRecord`, the sink protocol, `NullJobRecordSink`,
      `PostgresJobRecordSink`, `default_job_record_sink()` gated on `session_store="postgres"`
      (mirroring `default_audit_sink`), and the activity core's wrapper runs. It stores the payload
      (the problem definition), the rationale, and the **whole** result envelope — so a campaign's
      history outlives Temporal's history.
- [x] **3. The note says why.** `note_with_run_provenance`, applied by core to *any* connector note
      before the PR-gate, so no connector can forget and every merged md file answers "why was this
      done" as well as "what came out".
- [x] **4. Retrospective access.** `get_durable_job_status` falls back to the record when Temporal
      no longer knows the id; a new `find_past_jobs` tool lets a *new* session ask what has been run
      and why.
- [x] **5. BO leaves no silent gap.** Drop `CampaignSpec.publish_to_graph` (one decision, in the
      manifest, where the deployment owns it); the note gains the decision space and the objective
      direction, so it is self-contained.
- [x] ADR D-157, BACKLOG entries, `durable/README.md`, the retention docstring's refusal list.
- [x] `make lint type test` plus `connector-validate`, `template-validate`, `prose-validate`,
      `kg-validate`, `skill-validate`.

## Deliberate scope lines

- **Only finished jobs are recorded.** A failed run raises out of the child workflow and never
  reaches the record write. The audit trail already holds the attempt; a failure record is a second
  design (which status, written from where, does a retry supersede it) and is logged in BACKLOG
  rather than smuggled in here.
- **`job_records` is not pruned.** It joins `durable/retention.py`'s documented refusals for the
  reason `calculation_results` is there: it is now the record the system depends on for
  retrospective truth, and disposing of it is a GxP policy decision that needs its own ADR.

## Review

Implemented as planned. Five things the plan did not foresee, all recorded rather than papered over:

- **The launcher has three exits, and only one of them starts a workflow.** `inline_wait_seconds`
  returns a finished envelope inline, and a duplicate launch rejoins an existing run. Writing the
  record from the *workflow* rather than from the launcher covers all three by construction — no
  per-exit code and no per-exit test, because none of them is where the record is written.
- **`ConnectorJobInput` has two construction sites**, not one: the generated tool and
  `TemplateWorkflow._run_job_step`. The template path passes the step's declared `purpose`, falling
  back to a deterministic `template step '<id>' (job <name>)` when a step declares none — so the
  reject-if-absent rule holds on every path in, and no existing template breaks.
- **The note footer must not contain a wikilink.** `note_from_campaign_result` already documented
  this trap (a dangling link fails `kg-validate` on the very PR the note opens); the shared footer
  inherits it for *every* connector at once, so it is pinned by its own test.
- **`Note` is frozen**, so the footer builds a copy via `model_copy(update=...)` — which also leaves
  the connector's own object intact for the envelope the tool returns.
- **An activity nothing serves fails silently here.** The record write is best-effort, so an
  unregistered `record_job` would retry to its bound, log, and lose every record — discovered only
  when an id expired months later. `registered_activities("background")` is asserted directly, in a
  test that runs offline.

## Verification

`make lint`, `make type`, `make test` green: **2105 passed, 32 skipped**. The skips are the
Temporal test server (`temporal.download` is blocked by this environment's network policy) and the
xtb/crest binaries.

**The Postgres skips were removed rather than accepted**: `postgresql-16` was already installed
here, so a local cluster plus pgvector 0.8 (built from source — the packaged 0.6.0 predates
`bit_jaccard_ops`, which migration 002 needs) turns the whole Postgres-backed suite, including this
change's four new store tests, from skipped into run. Migration 023 is therefore applied and
exercised, not hand-checked.

Verified **by mutation** — break the line, run the suite, confirm the named test fails:

- blank rationale accepted → `test_a_launch_must_say_why_it_is_being_started` fails;
- the record fallback removed from `get_durable_job_status` →
  `test_a_job_whose_history_expired_is_still_collected_from_the_record` fails.

**Known gap, measured not assumed:** removing `note_with_run_provenance` from the wrapper leaves the
offline suite green — core *applying* the footer is asserted only in `test_connector_job_workflow.py`,
which needs a live Temporal server and therefore runs in CI alone. The footer function itself, and
every other pure piece (`job_record_for`, the sink selection, the activity registration), is pinned
offline. That is why `job_record_for` exists as a function at all.
