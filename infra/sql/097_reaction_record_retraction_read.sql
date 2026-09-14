-- The column 066 added is read now, and the deployed comment still says it never will be.
--
-- 068 replaced 066's comment with "Reserved and unread. Nothing writes this column", because the
-- ingest tier that would have set it was removed on review: it had no producer, the production
-- adapter wrapper hid the capability from the walk that looked for one, and — the finding that
-- decided it — with both of those fixed a retracted reaction was still returned by the unfiltered
-- evidence sweep and by the agent's own `similar_reactions`
-- (`D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry`).
--
-- All five readers now honour it and the producer is a field on the entry the source exports
-- rather than a sweep over absences (`D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports`), so
-- the deployed comment is the one thing left claiming the opposite — and it is what a DBA reading
-- `\d+ reaction_records` finds. Editing 068 in place would change nothing, because 068 is already
-- applied; the correction is a migration, and it is comment-only.
--
-- The two rules that are easy to get wrong are stated in the comment itself rather than here,
-- because the catalogue is where somebody meets this column:
--   * NULL is "not retracted", never "withdrawn" — a fetch is a delta, and an entry absent from an
--     export is what every already-ingested entry looks like;
--   * the upsert refreshes this field like every other, so a source re-publishing an entry without
--     a tombstone *un*-retracts it. A `COALESCE` here would make one bad export permanent on a
--     tier whose whole rule is that the row is what the source last said.
COMMENT ON COLUMN reaction_records.retracted_at IS
    'When the source reported this entry withdrawn; NULL means not retracted. Written by '
    '`ingest/eln/records.py` from `RawEntry.retracted_at` — the entry the source exported, never '
    'inferred from an entry''s absence from an export. Refreshed on every upsert in both '
    'directions, so re-publishing an entry without a tombstone lifts the withdrawal. The row is '
    'kept and stays readable — `read()` and `agent.graph_tools.expand_note` serve it with the '
    'withdrawal shown — while `is_current`, `eligible()`, the unfiltered retrieval sweep and the '
    '`similar_reactions` tool all drop it.';
