-- A calculation miss is claimed before it is computed, so two pods that miss one key do not both
-- compute it (D-011 extended: a result being computed by someone else is awaited, not recomputed).
--
-- The in-process future in `science/calc/store.py` joins concurrent misses inside one process only.
-- Measured with two processes missing the same key, both computed, and the second write replaced the
-- first: for a CREST search that is hours of duplicated CPU.
--
-- One row per key being computed, never a result. `attempt` is a fresh id per
-- attempt, so a takeover is distinguishable from a retry by the same pod). `lease_until` is the
-- holder's heartbeat on the DATABASE clock: every comparison against it is `now()` in SQL, so a
-- process clock that is minutes off cannot make a live claim look lapsed or a dead one look live.
-- A lapsed claim is taken over in place by exactly one claimant (the conflicting UPDATE is
-- serialised on the row).
--
-- `state` is `running` or `failed`. A holder whose computation raised records the failure here and
-- keeps the row, so the waiters on that attempt receive it rather than each recomputing the same
-- failing hours-long search; the next claim on the key replaces a failed row. A holder that is
-- cancelled deletes its row instead (the work was abandoned, not refused), and a waiter takes over.
-- No CHECK on `state`, for 118's reason: its one writer and reader is `science/calc/flight.py`.
CREATE TABLE IF NOT EXISTS calculation_claims (
    key         TEXT        PRIMARY KEY,
    attempt     TEXT        NOT NULL,
    state       TEXT        NOT NULL DEFAULT 'running',
    error       TEXT        NOT NULL DEFAULT '',
    lease_until TIMESTAMPTZ NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
