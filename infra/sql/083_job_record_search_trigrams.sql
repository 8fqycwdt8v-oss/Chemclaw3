-- The one index gap from the 2026-09-04 performance review that `081` does not close.
--
-- This migration was `081_performance_review_indexes.sql` and carried two: a bare-`reaction_id`
-- index on `reaction_records`, and the trigrams below. The first arrived independently on `main`
-- as `081_reaction_record_id_lookup.sql` — same table, same column, a different index name — so
-- keeping both would have created the same index twice and paid for it on every upsert. `081` is
-- the one that shipped; this file is what was left over, renumbered to the next free prefix
-- because a number is only free once.
--
-- `job_records_*_trgm_idx` — `durable/job_record_store.py`'s `_SEARCH` is a leading-wildcard
-- `ILIKE` over three columns, and the agent calls it (`search_job_records`). A leading wildcard is
-- unindexable by a btree, so every **miss** reads the whole table while holding one of the
-- process's pooled connections; a hit is fast only because the `completed_at` index lets the scan
-- stop early, which is why nothing in the suite or in a demo ever saw this.
--
-- `_SEARCH`'s own comment argued the shape was fine — "this table holds one row per durable run
-- (thousands, not millions) … a search index would be machinery to maintain for a scan the
-- database does in milliseconds". At 200 chemists × ~5 durable jobs/day that premise expires:
-- ~365k rows/year, and an agent searching for a phrase it invented produces exactly the miss.
--
-- Measured on 500 000 rows (185 MB) through psycopg with the shipped statement and its bound
-- parameters, `plan_cache_mode=force_custom_plan`:
--
--   term with no match : Index Scan on completed_at, Rows Removed by Filter: 500 000,
--                        19 920 buffers -> 1 036 ms   ->  BitmapOr of the three indexes below,
--                        84 buffers -> 1.09 ms  (950x)
--   term that matches  : 0.89 ms -> 1.07 ms (unchanged; the planner keeps the cheap
--                        early-stopping `completed_at` scan, which is the right plan for a hit)
--   empty term         : 0.93 ms -> 0.72 ms (unchanged)
--
-- **Trigrams rather than the `tsvector` that comment declined, and that is a semantic choice.** A
-- `tsvector` would change what the tool matches — word stems instead of substrings, `websearch`
-- boolean widening instead of a phrase — and the tool's docstring promises the substring search it
-- has today. `gin_trgm_ops` accelerates the *same* predicate: the rows returned are identical,
-- byte for byte, which is the property `tests/test_job_record_store.py` pins.
--
-- **Three indexes, not one over a concatenation**, because the predicate is an `OR` over three
-- columns and the planner needs all three arms indexed to build the BitmapOr; an expression index
-- over `rationale || summary || job` would also match a term straddling two of them.
--
-- Cost: 91 MB of index against a 185 MB table (rationale 57, summary 24, job 10) and three GIN
-- inserts per durable run — at ~1 000 runs/day, nothing. GIN's pending list absorbs them.
--
-- **The residual is stated rather than hidden.** A LIKE pattern shorter than three characters
-- yields no trigram, so `%dG%` or `%Pd%` cannot use these indexes and still scans: measured, a
-- two-character miss costs 930 ms before and after. Two-letter terms are real in chemistry (`Pd`,
-- `dG`, `IR`), so refusing them would cost a capability to close a hole that is ~1% of the
-- exposure; what closes it is a different search semantics, which is a decision rather than an
-- index.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS job_records_rationale_trgm_idx
    ON job_records USING gin (rationale gin_trgm_ops);

CREATE INDEX IF NOT EXISTS job_records_summary_trgm_idx
    ON job_records USING gin (summary gin_trgm_ops);

CREATE INDEX IF NOT EXISTS job_records_job_trgm_idx
    ON job_records USING gin (job gin_trgm_ops);
