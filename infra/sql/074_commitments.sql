-- The work a programme has committed to, mirrored in from the system that owns it (F4).
--
-- **Nine of nineteen `manager` bucket-C probes need one object this schema did not have: a unit of
-- committed work.** Seventy-three migrations, and `project` was a nullable text tag on
-- `reaction_records` — a facet on a row, not an entity. No programme, no activity, no dependency,
-- no milestone, no capacity, and no person beyond an `actor` string. The system prompt says so
-- plainly: "no project, programme, capacity, headcount or timeline data".
--
-- **This is a mirror, not a system of record, and the distinction is the whole design.** The
-- organisation already runs a portfolio tool and that tool is the truth; nothing here plans,
-- schedules, levels resources or computes a critical path, and a deployment that let it try would
-- have two answers to "when does this land". What this table adds is the one join no portfolio tool
-- can compute: between a slipping milestone and the *chemistry* that is slipping it.
--
-- Keyed on `(source, external_id)` for the reason `reaction_fingerprints` is
-- (`D-2026-08-27-a-fingerprint-is-keyed-by-its-source`): two systems may both call something
-- `PRJ-14`, and a bare id would silently merge them.
CREATE TABLE IF NOT EXISTS commitments (
    source        TEXT        NOT NULL,
    external_id   TEXT        NOT NULL,
    -- What kind of thing this is, in the vocabulary a programme uses. Bounded so a surface can
    -- group without parsing a title, and short because a deeper hierarchy is the portfolio tool's
    -- business rather than this mirror's.
    kind          TEXT        NOT NULL DEFAULT 'activity',
    title         TEXT        NOT NULL,
    -- Who owns it, in the *source's* namespace. Deliberately not resolved to an Entra oid here: a
    -- mapping this system invented would be a second directory, and a wrong one would attribute
    -- somebody else's work. A surface that wants to join it to a principal does so where the
    -- mapping is known.
    owner         TEXT        NOT NULL DEFAULT '',
    state         TEXT        NOT NULL DEFAULT 'open',
    due_at        TIMESTAMPTZ,
    -- The parent's `external_id` within the same source, or '' at the top. A string rather than a
    -- foreign key: a mirror receives rows in whatever order the export produces them, and a
    -- constraint would reject a child that arrived before its parent — turning a partial sync into
    -- a failed one.
    parent_id     TEXT        NOT NULL DEFAULT '',
    -- **The join, and the reason this table is worth having.** What science this commitment is
    -- waiting on: note ids, durable job ids, and compound identifiers, exactly as the source stated
    -- them. Arrays rather than a join table because nothing here queries *from* the science back to
    -- the commitment, and a table nobody joins in that direction is a table nobody maintains.
    note_ids      TEXT[]      NOT NULL DEFAULT '{}',
    job_ids       TEXT[]      NOT NULL DEFAULT '{}',
    compounds     TEXT[]      NOT NULL DEFAULT '{}',
    -- When the source last said this, so a stale mirror is visible as staleness rather than as
    -- fact. Every reading reports it.
    observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (source, external_id),

    CONSTRAINT commitments_kind_known
        CHECK (kind IN ('programme', 'activity', 'milestone', 'deliverable')),
    CONSTRAINT commitments_state_known
        CHECK (state IN ('open', 'in-progress', 'blocked', 'done', 'cancelled'))
);

-- "What is due, soonest first" and "what is late", over the states that are still live.
CREATE INDEX IF NOT EXISTS commitments_due_idx
    ON commitments (due_at) WHERE state IN ('open', 'in-progress', 'blocked');

-- "What does this person own", and "what hangs off this programme".
CREATE INDEX IF NOT EXISTS commitments_owner_idx ON commitments (owner, due_at);
CREATE INDEX IF NOT EXISTS commitments_parent_idx ON commitments (source, parent_id);
