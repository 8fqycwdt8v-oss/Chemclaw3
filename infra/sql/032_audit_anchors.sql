-- Signed high-water anchors over the audit chain — the one alteration the chain cannot see.
--
-- `011_audit_hash_chain.sql` makes modification, reordering, interior deletion and *prefix*
-- truncation detectable, because each of those breaks a link. Deleting a *trailing* run does not:
-- the remaining rows still chain cleanly, and nothing recorded how many rows there should have
-- been. That limit is written into `cli/verify_audit_chain.py` and was deferred as a regulatory
-- question — until backup/restore made it an operational one. **A point-in-time restore is exactly
-- a trailing deletion.** Any restore silently shortens the compliance trail in the single way the
-- chain was built not to notice.
--
-- An anchor records what the trail looked like at a moment — how many rows, the highest id, and the
-- tip's `row_hash` — and signs it. Signed, because an actor who can delete rows can also insert a
-- lower anchor; the HMAC key (`CHEMCLAW_AUDIT_ANCHOR_SECRET`) is not in the database, so a forgery
-- needs something a database compromise alone does not give.
--
-- **This table is not by itself the control, and pretending otherwise would be the whole failure
-- again.** A PITR rolls the anchors back with the events. What makes a restore detectable is that
-- every anchor is *also* emitted to the process log at a stable marker (`audit_chain_anchor=`), so
-- it lands in a store Postgres cannot roll back — and an operator hands that recovered value to the
-- verifier with `--anchor`. The table is the convenient copy; the log line is the out-of-band one.
--
-- Append-only in use: nothing updates or deletes a row here, for the same reason nothing prunes
-- `audit_events`.
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
    -- accepted the gap and why. A GxP trail may be shortened by a legitimate recovery; what it may
    -- never do is pretend it was not (`docs/guides/runbook.md` §(xiii)).
    reseal_reason TEXT         NOT NULL DEFAULT '',
    reseal_by     TEXT         NOT NULL DEFAULT ''
);

-- The verifier wants the newest anchor and nothing else.
CREATE INDEX IF NOT EXISTS audit_anchors_taken_at_idx ON audit_anchors (taken_at DESC, id DESC);
