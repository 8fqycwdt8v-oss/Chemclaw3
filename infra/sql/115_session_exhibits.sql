-- Artefacts: the versioned working documents a session shows beside its chat
-- (D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect). Named `exhibit` because
-- `artifact` is the calculation store's word in this tree (`artifact_blobs`, D-124).
--
-- **Two tables, and the revision model is `073`'s copied rather than shared.** The header is a
-- mutable projection — its head revision, title and last-touched time move — and the revisions under
-- it are append-only: a change is a new row naming the row it came from, and a write whose
-- `parent_revision` is not the head is refused rather than allowed to discard the revision it did
-- not see. A chemist's edit to what the agent drafted is the signal `073` calls the most informative
-- thing this system observes about its own output, and an UPDATE in place would keep the document
-- and throw the edit away.
--
-- **No foreign key to `session_owners`, for `session_messages`' reason.** An artefact is part of the
-- conversation and follows it: a session delete and an erasure remove it with the transcript
-- (`agent/session_store._SESSION_DELETE`, `agent/leaver._ERASE`), and the retention sweep holds the
-- ownership row back while one remains (`durable/retention._SESSION_SCOPED_ROWS`).
CREATE TABLE IF NOT EXISTS session_exhibits (
    -- `xb-` and 16 random hex. Random rather than content-derived: two identical tables in one
    -- session are two artefacts, and an id a client could compute is an id it could guess.
    exhibit_id          TEXT        PRIMARY KEY,
    session_id          TEXT        NOT NULL,
    kind                TEXT        NOT NULL,
    title               TEXT        NOT NULL,
    -- Denormalised from the revision table so a listing is one query; recomputed on every append
    -- under the header's row lock, never the source of truth.
    head_revision       INTEGER     NOT NULL,
    head_author_kind    TEXT        NOT NULL,
    head_author         TEXT        NOT NULL,
    -- The highest revision the agent has written, read or been told about. What makes the turn note
    -- announce a chemist's edit once rather than on every turn until the agent revises.
    agent_seen_revision INTEGER     NOT NULL,
    created_by          TEXT        NOT NULL,
    correlation_id      TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT session_exhibits_kind_known
        CHECK (kind IN ('document', 'table', 'structures', 'chart', 'result', 'link')),
    CONSTRAINT session_exhibits_head_author_known
        CHECK (head_author_kind IN ('agent', 'human'))
);

-- The pane's listing and the per-session cap: one session's artefacts, newest first.
CREATE INDEX IF NOT EXISTS session_exhibits_session_idx
    ON session_exhibits (session_id, updated_at DESC);
-- The retention sweep's cutoff.
CREATE INDEX IF NOT EXISTS session_exhibits_updated_idx
    ON session_exhibits (updated_at);

CREATE TABLE IF NOT EXISTS session_exhibit_revisions (
    exhibit_id         TEXT        NOT NULL
                                   REFERENCES session_exhibits (exhibit_id) ON DELETE CASCADE,
    -- 1-based and gapless; the key is the backstop if two writers ever race past the row lock.
    revision           INTEGER     NOT NULL,
    -- 0 on the first; the head it was derived from on every later one.
    parent_revision    INTEGER     NOT NULL,
    author_kind        TEXT        NOT NULL,
    author             TEXT        NOT NULL,
    change_note        TEXT        NOT NULL,
    spec               JSONB       NOT NULL,
    byte_size          INTEGER     NOT NULL,
    -- The numerals an agent-authored revision states that no tool in its session returned —
    -- unchecked, not wrong. NULL where nothing was checked (a human revision, or a deployment with
    -- no tool-result store), which is a different answer from "checked, and every figure was
    -- found" (`[]`).
    unverified_figures JSONB,
    correlation_id     TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exhibit_id, revision),
    CONSTRAINT session_exhibit_revisions_author_known
        CHECK (author_kind IN ('agent', 'human'))
);
