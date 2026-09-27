-- The note a run produced, on the row that records why the run happened.
--
-- `calculation_publication` exists to answer "what question was this meant to answer", and it
-- carried the session, the job, the actor, the correlation id and the rationale — every weak
-- link — while dropping the one **structured** link this system already holds. `job_records.note_id`
-- is written by `durable/connector_job.py::finished_job_record` from the envelope a finished
-- connector job returns, and both publish paths had it in hand and did not carry it: the live
-- activity is handed the same `ConnectorJobResult` that fills that column, and the backfill reads
-- the very row it sits in.
--
-- **A note id, not a reaction id**, and the difference is the whole of what stays open. Which ELN
-- run *motivated* a calculation is a fact nothing in this system records — no launcher argument, no
-- job record column and no cache row names it — so a column for it would be one nobody could fill,
-- which is the shape `D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports` and
-- `D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability` both refused inside a
-- week. See `D-2026-09-13-a-publication-carries-the-link-the-system-already-holds`.
--
-- Empty is the normal case and means "this run produced no note", which is what the source column
-- means. Nullable would add a third state nothing distinguishes.
ALTER TABLE calculation_publication
    ADD COLUMN IF NOT EXISTS note_id VARCHAR(256) NOT NULL DEFAULT '';

-- "Everything published from the work behind this note", which is the direction a chemist reads it
-- in: they have the note, they want the numbers. Partial, because the empty string is the majority
-- and indexing it would be one entry per row of the table for a value nobody looks up.
CREATE INDEX IF NOT EXISTS calculation_publication_note_idx
    ON calculation_publication (note_id)
    WHERE note_id <> '';
