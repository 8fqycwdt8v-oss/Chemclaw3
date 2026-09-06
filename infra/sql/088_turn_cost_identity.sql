-- A cost row is identified by the turn, not by the label the caller put on it
-- (D-2026-09-06-an-id-a-caller-chooses-is-not-a-key).
--
-- `033` keyed this table on `correlation_id` — "already unique per turn, already the key
-- `audit_events` is keyed on, and already stamped into every log line" — and every word of that is
-- true of an id this system mints. It is not true of the id it *adopts*:
-- `api/middleware._request_correlation_id` takes an inbound `X-Chemclaw-Correlation-Id` whenever it
-- matches `[A-Za-z0-9_-]{8,64}`, deliberately, so a chemist's click is traceable from the browser
-- through the ingress into this pod. `api/runner.run_turn` then keys the ledger on it and
-- `agent/turn_cost_store` writes `ON CONFLICT (correlation_id) DO UPDATE`. Measured 2026-09-06:
-- two turns for one actor, 900,000 and 1,000 input tokens, sent under one fixed header, left
-- **one row reading 1,000** — the ledger erased by the party it bills, with
-- `chemclaw_tokens_total` and `api/budget.py` still seeing both turns, so the two records disagree
-- by exactly what was overwritten.
--
-- `turn_id` is minted by this system for each turn record and never crosses the wire in either
-- direction (`core/turn_cost.TurnCost.turn_id`). Keying on it keeps **both** properties the upsert
-- was chosen for: a retried write of the same record still replaces rather than doubles, and the
-- join to `audit_events`, `session_messages` and every log line is unchanged, because
-- `correlation_id` stays on the row — as an ordinary indexed column, which is what it always was
-- semantically. Two turns that share a correlation id are now two rows that share a correlation id,
-- which is the truth about them.
--
-- Rows written before this migration are backfilled from their own `correlation_id`: it *was*
-- their identity, uniqueness is guaranteed by the primary key being dropped in the next statement,
-- and no row changes meaning.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS turn_id TEXT;

UPDATE turn_costs SET turn_id = correlation_id WHERE turn_id IS NULL;

-- `ADD PRIMARY KEY` builds a unique index under an ACCESS EXCLUSIVE lock, as `041`'s and `056`'s
-- did — seconds on a table of one row per turn, once, applied by `make db-migrate`. It also ends
-- the rollback in both directions: the previous image's `ON CONFLICT (correlation_id)` no longer
-- matches a constraint, and its INSERT omits a column the primary key makes NOT NULL. What an
-- operator does instead is in the ADR.
ALTER TABLE turn_costs DROP CONSTRAINT IF EXISTS turn_costs_pkey;
ALTER TABLE turn_costs ADD PRIMARY KEY (turn_id);

-- The join `033` got for free from the primary key, kept explicitly now that the key has moved.
-- Non-unique on purpose: a template run books one row per `agent` step under one run correlation
-- id (`durable/template_activities._book_step_spend`), and a client is free to reuse its own.
CREATE INDEX IF NOT EXISTS turn_costs_correlation_id_idx ON turn_costs (correlation_id);

COMMENT ON COLUMN turn_costs.turn_id IS
    'This system''s own id for the turn record — the primary key, minted server-side and never '
    'read off a request. `correlation_id` beside it is the join to the audit trail and the logs '
    'and may be a value the caller chose, which is why it is no longer the key (migration 088).';
