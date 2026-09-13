# D-2026-09-13-a-deadlock-victim-is-chosen-by-postgres-not-by-the-caller — both lock orders stay, and both sides retry

A `BACKLOG.md` row has recorded, since 2026-08-28, that `SessionOwnerStore.delete_session` and
`retention._DELETE_SESSIONS` take `session_owners` and `session_turns` in opposite orders, that
ordering them consistently was examined and is not available, and that the resulting deadlock is "one
statement wide, self-healing on the retention side (a Temporal activity retries) and **has not been
reproduced**". Its conclusion — *keep both orders* — is right. Two of its other clauses are not.

## What was measured

The two orders, spelled as the two modules spell them, against a migrated schema on two real
connections: take the first table's row lock, wait for the other side to hold its own, then reach for
the second.

**The deadlock fired on 16 of 16 attempts.** "Has not been reproduced" was a statement about who had
tried.

**Which side Postgres abandons is a coin flip.** Over those 16 runs:

| victim | runs |
| --- | --- |
| the route's order (`session_turns` then `session_owners`) | **9** |
| the retention pass's order (`session_owners` then `session_turns`) | **7** |

Postgres kills whichever transaction's lock request closes the cycle, which is a property of
scheduling rather than of either caller. So "self-healing on the retention side" covers about half
the occurrences. The other half is a chemist's `DELETE /sessions/{id}`, and that path had **no retry
at all** — `delete_session` runs one transaction and a `DeadlockDetected` propagates out of the route
as a 500.

## The decision

**Keep both orders.** The row's own analysis stands and is not re-litigated here: erasure must remove
session-scoped rows before `session_owners` because its statements re-resolve through a subquery over
that table; `_DELETE_SESSIONS` must take the ownership row first because the lease deletion reads
that `DELETE`'s `RETURNING`, which is what makes "a lease goes only if its ownership row went" true
rather than intended. Reversing either trades a deadlock window for a correctness bug.

**Retry the victim, on the side that could not.** `delete_session` now tries its transaction up to
`pg_deadlock_retries + 1` times, catching `DeadlockDetected` and `SerializationFailure`. Retrying is
the standard remedy for a cycle that cannot be ordered away, and it is safe here for a reason
specific to this transaction: every statement in it is an idempotent
`DELETE ... WHERE session_id = ...`, so an attempt that runs after the other side committed deletes
what is left — nothing — and still answers with the counts from the tables it did clear.

`pg_deadlock_retries` defaults to **2**, not 1: the victim of the retry's *own* collision would
otherwise be the answer. **0** restores the previous behaviour for a deployment that would rather see
the error than have it absorbed.

The retry wraps the whole transaction (`_delete_session_once`), because a deadlock abort rolls the
transaction back and resuming inside it is not a thing that exists.

**Nothing changes on the retention side.** Its activity already retries — `DeadlockDetected` is not in
`publish._BAD_DATA_TYPES`, so `BAD_DATA_RETRY` does retry it, which is the one clause of the row that
was both load-bearing and true.

## What was not done

**No advisory lock and no forced lock order through an explicit `SELECT ... FOR UPDATE`.** Either
would make the cycle impossible rather than survivable, and both put a new serialization point
between a chemist's delete and an hourly sweep — on the table every session-scoped read starts from.
Retrying costs one extra transaction on a collision measured to be one statement wide; a shared lock
costs every delete, always, to avoid a collision that resolves itself in milliseconds.

**The route's 500 on an exhausted retry is left as a 500.** Three aborts in a row on a
one-statement-wide window is not a transient to paper over, and a chemist retrying the delete is the
honest next step.

## What keeps it true

| property | test |
| --- | --- |
| the two real orders do deadlock, and **exactly one** of the two transactions is aborted — asserted, because two commits would mean no cycle formed and the run is evidence about nothing | `tests/test_session_store.py::test_the_two_session_delete_orders_really_do_deadlock` |
| a delete aborted as the victim is tried again, answers, and has actually removed the ownership row | `tests/test_session_store.py::test_deleting_a_session_survives_being_the_deadlock_victim` |
| the ownership row is still the last table the route's sweep touches | `tests/test_retention.py::test_the_ownership_row_is_the_last_table_the_sweep_touches` |
| `DeadlockDetected` is still counted and named rather than collapsed into "unavailable" | `tests/test_db_pool.py`, `core/db._failure_kind`'s own ordering |

**The abort is raced in the first test and injected in the second, and the split is deliberate.**
Which transaction Postgres kills is decided by which lock request closes the cycle, so external
orchestration can reliably make the *other* side the victim and cannot reliably make the route's side
the victim — the window in which the route holds `session_turns` and has not yet asked for
`session_owners` is inside one transaction and microseconds wide. An earlier single test that tried to
do both at once **passed with the retry removed, six times out of six**: it never formed the cycle at
all, because it released the other side before the delete had taken any lock. So the cycle is proven
against real connections, and the response to losing it is proven against the real exception class at
the real transaction boundary.

Two mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| `delete_session` makes one attempt (the pre-fix behaviour) | red — `test_deleting_a_session_survives_being_the_deadlock_victim` |
| the reproduction runs the route's order against *itself* instead of against the prune's | red on its own named assertion — *"the two delete orders did not deadlock, so this run is evidence about nothing"* — which is what makes the passing form mean something |
