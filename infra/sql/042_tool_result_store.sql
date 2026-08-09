-- Content-addressed store for what a tool call returned, so a surface can fetch it (D-2026-08-09).
--
-- `ToolResultEvent.preview` is 200 characters of a result, cut at whatever byte the budget lands
-- on and explicitly not JSON — so a hazard screen with severities and citations, a charge table,
-- a solvent ranking all reach the browser as prose the model wrote *about* them. The full text
-- already exists at emit time (`api/runner_trace.py::_result_text`, "Untruncated on purpose") and
-- was discarded once the event was built. These two tables are where it now lives, and
-- `GET /sessions/{id}/tool-results/{ref}` is how it is read back.
--
-- **Two tables, for the same reason 019 has two.** The blob is addressed by the SHA-256 of its own
-- content, so a repeated identical call stores nothing — the standing "never compute twice"
-- position (D-011) applied to bytes rather than to answers. The link row is what makes a blob
-- *reachable*, and it carries the session the call belonged to: that is what lets the read route
-- reuse `resolve_session`, the front door's existing ownership gate, instead of inventing an auth
-- story for a bare `/tool-results/{ref}`.
--
-- **These are trace blobs, not results of record.** `calculation_results` (001) is the answer and
-- is never evicted; `job_records` (023) is the durable evaluation record and retention refuses to
-- touch it. A tool-result blob is a *view* of a turn that already happened, and losing it costs a
-- rendering, never a recomputation — which is exactly what makes a plain age cutoff the right
-- instrument here and the wrong one there. `durable/retention.py` sweeps it by `created_at`; there
-- is deliberately no `last_access_at` and no LRU eviction machinery, because ordering evictions by
-- value only pays when what you are ordering is expensive to regenerate, and nothing here is.
--
-- Applied by `make db-migrate` (idempotent).
CREATE TABLE IF NOT EXISTS tool_result_blobs (
    content_hash TEXT PRIMARY KEY,        -- sha256 hex of the UTF-8 result text; the `ref` on the wire
    byte_size    BIGINT      NOT NULL,    -- length of `data`, so a reader can bound before fetching
    data         BYTEA       NOT NULL,
    -- When this content was last produced, which is what the TTL sweep orders by. Refreshed on a
    -- repeat rather than left at the first write: the same result text arriving again is a *live*
    -- trace, and expiring it on the clock of a turn from three weeks ago would delete a blob two
    -- of whose links are from this morning.
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Same argument as 019: a result is JSON-ish text, Postgres would spend a TOAST compression pass
-- on it, and this store's whole cost model is "cheap to write, cheap to drop". EXTERNAL keeps the
-- bytes out of line without that pass. (019 compresses in the application and so *must* set this;
-- here it is a throughput choice rather than a necessity, and it is the same choice.)
ALTER TABLE tool_result_blobs ALTER COLUMN data SET STORAGE EXTERNAL;

CREATE TABLE IF NOT EXISTS tool_result_links (
    session_id     TEXT NOT NULL,         -- the conversation the call belonged to: the ownership scope
    content_hash   TEXT NOT NULL REFERENCES tool_result_blobs (content_hash) ON DELETE CASCADE,
    tool           TEXT NOT NULL,         -- which tool answered, so a surface can label a fetch
    -- The turn's correlation id, so one fetched result joins the audit trail and the logs for the
    -- turn that produced it. Not part of the key: two calls in one turn returning identical text
    -- are one row, and that is the dedup working rather than a collision.
    correlation_id TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, content_hash)
);

-- `ON DELETE CASCADE` above is load-bearing, exactly as it is in 019: the retention sweep deletes
-- *blobs*, and the link rows go with them — so a link can never name bytes that are gone, and the
-- sweep stays one statement with no orphan pass to forget.
CREATE INDEX IF NOT EXISTS tool_result_links_session_idx
    ON tool_result_links (session_id, created_at DESC);

-- The retention sweep's scan: oldest content first.
CREATE INDEX IF NOT EXISTS tool_result_blobs_created_idx
    ON tool_result_blobs (created_at);
