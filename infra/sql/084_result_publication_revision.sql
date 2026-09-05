-- A calculation is one record with N publications, and the outbox has to be able to carry them
-- (D-2026-09-05-an-outbox-row-is-a-record-and-its-publications).
--
-- `record.py` has always said it: "two chemists asking the same question share one `calc_ref` ...
-- N publications per record is the correct cardinality", and `schema/result-store/001_core.sql`
-- keys `calculation_publication` on `(calc_ref, tenant_id, session_id, job_id)` precisely so a
-- site can hold several. The outbox could not deliver them. Its identity index is
-- `(sink, calc_ref, schema_version)` and its insert was `ON CONFLICT DO NOTHING`, so the *row* the
-- second enqueue dropped carried the second chemist's `publications` with it. Measured: alice then
-- bob for one `calc_ref` gave `alice_rows=1 bob_rows=0`, one stored document naming alice.
--
-- The identity index is right and stays: idempotency is what lets three enqueue call sites need no
-- coordination, a retried Temporal activity not double-queue, and the backfill CLI be re-runnable.
-- What was wrong is that the enqueue *identity* was read as `calc_ref` when it is "this
-- calculation, for this sink, on behalf of this requester". So the row is now merged rather than
-- dropped: a publication the stored document does not already contain is appended and the row is
-- returned to the queue, and a publication it does contain changes nothing at all — which is every
-- replay the `ON CONFLICT` clause exists for.
--
-- **`revision` and `claimed_revision` are what make that safe against the drain.** `claim` commits
-- before anything is delivered (a delivery may take the better part of a minute and must not hold
-- a row lock across it), so an enqueue can merge a publication into a row that is already in
-- flight. Without a guard `mark_delivered` would then mark the *merged* row delivered having sent
-- the un-merged document, and the second chemist's publication would be lost permanently — the
-- same defect this migration exists to fix, arriving through the back door. The claim snapshots
-- the revision it is about to deliver; `mark_delivered` only settles a row whose revision has not
-- moved since, and one that has moved stays pending for the next pass.
ALTER TABLE result_publications ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE result_publications
    ADD COLUMN IF NOT EXISTS claimed_revision INTEGER NOT NULL DEFAULT 0;

-- The dead-letter read, which was a sequential scan of the whole table once per drain pass.
--
-- `publish/outbox._DEAD_LETTERED` measured it and prescribed exactly this index: `Parallel Seq
-- Scan` over 200,000 rows to find 5,136 failed ones, 2,478 buffers, ~20 ms, vacuumed or not. That
-- comment judged 20 ms per pass not worth an index "unless the table reaches millions of rows" —
-- and then named what guarantees it will: retention prunes `delivered` rows only, a `failed` row
-- is kept forever by design, and `retention_result_publications_days` defaults to 0 (disabled), so
-- the scan grows with everything this deployment has ever published *and* everything it has ever
-- retired. An index whose cost is one entry per retired row is the cheaper side of that trade.
CREATE INDEX IF NOT EXISTS result_publications_failed
    ON result_publications (sink)
    WHERE state = 'failed';
