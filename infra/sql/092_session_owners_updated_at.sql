-- The session list's sort key stops being derived, because deriving it costs one index probe per
-- session the owner has **ever** created, on every page.
--
-- `043_session_listing.sql` decided the other way and said why: "a mirrored `updated_at` on
-- `session_owners` would be a second write per turn that can silently fall out of step with the
-- first". That argument is sound and this file does not overturn it — it prices it. What 043 had
-- no number for is the cost of the alternative, and the number is the reason this column exists.
--
-- `session_store._OWNER_LIST` joined `LATERAL (SELECT max(created_at) … WHERE session_id =
-- o.session_id)` and ordered by its output, so the planner had to evaluate the lateral for every
-- one of the owner's sessions before it could discard any. Measured on this schema, one owner, one
-- message per session, warm cache:
--
--     2 sessions      0.4 ms
--     600 sessions    5.3 ms
--     6 000 sessions  47.6 ms   18 051 buffers
--     20 000 sessions 155.0 ms  60 154 buffers
--
-- Linear, ~8 µs per lifetime session, and **the keyset cursor does not help**: page 2 measured
-- 49.4 ms against page 1's 47.6 ms at 6 000, because the cursor predicate is also on the lateral's
-- output and cannot prune the loop. Nothing bounds the row count either — `session_owners` is
-- pruned only by `retention_session_messages_days`, which ships at 0, and the companion UI mints an
-- ownership row on the first keystroke, so every abandoned draft is another loop iteration for the
-- rest of that person's life.
--
-- What the mirror costs is one `_OWNER_TOUCH` in the transaction that appends the turn's messages:
-- measured over a 2,000-message session, 0.068 ms and 6 buffers, against a turn that has just
-- spent seconds in a model.
--
-- **What answers 043's objection is not care, it is that the column has one definition and two
-- writers, both of them statements in `agent/session_store.py`.** `updated_at` *is*
-- `max(session_messages.created_at)`, computed by that expression at both sites — `_OWNER_INSERT`,
-- which is also the fork's (`agent/session_fork.py` imports it and inserts its ownership row after
-- the copied transcript), and the touch that follows `_INSERT` in `save_messages`, in the same
-- transaction as the rows it summarises. `tests/test_session_store.py` scans `src/` for a third
-- writer of `session_messages` and fails on one, and asserts the equality directly after driving
-- both writers. A drift the mirror could still take is deletion — `durable/retention.py` prunes
-- expired message rows — and it cannot show: rows are pruned oldest-first, so either the newest
-- message survives and the column is exactly right, or none does and the `EXISTS` arm in
-- `_OWNER_LIST` drops the session from the listing exactly as the old lateral join did.
--
-- **Rolling back to the previous image keeps working and leaves this column behind.** The pre-092
-- image ignores it and derives the sort key as before, so the listing is correct throughout; what
-- it does not do is maintain the column, so sessions that take their first turn during the
-- rollback window come back with `updated_at IS NULL` and are missing from the listing until they
-- are spoken in again (the next turn's touch repairs a session exactly). The remedy is this file's
-- own backfill, re-run by hand:
--
--     UPDATE session_owners o SET updated_at = (
--         SELECT max(created_at) FROM session_messages m WHERE m.session_id = o.session_id);
--
-- Nullable, and NULL is meaningful rather than missing: it is a session that has never held a
-- message, which is what 043's `ON m.updated_at IS NOT NULL` already dropped from the listing and
-- what `013`/`021`/`043` mean by every other nullable column on this row.
ALTER TABLE session_owners ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- The backfill is the derivation, run once. A correlated `max()` per row over
-- `session_messages_session_recent_idx` (043) — the same probe the listing used to pay per page,
-- paid once per session here instead.
UPDATE session_owners o
   SET updated_at = (SELECT max(created_at) FROM session_messages m WHERE m.session_id = o.session_id)
 WHERE o.updated_at IS NULL;

-- The index the rewritten listing walks: owner first (the predicate), then the sort key in the
-- order the statement asks for it, so `LIMIT 100` stops after 100 rows instead of after all of
-- them. `session_id DESC` is the third column because the cursor compares the pair
-- `(updated_at, session_id)` — a strict total order is what makes "everything after this row"
-- unambiguous, and an index that stops at `updated_at` would leave the tiebreak to a sort.
--
-- `046`'s `session_owners_owner_idx` stays. It is a prefix of this one and therefore redundant for
-- this statement, but `leaver._SESSION_SCOPED`'s `WHERE owner = ANY(...)` and
-- `retention._prune_session_owners` also read this table by owner, and dropping an index that
-- other measured plans reach is a separate decision from adding this one — `DROP INDEX` is in
-- `tests/test_migrations_are_additive.py`'s rollback bucket for exactly that reason.
CREATE INDEX IF NOT EXISTS session_owners_owner_updated_idx
    ON session_owners (owner, updated_at DESC, session_id DESC);
