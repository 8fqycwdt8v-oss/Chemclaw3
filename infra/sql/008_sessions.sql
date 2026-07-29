-- Durable conversation history (plan Phase F3, agents.session_store.PostgresHistoryProvider).
-- One append-only row per stored message in a session, so a chat thread survives a pod restart:
-- a fresh process over the same database resumes the session by reading its rows in `id` order.
--
-- This is the *conversation* layer, distinct from Temporal job state (D-002) and from the
-- calculation cache. `message` holds the MAF `Message.to_dict()` payload verbatim (role + contents
-- + additional_properties), reloaded via `Message.from_dict()`; the store does not interpret it, so
-- a MAF message-shape change is a value change, not a schema change. Ordering is the monotonic
-- `id` — the provider appends the turn's messages and reads them back in insertion order.
CREATE TABLE IF NOT EXISTS session_messages (
    id         BIGSERIAL   PRIMARY KEY,
    session_id TEXT        NOT NULL,
    message    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Load one session's thread in order (the provider's only read path).
CREATE INDEX IF NOT EXISTS session_messages_session_idx ON session_messages (session_id, id);
