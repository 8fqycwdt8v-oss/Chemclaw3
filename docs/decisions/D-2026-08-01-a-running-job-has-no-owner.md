# D-2026-08-01-a-running-job-has-no-owner — A running job has no owner, so cancelling one is an operator action

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-011 (never compute twice), D-157 (the
durable job record)

## Context

There was no job surface for a user at all. `GET /jobs`, `GET /jobs/{id}` and a cancel were absent
from the sixteen routes; status and result were reachable **only** as an agent tool inside a turn
(`get_durable_job_status`). So a chemist could not list what was running, could not fetch a result
once the session holding it had been evicted — even though `job_records` had held that result since
D-157 — and could not stop a run that had gone wrong.

The backlog asked for the obvious shape: `GET /jobs`, `GET /jobs/{id}`, `DELETE /jobs/{id}`,
**owner-scoped**.

Owner-scoped cancel cannot be built, and the reason is a property of the system rather than a
missing column. `connectors/jobs.py::job_workflow_id` hashes `[connector, job, payload]` and
*deliberately excludes the requester*, so two chemists asking for the identical campaign rejoin one
run — that is D-011 working, and the comment beside it says so: "two chemists asking for the
identical campaign with differently-worded reasons must still rejoin one run rather than each paying
for it". The parent workflow carries no memo either; `requested_by` reaches the *child*.

So a running job genuinely has more than one requester, and "my job" is not a thing.

## Decision

**Reads are open; the cancel is an operator action.**

`GET /jobs` and `GET /jobs/{id}` are not owner-scoped, and that is the deployment's existing
position rather than a new one: `find_past_jobs` — the agent tool over this same table — is
unscoped, for the cross-project learning D-004/KM-9 argues for. A read the agent will happily make
on a chemist's behalf is not one to withhold from the chemist. `requested_by` is on every row, so a
surface can filter by it; nothing pretends the row is private.

`DELETE /jobs/{id}` requires a privileged role. Cancelling a shared run cancels it for everyone who
joined it, and the first requester is not more entitled to that than the second. An owner-scope
check here would *read* as ownership and not be it, which is worse than an honest role gate: it
would let the first requester silently cancel the second's work while appearing to protect them
from exactly that.

The cost is real and is stated rather than hidden: **a chemist cannot stop their own runaway run
without an operator.** The alternative — making the workflow id per-requester — would trade a
recompute of every shared expensive job for it, which is the trade D-011 exists to refuse.

It returns **202, not 204**: Temporal cancellation is cooperative, so the request is *delivered*,
not completed. Poll `GET /jobs/{id}` for the outcome.

`job_status` is extracted so the agent tool and the route call one function. A chemist polling in
chat and a chemist refreshing a page must not be able to get different answers about one run.

## Consequences

The durable-job layer is reachable from outside a turn for the first time. A result survives its
conversation, which is what D-157 built the record for and what nothing had exposed.

**`cancel_job` is deliberately not an agent tool**, for the reason `POST /approvals/{id}/decision`
and the plan gate are not (D-005): stopping work a person asked for is a decision about that
person's work.

**What this does not give a chemist**: a way to stop their own run. If that becomes a real need, the
fix is not a scope check on this route — it is a *per-requester* job id, and that is a change to
D-011's idempotency contract with a measurable cost, so it wants its own decision.

## The other half: a failure a user can act on

Shipped in the same change because it is the same complaint — the surface cannot tell a user what
happened. `runner.py` caught `Exception` and returned "The turn could not be completed due to an
internal error (session …)", so a connector being down, an LLM timeout, a database outage and a
malformed tool argument were one string. A UI could offer no next step, and "try again" was as
likely to be wrong as right.

`ErrorEvent` gains `code`, `retryable` and `correlation_id`. The taxonomy is short and closed on
purpose: each member is *a different thing for the user to do* — retry, wait, fix the input, ask an
operator — not a different place the traceback came from. An unrecognised failure stays `internal`,
because admitting the classification is missing beats guessing a friendlier code: a wrong
`retryable=True` sends someone to burn another turn on a failure that cannot succeed.

`correlation_id` is the field that was actually missing. The old message named the *session* — the
id the user already has. The correlation id is what `audit_events` is keyed on
(D-2026-07-31-the-audit-chain-is-versioned), so quoting it in a bug report is what lets an operator
find the turn. It is a random per-turn hex string, so nothing sensitive travels with it.
