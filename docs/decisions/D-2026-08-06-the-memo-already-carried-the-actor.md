# D-2026-08-06-the-memo-already-carried-the-actor — The memo already carried the actor

**Status:** accepted · **Date:** 2026-08-06

## Context

`resume_campaign` on a Bayesian-optimization campaign that had run **durably** reported no such
campaign — about hours of evaluation, a PR-gated recommendation, and a real result. Both paths mint
ids from one `campaign_id_for` space, and only the inline `suggest_next_experiment` ever wrote to
the campaign record (BO deep review, `D-2026-08-05-a-ceiling-that-does-not-hold`).

That review filed it as needing a decision rather than a fix, in these terms: `record_suggestion`
writes `opened_by`, and `BoCampaignWorkflow` deliberately does not know the actor because core's
`ConnectorJobWorkflow` owns attribution (D-093). Recording from inside the workflow therefore meant
either threading identity through a seam built to keep it out, or writing a fabricated actor into an
audited column.

## Decision

**Neither. `BoCampaignWorkflow` reads the actor off the run's memo, which core has set on every
connector job since D-118, and hands it to a bundle-owned activity that reuses `record_suggestion`
unchanged.**

`ConnectorJobWorkflow._run_child` sets `memo={"requested_by": ..., "correlation_id": ...}` on the
child, with a comment stating the exact purpose: *a bundle whose backend runs under a shared service
identity must still be able to name the user behind a run, and `payload` is exactly the
model-authored arguments, so putting the actor there would make it a field the LLM could fill in.*

`connectors/qm/workflows.py` has read that same memo in production since F5, with
`settings.service_actor_id` as the fallback for a run started outside the wrapper (a test, a manual
re-drive) — the same fallback `require_actor` uses. `tests/test_connector_job_workflow.py` already
pinned the crossing end to end.

So the dilemma was between two options neither of which was necessary. **The seam was not built to
keep identity out; it was built to keep identity out of the *payload*.** A memo is per-execution
metadata beside the argument, which is precisely the distinction that makes this safe.

## What else landed with it

Two adjacent gaps from the same review, in one migration because they are two columns on one table:

- **`bo_suggestions.problem`** — the decision space *as it was*. The campaign row holds the latest
  problem, because a chemist who widens a bound is still working the same optimization; a suggestion
  read back afterwards was then described by bounds that never applied to it. The candidates and the
  observations were already snapshotted for exactly this reason.
- **`bo_suggestions.job_id` + a partial unique index** — the idempotency key. The inline path wrote
  once per turn and a duplicate was harmless; a Temporal activity is retried by design.

The index is on the **run identity, never the content**. Two genuinely identical asks are two
history entries — that is what "the sequence *is* the campaign's history" means in
`031_bo_campaigns.sql` — so deduplicating by candidates-and-observations would erase real history.
It is partial on `job_id <> ''` because the inline path has no run to name, and a shared default
would collapse a campaign's whole inline history into one row.

Both store backends implement the rule. A `session_store="memory"` deployment runs the same retried
activity, so a guarantee that held only against Postgres would be one a dev stack could not rely on.

## Verification

Proven live rather than argued, since this was the one review item that could not be checked
offline:

- Migration 037 applied to the running database with `make db-migrate`.
- The partial unique index and the `ON CONFLICT ... WHERE` inference asserted against real
  Postgres — that combination is exactly the kind of thing that is right in prose and wrong in SQL.
- The whole campaign driven through the real broker and workers: summary `recorded as
  campaign-a9957bf78a2212aa`, resumed with `opened_by` `chemist@example.com` off the memo and all
  four observations present.

## Consequences

- `record_suggestion` gains one optional `job_id`, defaulted for the inline caller. It is not an
  abstraction with one caller — it has two, and they are the two writers this record has ever had.
- `_BO_ACTIVITIES` in `tests/test_bo_campaign.py` now comes from `registered_activities` instead of
  a hand-written list. The new activity would otherwise have been written, imported, absent from
  that list, and never run — the failure `chemclaw.durable.registry` exists to prevent, re-created
  one level down in a test.
- The write is best-effort by inheritance: `record_suggestion` swallows a database blip and never a
  programming error, which is the right rule here too. A campaign that finished must not fail
  because a record of it could not be written.
