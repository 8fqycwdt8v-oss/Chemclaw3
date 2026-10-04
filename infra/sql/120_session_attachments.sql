-- Uploaded working files, held where every front-door replica can read them
-- (D-2026-10-04-an-upload-is-session-state-not-pod-state).
--
-- **Why a table, when an attachment is working material rather than the record.** It was held in
-- the memory of the pod that received the upload (`agent/attachments.STORE`), and a later turn
-- resolved it by session id in *that* process's dict. With more than one front-door replica the
-- turn that asks about the file can land on a sibling — the companion UI's BFF reaches this service
-- through its ClusterIP Service, where the Route's affinity cookie does not exist — and the agent
-- answered "no attachment named 'runs.csv' in this conversation" about a file uploaded to exactly
-- that conversation. A session is already resolvable from any replica (`session_owners`); its
-- working files now are too.
--
-- **The parsed text, not the uploaded bytes.** What a turn reads is the text the isolated parser
-- produced; keeping the bytes would store a second, larger copy of the same file in a form nothing
-- reads, and re-parsing on every read would put an untrusted-document parse on the turn path.
--
-- **A dropped upload keeps its row, with `body` NULL.** The per-session bounds
-- (`attachment_max_per_session`, `attachment_store_max_bytes`) drop the oldest files, and the
-- tools say which were dropped rather than answering "never sent" about them
-- (`agent/attachments.SessionAttachments`). The name, size and upload time stay so that sentence
-- can still be written by whichever replica answers the next turn; the text — the part that costs
-- anything — goes.
--
-- **No foreign key to `session_owners`, for `session_messages`' and `session_exhibits`' reason.**
-- An attachment is part of the conversation and follows it: a session delete and an erasure remove
-- it with the transcript (`agent/session_store._SESSION_DELETE`, `agent/leaver._ERASE`), and the
-- retention sweep holds the ownership row back while one remains
-- (`durable/retention._SESSION_SCOPED_ROWS`).
CREATE TABLE IF NOT EXISTS session_attachments (
    -- Upload order within a session, and the tie-breaker `created_at` cannot be: two files
    -- dropped on the UI together commit inside one clock tick.
    attachment_id BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    TEXT        NOT NULL,
    -- The sanitized basename (`agent/attachments._safe_name`) — the handle the model passes to
    -- `read_attachment`, never the client's raw filename.
    name          TEXT        NOT NULL,
    content_type  TEXT        NOT NULL,
    row_count     INTEGER     NOT NULL DEFAULT 0,
    -- The parsed text; NULL once the per-session bound has dropped this upload.
    body          TEXT,
    -- `octet_length(body)` at insert, kept after `body` is cleared so the eviction arithmetic and
    -- an operator's storage question are both one indexed read.
    byte_size     BIGINT      NOT NULL,
    -- Who uploaded it: the authenticated principal's oid. What lets an erasure reach a member's
    -- upload in a session somebody else owns, as `session_messages.actor` does for their words.
    uploaded_by   TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    evicted_at    TIMESTAMPTZ,
    CONSTRAINT session_attachments_evicted_has_no_body
        CHECK ((evicted_at IS NULL) = (body IS NOT NULL))
);

-- Every read is one session's uploads in upload order: the listing, the by-name read, the
-- eviction pass and a session delete.
CREATE INDEX IF NOT EXISTS session_attachments_session_idx
    ON session_attachments (session_id, attachment_id);
-- The retention sweep's cutoff (`durable/retention._prune_by_age`).
CREATE INDEX IF NOT EXISTS session_attachments_created_idx
    ON session_attachments (created_at);
-- An erasure's per-person arm.
CREATE INDEX IF NOT EXISTS session_attachments_uploaded_by_idx
    ON session_attachments (uploaded_by);
