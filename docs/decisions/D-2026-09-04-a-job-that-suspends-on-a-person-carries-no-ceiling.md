# D-2026-09-04-a-job-that-suspends-on-a-person-carries-no-ceiling — `awaits_answer` and the reaper it gives up

**Status:** accepted · **Date:** 2026-09-04 · Decided by the repository owner from three options the
review measured.

## Context

A BO campaign with a *measured* objective opens a durable wait for the wet-lab result.
`bo_measurement_deadline_days` is 14 days. That wait ran inside a child workflow whose execution
timeout is `min(declared, connector_job_timeout_seconds)` — and that ceiling is 18,000 s. A manifest
can only *lower* it, never raise it.

Measured against a live broker: **67.2x** over. The child `TIMED_OUT`, the job recorded
`failed: Timed out`, the wait was `TERMINATED`, and its `pending_requests` row sat `waiting` in the
inbox — answerable, for the rest of its deadline, about work that was already dead. The feature
could not survive its own wait.

Three options were put to the owner and the two cheap ones were rejected with their costs stated:
raising `connector_job_timeout_seconds` past 14 days gives *every* connector job in every bundle a
fortnight-long ceiling, so a genuinely wedged xTB or CREST job sits for two weeks instead of being
reaped in hours; clamping `bo_measurement_deadline_days` to fit destroys the feature, because a
wet-lab measurement does not return inside five hours.

## Decision

**A job that suspends on a person carries no wall-clock execution ceiling.** The seam is one
manifest field: `JobSpec.awaits_answer`. `child_execution_timeout` returns `None` for such a job,
and only `start_optimization_campaign` declares it.

A validator refuses `awaits_answer` beside `timeout_seconds`, because those are opposite claims and
honouring one silently would rebuild this defect somewhere quieter.

**`None` rather than a larger number, on arithmetic rather than taste.** A measured campaign is
`n_rounds + 1` waits; the shipped default spec alone spans 154 days and `bo_max_rounds` permits 501.
Every finite value is this same defect at a different scale. It is also not a new posture —
`_approve_effect` already waits three days, and works only because `connectors/jobs.py` gives the
wrapper no execution timeout either. This decision names what was already true for one path and
makes it declarable rather than accidental.

## Consequences

**`start_optimization_campaign` no longer has a wall-clock reaper.** A bo worker permanently gone
between rounds leaves the campaign `running` rather than failing it in five hours. What still bounds
it: `awaiting_max_days` per wait, an unanswered wait ending the campaign, per-activity
start-to-close plus heartbeat, and `bo_max_rounds` at launch. No config default was added or
changed.

`ParentClosePolicy.REQUEST_CANCEL` is set on the bundle's child too. Its three values were measured
against a live broker rather than chosen: `TERMINATE` settles nothing; **`ABANDON` is worse than
either** — the child stays `RUNNING`, leaving a live, answerable question about dead work for the
full deadline; `REQUEST_CANCEL` reaches the `except asyncio.CancelledError` the module already wrote
its detached settle for. That settle is still **racy** under cancellation and is a `BACKLOG.md` row;
the test therefore asserts the child's status, not the settle.

**Residual, recorded rather than hidden**: the template path still bounds the *wrapper* at
`wrapper_execution_timeout()`, so a measured campaign — or any irreversible job's three-day approval
— run as a template `job` step is still cut off at about five hours. It is named in that function's
docstring.
