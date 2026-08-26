-- A run's figures ride on the reaction row, as numbers rather than as prose (D-2026-08-25).
--
-- `kg.note.ProcessConditions` landed while the transcription tier was in flight and put a run's
-- setpoints and outcomes into note frontmatter, arguing for that over "a second table". That
-- argument survives the move rather than being overridden by it: what it rejected was a store
-- *just* for conditions, and the reaction row already exists, so these ride on it and there is
-- still exactly one place a run's figures live.
--
-- **A separate migration rather than an edit to `052`, and that is the whole reason this file
-- exists.** The column belongs in `052` by every aesthetic measure — it is one table, added the
-- same day, by the same change. But the runner keys each file by a checksum of its statements, so
-- editing an applied migration breaks `make db-migrate` on every database that already ran it.
-- Measured the hard way: editing it in place did exactly that to this repo's own dev database,
-- and `tests/test_migrations_are_additive.py` failed the commit that tried it.
ALTER TABLE reaction_records ADD COLUMN IF NOT EXISTS conditions JSONB;

-- No index. The column is read only as part of a row already located by its primary key —
-- `expand_note` on one citation, `condense_protocols` on a handful — and never filtered on, so an
-- index here would cost every ingest write and serve no query.
COMMENT ON COLUMN reaction_records.conditions IS
    'The setpoints and outcomes a run recorded (kg.note.ProcessConditions), as JSONB rather than '
    'seven columns: the shape is one model with one reader, and a column per field would be a '
    'migration every time that model gains one. NULL means the entry recorded none of them — '
    'never "all zero", which is the distinction comparison.MISSING renders.';
