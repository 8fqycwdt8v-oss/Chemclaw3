# D-2026-08-01-spend-is-a-ledger-not-a-label — Spend is a ledger, not a label

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-152 (the bounded metric registry),
D-157 (the durable job record), D-130 (what may not `await` on the teardown path)

## Context

The readiness row read: *"Token metrics carry `profile` only, so 'what did team X cost' is
unanswerable, and HPC/compute spend is entirely unmetered — no counter for jobs launched or
node-hours, on the most expensive thing the system does."*

Half of that is exactly right and half of it is not, and separating them is most of the decision.

**The tokens half is right, and the obvious fix is unavailable.** `chemclaw_tokens_total` and its
four siblings carry one label, `profile`. Adding `actor` looks like a one-line change and is a
trap: `api/metrics` caps a counter at 64 label series and *refuses* past it (D-152), because a
label value is attacker-influenced and an unbounded map keyed on one is the slow leak this codebase
has already fixed three times. An Entra `oid` is precisely such a key — minting tokens for many
oids is the documented way around any per-principal limit. A per-actor label would therefore lose
series silently in any deployment with more than 64 users, which is worse than not having them.

The other place spend was measured is `api/budget.py`, which meters tokens per user *in order to
refuse a turn*: in process, reset on restart, LRU-evicted under a cap. Its own docstring says so.
A guard that forgets is correct. A ledger that forgets is not.

**The compute half is partly false.** `chemclaw_jobs_started_total` has existed since D-118
(`connectors/jobs.py`). What was missing is not a count but a *magnitude*: a two-second xTB call
and a six-hour DFT run incremented it identically, and `job_records` — the durable row that records
what ran, on what, and why — said nothing about what any of it consumed.

## Decision

**Per-actor spend is a table; fleet-wide spend stays a counter.** They answer different questions
and want opposite properties: a rate over seconds with bounded cardinality, versus a sum over a
quarter with unbounded cardinality. `turn_costs` holds one row per completed turn — actor, session,
profile, the four token counts separately (they are priced separately), duration, and whether the
turn answered — keyed on `correlation_id`, which already identifies the turn, already keys
`audit_events`, and is already on every log line. So "what did team X cost" is a `GROUP BY`, and it
joins to the audit trail with no new correspondence to maintain.

**A turn that never answered is billed, and marked as such.** A client that hangs up on a runaway
turn is precisely the spend a ledger exists to find; excluding it would under-report the one case
that matters. `completed` is a column rather than a filter.

**The write does not `await`.** The runner books it from a `finally` that also runs on the
disconnect path, where an `await` re-raises the pending cancellation and silently skips everything
after it — including five context-var resets, which would leak one turn's identity into the next
(D-130). So `record_turn_cost` is an ordinary function that schedules the write as a task, holds a
strong reference to it (or the loop's weak one lets a cost row be collected mid-write), and
swallows and logs its own failure. **Losing a cost row is acceptable; failing a turn that already
answered in order to record what it cost is not.**

**Compute gets a consumption counter and a durable duration.** `job_records.runtime_seconds` is
measured by the wrapper workflow across the child with `workflow.now()`, and
`chemclaw_job_runtime_seconds_total{connector}` accumulates it — so `rate()` reads as
compute-seconds per second, the same shape as the token counters and the standard way spend is
expressed. Booked in the `record_job` **activity** and not in the workflow body, because a workflow
may be replayed and a replayed increment would count one expensive run several times.

## Why not the alternatives

**An `actor` label on the token counters.** Refused by the registry past 64 series, by design. Not
a limitation to route around — raising the cap would restore exactly the unbounded-map leak the cap
exists to prevent.

**Push attribution into OpenTelemetry.** MAF already emits `gen_ai.client.token.usage` with richer
labels than this registry could cheaply provide, and the chart turns OTel on. But a span is sampled
and expires with the collector's retention; chargeback is a question asked of a quarter, and the
answer must be exact rather than sampled.

**Put the spend on `audit_events`.** It is already durable, actor-attributed and correlation-keyed,
which is genuinely tempting. It is also a hash-chained GxP compliance record with a versioned
schema and a deliberate no-prune rule, and cost telemetry does not belong inside the artifact whose
integrity is the point. Two tables, one join.

**Make the budget tracker durable instead.** That conflates a guard with a ledger. The guard must be
fast and in-process on the hot path of every turn; the ledger must be exact and complete. Merging
them makes the guard slower and the ledger subject to the guard's LRU eviction.

**A histogram for job duration.** Wrong instrument twice: the shared bucket set tops out at 300 s,
which is noise for HPC work, and the question is how much was consumed rather than how the
durations were distributed.

## Consequences

- "What did this actor/team spend over this window" is one query (`read_spend_by_actor`), exact,
  and retained as long as the operator keeps the rows.
- The metric counters are unchanged. Nothing that reads them today reads differently; the ledger is
  a second booking of the same numbers against an identity the counters cannot carry.
- A deployment without Postgres schedules no task and writes nothing — the same
  `session_store == "postgres"` switch the audit sink and the job record already read.
- The most expensive thing the system does now has a magnitude beside its count, in two places: a
  durable per-run number and a per-connector consumption rate.
- **What this is not: node-hours.** Parallelism belongs to the launcher, and no launcher in this
  repository reports it back yet — that needs the real Seqera/Tower run metadata and stays an open
  row. Runtime is the factor node-hours multiplies, and it is what is measurable today; the row
  says so rather than reading as closed.
- **What this is not: pricing.** Tokens and seconds are recorded, never money. A rate card is a
  deployment's own fact, changes without notice, and differs per model and per cluster; the ledger
  holds the quantities and leaves the multiplication to whoever knows the numbers.
