-- The listing the front door actually serves most often, which is the one 073 did not cover.
--
-- 073 declares `(status, updated_at DESC)` and `(project, updated_at DESC)` and calls them "the two
-- listings the front door serves". Both `GET /protocols` and `find_experiment_protocols()` default
-- to **no** status and **no** project, so the common call is `ORDER BY updated_at DESC LIMIT 50`
-- with no `WHERE` at all — the one shape neither index answers.
--
-- Measured on 200,000 header rows with `EXPLAIN (ANALYZE, BUFFERS)`: a parallel sequential scan and
-- a sort, 2,396 shared buffers and 23.3 ms, against 6 buffers and 0.052 ms for the same query with
-- a status filter. That is 400x the buffers for the query a chemist runs by opening the page.
CREATE INDEX IF NOT EXISTS experiment_protocols_recent_idx
    ON experiment_protocols (updated_at DESC);
