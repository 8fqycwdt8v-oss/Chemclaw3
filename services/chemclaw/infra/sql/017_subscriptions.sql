-- Standing queries (gap IDEA-1).
--
-- The system was strictly pull. It already had durable sessions, a push-back mailbox, per-user
-- identity and fingerprint search — every ingredient for *push* — and used none of them that way,
-- so a chemist learned about a relevant new experiment only by asking again at the right moment.
--
-- One row per standing query. `last_seen_at` is the watermark: the digest job reports only what
-- appeared since the subscriber was last told, so a daily digest is a digest and not a re-send of
-- the whole corpus. Deliberately per-user rather than per-session: a standing query outlives the
-- conversation that created it, which is the entire point.
CREATE TABLE IF NOT EXISTS subscriptions (
    id           BIGSERIAL   PRIMARY KEY,
    owner        TEXT        NOT NULL,
    query        TEXT        NOT NULL,
    note_type    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The same person must not accumulate duplicate standing queries by asking twice.
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_identity
    ON subscriptions (owner, query, coalesce(note_type, ''));

CREATE INDEX IF NOT EXISTS subscriptions_owner ON subscriptions (owner);
