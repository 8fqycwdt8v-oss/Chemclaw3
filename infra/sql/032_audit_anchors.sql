-- RETIRED. Signed high-water anchors over the audit chain — how many rows the trail held at a
-- moment, its tip hash, and an HMAC over all of it — because a *trailing* deletion (which is what a
-- point-in-time restore is) left the surviving rows chaining cleanly and so was the one alteration
-- the chain by itself could not see.
--
-- The chain and the anchors were built for a regulated deployment and have been removed. Nothing
-- writes this table; the schema is forward-only, so the statements below stay and re-running them
-- on an existing database is a no-op.
CREATE TABLE IF NOT EXISTS audit_anchors (
    id            BIGSERIAL PRIMARY KEY,
    taken_at      TIMESTAMPTZ  NOT NULL,
    -- What the trail held at `taken_at`. `row_count` and `max_event_id` are separate numbers on
    -- purpose: they disagree when rows were deleted *and* new ones appended, which a single
    -- counter would hide.
    row_count     BIGINT       NOT NULL CHECK (row_count >= 0),
    max_event_id  BIGINT       NOT NULL CHECK (max_event_id >= 0),
    -- The tip's `row_hash`, so a restored trail of the right length but different content is
    -- caught too. Empty only for an anchor over an empty trail.
    tip_hash      TEXT         NOT NULL,
    chain_version INTEGER      NOT NULL,
    signature     TEXT         NOT NULL,
    -- Set only on an anchor written by a deliberate `--reseal` after a restore, naming who
    -- accepted the gap and why. A trail may be shortened by a legitimate recovery; what it may
    -- never do is pretend it was not.
    reseal_reason TEXT         NOT NULL DEFAULT '',
    reseal_by     TEXT         NOT NULL DEFAULT ''
);

-- The verifier wants the newest anchor and nothing else.
CREATE INDEX IF NOT EXISTS audit_anchors_taken_at_idx ON audit_anchors (taken_at DESC, id DESC);
