-- The outbox: what has been projected for publishing, and whether it got there
-- (D-2026-08-25-a-cache-is-not-a-record).
--
-- **Why an outbox rather than publishing inline.** A calculation that has finished is science this
-- system already owns; an external results database being down must never fail it, and must never
-- lose it either. Those two together are what an outbox is: the record is written here in the same
-- act that produces it, and a Temporal job drains it with retries. Publishing inline would force a
-- choice between the two — fail the job, or drop the result — and both are wrong.
--
-- **The unique index IS the idempotency.** `(sink, calc_ref, schema_version)` means a second
-- enqueue of a calculation already queued for a sink is a no-op, so the three enqueue call sites
-- do not have to coordinate, a retried Temporal activity cannot double-queue, and the backfill CLI
-- can be run twice. It carries `schema_version` because a contract bump is a genuine re-publish:
-- the same calculation under a new record shape is new information, not a duplicate.
--
-- **`document` is the projected record, not the raw payload.** Projection happens at enqueue time
-- rather than at drain time, deliberately: it is the step that can fail on a shape this release
-- cannot read, and failing there means failing next to the calculation that produced it, where the
-- context to diagnose it exists. A drain that projected would discover the problem hours later in
-- a background worker.
--
-- **Pruned, unlike `calculation_results` and `job_records`.** A delivered row is a receipt for
-- something that now lives in two places; keeping it forever would be a third copy of every result
-- this deployment has ever computed. `durable/retention.py` sweeps delivered rows only — a pending
-- or failed row is the only record that something has *not* been published, and deleting that
-- would turn an outage into a silent gap.
--
-- Applied by `make db-migrate` (idempotent).
CREATE TABLE IF NOT EXISTS result_publications (
    id             BIGSERIAL   PRIMARY KEY,
    -- Which enabled sink this row is destined for. One row per (sink, calculation): two sinks mean
    -- two rows, so one destination being down cannot hold up another.
    sink           TEXT        NOT NULL,
    calc_ref       TEXT        NOT NULL,
    -- The canonical record, already projected. See above.
    document       JSONB       NOT NULL,
    -- The contract version `document` was built against, so a consumer of this table — and the
    -- uniqueness rule above — can tell a re-publish from a duplicate.
    schema_version INTEGER     NOT NULL DEFAULT 1,
    state          TEXT        NOT NULL DEFAULT 'pending',
    attempts       INTEGER     NOT NULL DEFAULT 0,
    -- The last failure's message, kept so an operator reading this table sees why rather than only
    -- that. Overwritten each attempt: the current reason is what matters, and a history of
    -- identical connection errors is noise.
    last_error     TEXT        NOT NULL DEFAULT '',
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at   TIMESTAMPTZ,

    CONSTRAINT result_publications_state_known
        CHECK (state IN ('pending', 'delivered', 'failed'))
);

-- The idempotency rule itself. A unique index rather than a primary key because the surrogate `id`
-- is what the drain claims rows by, and ordering a claim by a content hash would scatter the scan.
CREATE UNIQUE INDEX IF NOT EXISTS result_publications_identity
    ON result_publications (sink, calc_ref, schema_version);

-- The drain's own scan: oldest pending first, so a backlog drains in the order it accumulated and
-- a burst of new results cannot starve what was already waiting. Partial, because a delivered row
-- is never scanned again and a deployment that has been publishing for a year has vastly more of
-- those than of pending ones.
CREATE INDEX IF NOT EXISTS result_publications_pending
    ON result_publications (sink, enqueued_at)
    WHERE state = 'pending';

-- Retention's scan, and an operator's "what has gone out lately".
CREATE INDEX IF NOT EXISTS result_publications_delivered
    ON result_publications (delivered_at)
    WHERE state = 'delivered';
