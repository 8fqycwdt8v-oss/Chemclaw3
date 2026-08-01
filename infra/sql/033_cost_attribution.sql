-- What a turn and a job actually cost, kept where the question can be asked of it.
--
-- Spend was measured in two places and attributable from neither. `chemclaw_tokens_total` and its
-- four siblings carry one label, `profile` — so "what is this deployment spending" is answerable and
-- "what did this team spend last quarter" is not. That is not an oversight in the label set: the
-- metric registry caps a counter at 64 label series and refuses beyond (D-152), because a label
-- value is attacker-influenced and an unbounded map keyed on one is a memory leak this codebase has
-- already fixed three times. An Entra `oid` is exactly such a key. Per-user attribution needs
-- unbounded cardinality and quarters of history, and that is a database's job, not Prometheus's.
--
-- The other place was `api/budget.py`, which meters tokens per user *to refuse a turn* — in process,
-- reset on restart, and LRU-evicted under a cap. A guard, deliberately, not a ledger.
--
-- So this table is the ledger: one row per completed turn, carrying the identity the metric cannot
-- and joining to `audit_events` on `correlation_id` — the same join `chemclaw explain` walks and the
-- log lines now carry. "What did team X cost" becomes a GROUP BY.
CREATE TABLE IF NOT EXISTS turn_costs (
    -- The turn's correlation id: already unique per turn, already the key `audit_events` is keyed
    -- on, and already stamped into every log line. Primary key, so a retried write is an upsert
    -- rather than a double-count — the one arithmetic error a cost ledger must not make.
    correlation_id     TEXT        PRIMARY KEY,
    session_id         TEXT        NOT NULL DEFAULT '',
    -- The Entra oid, or the static dev actor. The column the whole table exists for.
    actor              TEXT        NOT NULL DEFAULT '',
    -- '' is spelled `default` by the writer, matching the metric's label, so a sum here and a sum
    -- there answer the same question the same way.
    profile            TEXT        NOT NULL DEFAULT '',
    input_tokens       BIGINT      NOT NULL DEFAULT 0,
    output_tokens      BIGINT      NOT NULL DEFAULT 0,
    -- Priced differently from the two above, which is the only reason they are separate columns.
    cache_read_tokens  BIGINT      NOT NULL DEFAULT 0,
    cache_write_tokens BIGINT      NOT NULL DEFAULT 0,
    -- Wall-clock, including the time a turn spent waiting on a durable job. Not a cost on its own;
    -- it is what makes "expensive because it thought hard" separable from "expensive because it
    -- looped".
    duration_seconds   DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Whether the turn reached an answer. A turn torn down by a disconnect still spent its tokens,
    -- and excluding those rows would under-report exactly the runaway this ledger exists to find.
    completed          BOOLEAN     NOT NULL DEFAULT TRUE,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The two orders a cost question is asked in: "this person/team over this period" and "the whole
-- deployment over this period".
CREATE INDEX IF NOT EXISTS turn_costs_actor_recorded_at_idx ON turn_costs (actor, recorded_at DESC);
CREATE INDEX IF NOT EXISTS turn_costs_recorded_at_idx ON turn_costs (recorded_at DESC);

-- The compute half of the same question. `job_records` says what ran, on what, and why (D-157) —
-- and said nothing about how much of the cluster it took, so a two-second xTB call and a six-hour
-- DFT run were the same row shape and the same single increment of
-- `chemclaw_jobs_started_total`. On the most expensive thing this system does, "how many" was the
-- only number anyone had.
--
-- Wall-clock seconds of the whole run, measured by the wrapper workflow across the child. Not
-- node-hours: the parallelism belongs to the launcher, and no launcher in this repository reports
-- it back yet (see docs/planning/BACKLOG.md). Runtime is the honest measurable today, and it is the
-- factor node-hours multiplies.
ALTER TABLE job_records ADD COLUMN IF NOT EXISTS runtime_seconds DOUBLE PRECISION NOT NULL DEFAULT 0;
