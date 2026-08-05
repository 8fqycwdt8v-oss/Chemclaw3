# D-2026-08-05-a-sweep-that-commits-once — a sweep that commits once can lose everything it did

**Status:** accepted · **Date:** 2026-08-05

## Context

`durable/retention.py` already carries this argument, in `prune_expired_rows`'s own docstring:

> Each table is pruned **and committed** in its own statement, so one failure cannot roll back the
> others. That was the docstring's claim before it was true: there was a single `commit()` after
> the loop, so a timeout on the second table discarded the first table's deletions and the run
> reported them as done — a sweep that says it removed rows it then rolled back is worse than one
> that fails outright, because the growth it was meant to bound continues while the log says
> otherwise.

The fix was applied at the table level and the identical structure was left one level down.
`session_messages` cannot be pruned with a single `DELETE` — whether an expired row may go depends
on the rows *paired with it*, which may not be expiring (D-145) — so it is pruned per session, in a
loop, with `await conn.commit()` after the whole loop. Every property the paragraph above objects
to held inside that loop:

- A failure on the four-thousandth session discarded the deletions of the first three thousand nine
  hundred and ninety-nine, and the pass reported nothing removed while the table went on growing.
- The transaction held its row locks on `session_messages` for the entire sweep, on the
  single-replica `background` worker that also runs the reindex, the chain verification and every
  connector job's record.

**And the sweep was unbounded.** `SELECT DISTINCT session_id FROM session_messages WHERE created_at
< cutoff` had no `LIMIT`, so the first pass after a deployment enables retention faces every session
it has ever had — three round trips each, inside one activity's `retention_timeout_seconds`, under
a 30 s `pg_statement_timeout_seconds` per statement. Exceeding either costs one of
`activity_max_attempts` and, before this ADR, committed nothing at all. Five attempts later the
schedule has deleted nothing and will do the same tomorrow: a retention job that can never complete
its own first pass.

## Decision

**Commit per session**, so the loop's failure mode is "stopped early" rather than "did nothing".
This is the table-level argument applied where it was missing, not a new one.

**Cap the batch** at `retention_max_sessions_per_pass` (500), and **report the remainder** in
`RetentionOutcome.sessions_deferred`. The cap makes every pass bounded and the schedule drains the
tail; the report is required because *a cap that is not reported reads as "there was nothing more"*
— a table still growing would look bounded in every result this job returns, which is the same
class of silence the module docstring already objects to.

500 is roughly a minute of round trips: far more than a steady state produces in a day, far less
than a first pass over a year of history.

The cap is implemented by asking for `cap + 1` and working `cap`, so the job learns whether a tail
exists without a second query and without ever reporting a count it did not measure.

## Consequences

**A first pass over a long backlog now takes several scheduled runs instead of failing all of
them.** At the shipped daily cadence a large backlog drains over days rather than never; a
deployment that wants it faster raises the cap, which is now a number rather than an implicit
"all".

**`RetentionOutcome` gained a field**, so a caller reading the job's result sees three numbers
instead of two. That is the point of it being in the result rather than only in a log: the job's
own record is what makes the deletion auditable, and "and there is more" belongs in the record.

**Row locks are held for one session at a time.** The sweep no longer blocks concurrent writes to
`session_messages` for its whole duration — which matters because those writes are chat turns.

**Not decided here:** the eight tables retention neither prunes nor refuses (`session_owners`,
`session_turns`, `turn_costs`, `predictions`, `measurements`, `note_proposals`, `plan_approvals`,
`bo_suggestions`), and the `session_owners` row that survives its session's pruned history, leaving
a listable session with nothing in it. Both are recorded in `docs/planning/BACKLOG.md`; each needs
a disposal *policy* first, and inventing one inside a sweep is exactly what the three documented
refusals in this module exist to prevent.
