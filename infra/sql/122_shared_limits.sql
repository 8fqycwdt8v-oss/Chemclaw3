-- The two limits a replica used to hold in its own memory: a principal's request budget and the
-- number of turns the deployment runs at once.
--
-- With `maxReplicas: 6` the in-process token bucket admitted six times the configured rate, and the
-- per-process turn cap six times the permitted concurrency; neither could be stated for the
-- deployment, only per pod. Both now live where every replica can see them.

-- A principal's token bucket, one row each. `tokens` is the balance as of `refilled_at`; the refill
-- is computed lazily by whoever spends next, in the same statement that spends
-- (`api/rate_limit_store.py`), so no process reads a balance and writes it back. `allowed` records whether the last spend was
-- granted, because a single `INSERT … ON CONFLICT … RETURNING` cannot otherwise tell the caller
-- whether the token it asked for was taken. The clock is the database's, so replicas whose own
-- clocks differ cannot grant each other extra refills.
--
-- A bucket idle long enough to be full carries no information (an absent row starts full), so the
-- table is bounded by the principals served and a sweep loses nothing. For the same reason it is
-- UNLOGGED: every authenticated request writes here, a write-ahead record would add a log flush to
-- each of them, and what a crash or a failover discards, a full bucket, is exactly what a missing
-- row means. `fillfactor` leaves room for the in-place updates, which keeps them HOT. (Made
-- unlogged by an `ALTER` while the table is empty, which is free.)
CREATE TABLE IF NOT EXISTS request_buckets (
    principal_id TEXT             PRIMARY KEY,
    tokens       DOUBLE PRECISION NOT NULL,
    refilled_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    allowed      BOOLEAN          NOT NULL DEFAULT true
) WITH (fillfactor = 50);
ALTER TABLE request_buckets SET UNLOGGED;

-- A turn that has taken one of the deployment's concurrent-turn slots. The lease the turn already
-- holds on its session (`session_turns`: holder, `expires_at`, refreshed while the turn streams) is
-- the slot's lease, so a pod that dies mid-turn frees its slot when the lease lapses and no second
-- lease table exists. `admitted` is set by the admission statement, which runs under an advisory
-- lock so two replicas cannot both count the last free slot, and starts false so the claim a turn
-- takes while it waits for a slot, or a maintenance hold, is not counted as running.
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS admitted BOOLEAN NOT NULL DEFAULT false;
