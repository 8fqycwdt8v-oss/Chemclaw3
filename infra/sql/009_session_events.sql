-- Job→session push-back channel (plan Phase F3-T2, agents.session_events).
-- A durable mailbox from the job side (a completing Temporal workflow) to the conversation side
-- (the front-door service): the worker appends a row, the service tails unconsumed rows for a
-- session and wakes it — appending the result and flipping the `awaiting` todo — so a long job's
-- result reaches the chat with no client polling. `consumed_at` marks a row the service has already
-- delivered, so a restarted tailer neither replays nor drops it.
--
-- Payload is opaque JSONB (e.g. a job id + typed result); the channel does not interpret it. Kept
-- separate from Temporal's own state — this carries only the *notification*, durability stays in
-- Temporal (D-002).
CREATE TABLE IF NOT EXISTS session_events (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at TIMESTAMPTZ
);

-- The tailer's read path: a session's not-yet-consumed events in arrival order.
CREATE INDEX IF NOT EXISTS session_events_unconsumed_idx
    ON session_events (session_id, id)
    WHERE consumed_at IS NULL;
