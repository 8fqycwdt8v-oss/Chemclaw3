# D-2026-10-08-a-calculation-miss-is-claimed-in-postgres-before-it-is-computed — a result being computed by someone else is awaited, not recomputed

**Status:** accepted · **Date:** 2026-10-08

## Context

`science/calc/store.py::cached_compute` single-flighted concurrent misses on one key with an
in-process future. Two pods that missed the same key both computed it, and the second `put`
replaced the first: for a CREST search that is hours of duplicated CPU, and D-011 ("a persisted
result is never recomputed") said nothing about a result that was *being* computed. The program
item is W3.3 of `tasks/todo.md`; the substrate is Postgres only (D-2026-10-07).

Measured before the change, two processes each missing one key at the same moment: both ran the
computation (two lines in the shared counter file), and the later write won.

## Options

1. **A `pending` state in `calculation_results`.** One table, no new migration shape. Rejected: the
   results table is never pruned and its rows are payloads; a lease, a holder and an error text are
   transient coordination state with a different lifetime, and a half-row in a table `find`,
   `known` and the epoch filter all read would have to be excluded from each of them.
2. **A Postgres advisory lock per key held for the computation.** No table. Rejected: the lock lives
   as long as the connection, so a computation of hours pins a pooled connection (or a dedicated
   one per computation) for its whole duration, `statement_timeout` and pool recycling can drop it
   silently, and a waiter has nothing to read except "the lock is gone" — no failure to receive, no
   way to tell a crash from a finish.
3. **A claim row with a lease, heartbeated by the holder, over `INSERT … ON CONFLICT`
   (`calculation_claims`), woken by `LISTEN/NOTIFY` with a bounded poll.** Taken.
4. **Poll only.** Simplest, and correct by itself. Rejected as the primary path because a poll
   interval short enough to feel prompt costs a query per waiter per interval for hours; kept as the
   fallback, because notifications are not delivered across a dropped connection.
5. **Route every miss through Temporal** (workflow id = the key). Rejected: the interactive tool
   path is not a workflow, and queueing a cache lookup behind a worker would put a round trip on
   the path this change exists to keep free.

## Decision

Option 3. The hit path is unchanged: `cached_compute` reads the result first and returns, with no
claim and no extra round trip (p50 of a Postgres-backed hit, 4,000 interleaved calls on a pooled
connection: 1,550 µs before, 1,561 µs after). On a miss:

- **Claim.** One `INSERT … ON CONFLICT DO UPDATE … WHERE` on `calculation_claims`: a vacant key is
  inserted, a *lapsed* claim is replaced in place, a live one is left alone. Concurrent claimants
  serialise on the row, so exactly one gets a row back. The winner looks the result up once more
  (the previous holder may have finished between the miss and the claim), computes, persists, then
  deletes its claim and notifies.
- **Lease on the database clock.** `lease_until` is written and compared as `now()` in SQL; no
  process clock is read. The holder refreshes it three times per `calc_claim_lease_seconds`
  (default 30). A holder killed with `kill -9` stops refreshing; once the lease lapses exactly one
  waiter's conflicting `UPDATE` succeeds and it computes. A holder whose beat finds the claim gone
  (a stall longer than the lease) finishes anyway — the result is content-addressed, so the two
  writes agree — and counts `lost`.
- **Waiters.** A waiter registers with a per-event-loop `LISTEN calc_flight` connection shared by
  every waiter in the process (started by the first, closed with the last), then loops: look up the
  result, try to claim, read the row. It sleeps until the notification or one poll
  (`lease / 6`), whichever is first, so a missed notification costs one poll and never a hang. The
  in-process future stays the first level: eight tasks in one process produce one waiter.
- **A holder's failure is delivered, not retried, and keeps its retry class.** A computation that
  raises records its error on the row (`state = 'failed'`, text `refused|` or `failed|`, then the
  exception's class and the start of its message) and notifies. A `ValueError` (a `ChemclawError`
  such as `CalcToolError`, a validation error) is a deterministic refusal: every waiter on that
  attempt raises `PeerCalculationRefused`, listed non-retryable by name in
  `durable/publish.py::_BAD_DATA_TYPES`. Anything else raises `PeerComputationFailed`, retryable
  like the holder's own error. Without the carry, each waiter's activity would retry a refusal,
  claim over the failed row and recompute an hours-long deterministic failure. A waiter never
  claims over a failed row; a caller arriving afterwards starts afresh and replaces it. A
  *cancelled* holder is not a failure: its lease is shortened to one beat interval (a cooling-off,
  because its server-side call is still unwinding as the session closes) and the next waiter takes
  the key over then.
- **The heartbeat is bounded inside the lease.** Each beat has a hard bound of `lease / 6` (pool
  wait, connect and statement together), is retried after that bound when it fails, and re-asserts
  the claim when the database returns, because the statement matches the attempt, not the lease.
  A database outage shorter than the lease costs no duplicate; a longer one (a failover) lapses
  the claim and one waiter takes it over — the returning holder finds the claim gone, counts
  `lost`, finishes, and cannot remove its successor's row (release and failure match the attempt).
  Set `calc_claim_lease_seconds` above the failover time to avoid that duplicate.
- **A key is taken over only while a lease of the waiter's budget remains.** Starting a calculation
  under a deadline about to cancel it would hand the key on, one partial run after another; the
  waiter raises `PeerWaitTimeout` instead.
- **A waiter holds nothing.** Its own timeout or cancellation unregisters its wake-up and leaves the
  holder's work and row untouched. Its wait is bounded by `wait_seconds`, which `cached_remote`
  sets to the calculation's own request timeout (D-2026-08-26-a-request-timeout-bounds-the-wait-not-the-work),
  so a waiter never waits longer than a computer would be allowed; on expiry it raises
  `PeerWaitTimeout` and the work continues for the next caller.
- **Selection follows the session store.** Cross-process coordination is on for
  `session_store=postgres` and a store that offers `claims()`; the memory selection is unchanged
  (the in-process future is the whole answer there).

## Consequences

- Two real processes with eight concurrent callers each, missing one key: one computation (counter
  file), fifteen callers `was_cached=True`, no claim left behind
  (`tests/test_calc_single_flight.py`, with `tests/calc_flight_worker.py` as the second process).
- `kill -9` of the holder with three waiter processes and three waiter threads already blocked on
  it: exactly one computes; the result reached the first waiter 1.6 s after the kill at a 1.2 s lease
  (takeover is bounded by lease plus one poll; the test asserts exactly-once, not the time).
- Wake-up after the holder's release commits: 16–40 ms to the waiter returning, with the poll
  pushed out to 100 s so only `NOTIFY` could have woken it; that figure includes the waiter's
  re-read on a non-pooled connection.
- Cost: a `LISTEN` connection per process while any waiter exists — a dedicated connection outside
  the pool, so `pg_fleet_max_connections` does not count it (one per replica with a waiter) — and a
  claim, a read and a release per *miss* (never per hit). A waiter holds its calculation session
  open for the length of its wait: it counts in `chemclaw_calc_requests_in_flight` and in the
  server's `MCP_MAX_SESSIONS`, and a takeover after a long wait computes on that session. The
  failure text kept on the row is the retry class, the exception's name and up to 300 characters of
  its message, readable by any session asking for the same calculation; the cache is global, so
  that is no wider than the result itself.
- The table is transient (`infra/sql/125_calculation_claims.sql`): not swept on a clock, because a
  live row is a computation somebody awaits; the residue is a key-sized row per key whose last
  attempt died and was never asked for again.
- Metrics: `chemclaw_calc_claims_total{outcome}` (`won`, `awaited`, `taken_over`, `lost`,
  `peer_failed`, `wait_timed_out`) and `chemclaw_calc_claim_wait_seconds`.

Revisit when: the claim statements show in `chemclaw_db_query_duration_seconds{operation="calc_claim"}`
as a measured cost, or a deployment fronts Postgres with a transaction-mode pooler (which breaks
`LISTEN`; the poll fallback keeps correctness, and the wake-up then costs one poll).
