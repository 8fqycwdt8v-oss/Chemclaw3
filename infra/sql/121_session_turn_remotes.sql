-- A running turn is reached from any replica, through rows its own process polls
-- (D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica).
--
-- Measured with two front-door processes on one database: a turn started on A answered 404 "no
-- turn is running for this session" to `GET /sessions/{id}/turn/stream` and to
-- `POST /sessions/{id}/turn/stop` on B, and ran to its answer as if nobody had pressed Stop. The
-- pump, its readers and its cancel live in A's memory, and the BFF reaches the front door through
-- the Service, so with two replicas half of every reattach and every Stop landed on the wrong one.
--
-- `session_turns.actor` is who sent the running turn — the in-process lease already knows it, and a
-- replica that does not hold the turn needs it to apply the stop route's rule (a member stops only
-- their own turn, the owner any) before asking the holder to stop. Nullable: a claim taken by the
-- previous image carries none, and the reader treats an unknown sender as somebody else's turn.
ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS actor TEXT;

-- One replica's standing request to the process holding a session's turn: follow it (`watch`) or
-- stop it (`stop`, `unload_stop`). `holder` names the claim it addresses, so a request outlives
-- nothing — the next turn on the session has another holder and never sees it. `state` is the
-- holder's answer, written by the holder; `lease_until` is the asker's heartbeat, so a request whose
-- asking process died stops being served after one lease. No CHECK on the two vocabularies, for
-- 118's reason: their one writer and reader is `agent/turn_remotes.py`.
--
-- **Cascades from `session_owners`**, for `session_turn_queue`'s reason: deleting a session, the
-- retention sweep and an owner's erasure take its requests with it. `actor` is the asker, and a
-- leaver's own rows go by actor (`agent/leaver.py`).
CREATE TABLE IF NOT EXISTS session_turn_remotes (
    id             TEXT        PRIMARY KEY,
    session_id     TEXT        NOT NULL REFERENCES session_owners (session_id) ON DELETE CASCADE,
    holder         TEXT        NOT NULL,
    kind           TEXT        NOT NULL,
    actor          TEXT        NOT NULL,
    state          TEXT        NOT NULL,
    correlation_id TEXT,
    lease_until    TIMESTAMPTZ NOT NULL
);

-- The holder's one read per poll: the live requests addressed to the turns it holds.
CREATE INDEX IF NOT EXISTS session_turn_remotes_turn_idx ON session_turn_remotes (session_id, holder);

-- An erasure removes a departing person's requests in sessions somebody else owns.
CREATE INDEX IF NOT EXISTS session_turn_remotes_actor_idx ON session_turn_remotes (actor);

-- The frames a holder relays to one remote view, consumed (deleted) by the view as it reads them.
-- `frame` is the SSE frame exactly as the holder's own readers receive it; `NULL` ends the view.
-- Goes with its request by cascade, so a view that ends — or a session that is deleted — leaves
-- nothing behind.
CREATE TABLE IF NOT EXISTS session_turn_frames (
    seq       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    remote_id TEXT   NOT NULL REFERENCES session_turn_remotes (id) ON DELETE CASCADE,
    frame     JSONB
);

CREATE INDEX IF NOT EXISTS session_turn_frames_remote_idx ON session_turn_frames (remote_id, seq);
