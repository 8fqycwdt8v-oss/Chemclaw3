-- The index the retention sweep has always needed on `session_events` (DARK-5).
--
-- `chemclaw.durable.retention` prunes this table with
-- `WHERE consumed_at IS NOT NULL AND created_at < now() - interval`, and migration 009 gave it one
-- index: `(session_id, id) WHERE consumed_at IS NULL` — the *tailer's* read, whose partial
-- predicate is the exact complement of the sweep's. So the delete has never had an index it could
-- use and has always been a sequential scan.
--
-- That is invisible until it isn't. The sweep runs under `pg_statement_timeout_seconds` (30 s by
-- default), so the failure mode is not "slow": past the size where the scan exceeds the timeout the
-- job starts failing *every* run, permanently, and the table it was meant to bound then grows
-- without limit — with the timeout, not the growth, as the only symptom. The gap is the same one
-- migration 022 closed for `session_messages`; this table was simply not looked at then.
--
-- Partial on `consumed_at IS NOT NULL` so it indexes only the rows the sweep can delete, which is
-- also what keeps it small: an unconsumed row is one the tailer still owns, and it is never a
-- candidate. Applied by `make db-migrate`.
CREATE INDEX IF NOT EXISTS session_events_consumed_idx
    ON session_events (created_at)
    WHERE consumed_at IS NOT NULL;
