-- Retention stopped being one sweeping DELETE, so it needs an index it never needed before (D-145).
--
-- `session_messages` had exactly one index, `(session_id, id)`, described in 008 as "the provider's
-- only read path" — and that was true while retention was a single
-- `DELETE ... WHERE created_at < cutoff` that seq-scanned once per pass and nobody minded.
--
-- It cannot stay one statement. A conversation row is not disposable on its own terms: a `tool_use`
-- and the `tool_result` answering it must be deleted together or not at all, and whether an expired
-- row's partner is *also* expiring is not a question SQL can answer in the same statement that does
-- the delete. So the prune became "find the sessions with expired rows, then decide per session",
-- and that first step is a `created_at` predicate with no index behind it — inside a 600 s activity
-- budget, on the one table that grows without bound.
--
-- `(created_at, session_id)` rather than `(created_at)`: the lead step is the range scan, and
-- carrying `session_id` lets the DISTINCT be answered from the index alone rather than by visiting
-- every expired row's heap page. The existing `(session_id, id)` index still serves the per-session
-- read and the delete, so this adds a scan path rather than replacing one.
CREATE INDEX IF NOT EXISTS session_messages_created_at_idx
    ON session_messages (created_at, session_id);
