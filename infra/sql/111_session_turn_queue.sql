-- The messages waiting for a session's running turn to end
-- (D-2026-09-27-a-queued-message-waits-in-its-senders-request).
--
-- **Order, not content.** A row is a place in one session's line: which session, whose message, a
-- ticket that orders it and a lease that proves its waiter is still alive. The message text and the
-- sender's roles are deliberately absent — the waiting happens in the sender's own request, which
-- holds both and runs the turn with the principal its own token established. So nothing here can
-- run a message, and nothing here outlives the token that authorized it.
--
-- **A lease, like `session_turns`.** A waiter refreshes `lease_until` each time it asks for its
-- position; a waiter whose process died stops refreshing, stops counting as ahead of anybody once
-- the lease lapses, and is swept by the next enqueue on its session. The ticket is an identity
-- column, so "first in line" is the order the database admitted the rows in.
--
-- **Cascades from `session_owners`**, for `session_members`' reason (`110_shared_sessions.sql`):
-- deleting a session, the retention sweep's `_prune_session_owners` and an owner's erasure take its
-- line with it, and a waiter reads its vanished ticket as its message having been withdrawn. A
-- sender's own rows in sessions somebody else owns are erased by `agent/leaver.py`.
--
-- **No default on `sender`** (`tasks/lessons.md` rule 89): a new table, no existing row for a
-- default to make a claim about, and a place in line with nobody's name on it means nothing.
CREATE TABLE IF NOT EXISTS session_turn_queue (
    ticket      BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  TEXT        NOT NULL REFERENCES session_owners (session_id) ON DELETE CASCADE,
    sender      TEXT        NOT NULL,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ NOT NULL
);

-- "Who is ahead of me in this session" — every read and the per-session sweep go by session, in
-- ticket order.
CREATE INDEX IF NOT EXISTS session_turn_queue_session_idx ON session_turn_queue (session_id, ticket);

-- An erasure removes a departing person's places in sessions somebody else owns.
CREATE INDEX IF NOT EXISTS session_turn_queue_sender_idx ON session_turn_queue (sender);
