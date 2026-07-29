-- Durable session ownership (plan Phase F3 follow-up, agents.session_store.SessionOwnerStore).
-- The front door holds live AgentSession handles in an in-process LRU that a pod restart wipes.
-- Without a durable record of *who owns which session id*, a returning client's id is unknown after
-- a restart, so it is forced onto a brand-new session — orphaning its durable history
-- (session_messages) and any unconsumed job push-back (session_events). This table is that record:
-- one identity row per session, written once at creation, so the restarted front door can look the
-- owner up, authorize a reattach, and rebuild the live handle over the same durable history.
--
-- Distinct from session_messages (append-only, many rows per session): this is the single
-- security-relevant fact — the owner — that the in-memory LRU lost. `owner` is nullable so a
-- dev/system session with no Entra oid (the shared principal) is still recorded and reattachable.
CREATE TABLE IF NOT EXISTS session_owners (
    session_id TEXT        PRIMARY KEY,
    owner      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
