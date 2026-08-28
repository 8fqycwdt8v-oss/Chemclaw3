-- Where an append-only reaction feed's drain stopped, so tomorrow's fire resumes instead of
-- re-walking (D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop).
--
-- `corpus_sync.py` carries its keyset cursor **inside one run** and deliberately stores nothing:
-- "a re-drain of an unchanged release is a no-op … and a *new* release must be walked from the
-- top". That is right for a versioned vendor load and wrong for a live feed, where every daily fire
-- reads the entire corpus to discover the rows added since yesterday. Correct — every write is an
-- id-keyed upsert — and O(whole corpus) per day, forever.
--
-- **Its own table rather than a row in `sync_cursors` (007).** That column is `TIMESTAMPTZ` and its
-- contract is a datetime watermark; a keyset position is a `TEXT` key in the *source's* own domain,
-- which may be a bigint, a ULID or a padded string. Putting one in a timestamp column would be the
-- shape mismatch this schema keeps being taught, and widening `sync_cursors` would put two cursor
-- kinds behind one name where a reader cannot tell which it holds.
--
-- **A row exists only for a source whose binding says `append_only: true`.** A release-mode source
-- writes nothing here and behaves exactly as it did before this migration, which is what makes the
-- change additive in behaviour and not only in DDL.
--
-- **Deleting a row is the supported way to force a full re-walk** — after a binding change, a
-- `STANDARDIZATION_VERSION` bump, or a backfill the watermark would have hidden. That is the whole
-- operator interface, and it is why the table has no `release` column: naming the load a cursor
-- belongs to would imply this side can detect a new one, and it cannot.
--
-- Applied by `make db-migrate` (idempotent).
CREATE TABLE IF NOT EXISTS corpus_cursors (
    -- The registry source name (`CHEMCLAW_DATA_SOURCES`), the same string that is half of every
    -- `reaction_labels` and `reaction_species` key.
    source     TEXT        PRIMARY KEY,
    -- The last key the previous pass saw, verbatim from the binding's `order_by` column. Empty is
    -- not stored: a pass that advanced past nothing has nothing to resume after.
    after      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
