# D-2026-09-07-a-claim-that-outlives-its-transaction-is-a-lease — the outbox claim is a timestamp, not a fourth state

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
`D-2026-09-06-a-response-class-nobody-named-is-a-delivery-nobody-made` (which measured this defect,
corrected `_CLAIM`'s comment rather than its guard, and added the exhausted-row reaper), D-011 ·
**Departs from** the shape the `BACKLOG.md` row it closes prescribed (`state='in_flight'`), for the
reason under *Decision*.

## Context

`publish/outbox._CLAIM` spends the attempt and commits **before** the delivery, deliberately: a
delivery can take the better part of a minute and must not hold a row lock across it. So
`FOR UPDATE SKIP LOCKED` excludes only *overlapping transactions*, and this one lasts milliseconds
— while the case the guard was written for, a scheduled drain plus an operator's manual one,
overlaps over the **delivery**.

## What was measured

With a sink taking 1.0 s and a second drain started 0.3 s in, driven through `_drain_one` on a real
database: both drains delivered the same row (`['overlap', 'overlap']`) and it came to rest at
`attempts=2` for one delivery. Duplicate delivery is safe — every far-side key is a content hash,
verified over three redeliveries — so the harm is the accounting: an attempt budget of 8 empties
after 4 real attempts against one destination's outage, retiring rows a recovering destination
would have accepted.

## Decision

A claim is a **lease**, and the lease is a `claimed_at` timestamp (migration
`089_result_publication_lease.sql`) rather than a fourth `state`. Both spellings close the
double-claim by predicate; the timestamp is chosen because the state does not carry its weight:

* `result_publications` goes from three states to four, and every reader of the column has to learn
  the new one — `_PENDING` and `_ORPHANED` would have to add it back to keep counting an
  undelivered row as backlog (it *is* backlog), `_MARK_FAILED`'s `state = 'pending'` guard would
  have to move, and `backfill --requeue` and `durable/retention.py` each need re-reading. A leased
  row is still `pending`, which is the truth, so none of that is work anybody has to do.
* An abandoned claim would need a **second** reaper to return an `in_flight` row to `pending`. It
  needs none: a claimer that died still leaves the row `pending` with its attempts spent, which is
  exactly what the reaper added by the ADR above already retires — so the crashed-claimer path is
  the one that was built last week, unchanged.
* The migration is one additive nullable column with no `CHECK` to widen, so it needs no
  `_REVIEWED_ROLLBACK_BREAKS` exemption and the previous image keeps writing the table.

An abandoned lease returns to the queue **by predicate, at the head of the next ordinary claim for
that sink** — no second timer, no sweeper to schedule. `_UNLEASED` is one SQL fragment shared by
`_CLAIM` and `_REAP_EXHAUSTED`: `claimed_at IS NULL OR claimed_at < now() - lease`. Sharing it is
not tidiness — a reaper whose idea of an abandoned claim were wider than the claim's would
dead-letter a row another drain is delivering at that moment, and `_MARK_FAILED`'s `state =
'pending'` guard would then drop the destination's own account of the failure in favour of the
reaper's generic sentence.

`result_publish_lease_seconds` is **derived, not configured**: it is the drain activity's own
`start_to_close` ceiling, `result_publish_timeout_seconds × max(1, sinks)`. Below it a second drain
steals rows the first is still delivering; above it an abandoned row waits longer than it must; at
it, Temporal has already given up on the claimer. `PublishResultsWorkflow` now reads the same
property it used to spell out, so the lease and the budget it tracks cannot drift apart.

`mark_delivered` and `mark_failed` both clear `claimed_at`. For the failure path that is not
bookkeeping: a row still leased is not in the queue, so without the release a destination's outage
would cost one retry per *lease period* instead of one per drain pass.

## What holds it

`tests/test_publish_outbox.py::test_two_overlapping_drains_do_not_both_deliver_one_row`, watched
failing against the unfixed source with `['overlap', 'overlap']`, and
`test_a_lease_its_claimer_died_holding_returns_to_the_queue_on_the_next_claim`, watched failing on
the second claim taking a row the first still held. The crashed-claimer case is asserted twice
more: the exhausted-row reaper's own test now drives eight *dead passes* rather than eight
back-to-back claims, and `test_the_real_failure_reason_outranks_the_reaper_s_generic_one` asserts
that a *reported* failure is claimable on the next pass rather than the next lease period.

Three tests in that file previously asserted the old semantics — two claims in a row both
succeeding. `test_claiming_a_row_spends_its_attempt` is the sharpest: it asserted the double-claim
*as the design*, on the argument that spending the attempt inside the claim is what keeps the
budget correct under two runs. Spending it there is necessary and was never sufficient.

## What is deliberately not done

No index on `claimed_at`. The claim already scans `result_publications_pending` in `enqueued_at`
order and stops at `LIMIT`; the lease is a filter on rows that scan has read anyway, and the leased
set is bounded by the batch size times the number of concurrent drains.
