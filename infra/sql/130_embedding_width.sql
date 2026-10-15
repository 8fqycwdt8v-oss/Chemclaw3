-- The embedding width is configuration, not schema
-- (D-2026-10-15-an-embedding-width-is-a-column-not-a-rewrite).
--
-- `note_index.embedding` and `document_chunks.embedding` were created `vector(1536)` (012, 037), and
-- a `vector(N)` column keeps its width for life: `ALTER COLUMN … TYPE` is refused by the additive
-- rule, and an HNSW index needs a fixed width. So a deployment whose embedding model has another
-- width gets a sibling column and index, `embedding_<N>`, beside the original; the original keeps its
-- name and stays the home of the 1536-wide default, so a deployment that never changes width changes
-- nothing and re-embeds nothing.
--
-- This file defines the function; `core/migrate.py` calls it on every run with
-- `settings.embedding_dim`, because the width is a setting that can change between releases while a
-- migration runs once. It does nothing when the columns and their indexes exist, and for 1536 (the
-- original column); in either case it takes no lock and rewrites no row, so the pre-upgrade hook
-- pays nothing on an unchanged width.
--
-- **When it does work it is not milliseconds.** Both tables are locked `ACCESS EXCLUSIVE` up front,
-- `note_index` first and `document_chunks` second, in one statement, so it queues once and a writer
-- touching both cannot deadlock it; and the locks are held to the end of the migration's one
-- transaction (`infra/sql/README.md`, "Migrations that lock a hot table"). `ADD COLUMN` of a
-- nullable column with no default rewrites nothing and is instant. The HNSW build is not: it scans
-- the whole heap although every vector is NULL, so the hold, which blocks reads as well as writes,
-- grows with the two tables' size, about 0.3-1.8 s per million rows on the measured image, more for
-- `document_chunks`' wide rows. `lock_timeout` aborts the run harmlessly if the lock does not come.
-- To keep the hold short on a large corpus, build the columns and indexes first outside the
-- transaction, which this function then finds and leaves alone:
--     ALTER TABLE note_index ADD COLUMN IF NOT EXISTS embedding_N vector(N);
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS note_index_embedding_N_idx
--         ON note_index USING hnsw (embedding_N vector_cosine_ops);
-- and the same for `document_chunks`. A failed CONCURRENTLY build leaves an invalid index of that
-- name, which `IF NOT EXISTS` would then skip: check `pg_index.indisvalid`, drop it, rebuild.
--
-- Which row belongs to which width is `embedding_key` (038, 039), which spells the width as `d<N>`:
-- a row is read only when its key is the live one, so a half-migrated corpus is never ranked across
-- widths, and `chemclaw.durable.reembed` moves rows to the new column and empties the old one.
-- Rolling back is deploying the previous image: it reads `embedding` and treats every row stamped
-- with another key as stale, which its own re-embed heals; the sibling columns are left unread.
--
-- 2000 is the widest `vector` pgvector will build an HNSW index on.
CREATE OR REPLACE FUNCTION ensure_embedding_width(width integer) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    holder text;
    col text := 'embedding_' || width;
    idx text;
    missing boolean := false;
BEGIN
    IF width < 1 OR width > 2000 THEN
        RAISE EXCEPTION 'embedding width % cannot be indexed: pgvector builds HNSW on 1 to 2000 dimensions', width;
    END IF;
    IF width = 1536 THEN
        RETURN;
    END IF;
    FOREACH holder IN ARRAY ARRAY['note_index', 'document_chunks'] LOOP
        IF to_regclass(holder) IS NOT NULL AND (
            NOT EXISTS (
                SELECT 1 FROM pg_attribute
                WHERE attrelid = to_regclass(holder) AND attname = col AND NOT attisdropped
            ) OR to_regclass(holder || '_' || col || '_idx') IS NULL
        ) THEN
            missing := true;
        END IF;
    END LOOP;
    IF NOT missing THEN
        RETURN;
    END IF;
    IF to_regclass('note_index') IS NOT NULL AND to_regclass('document_chunks') IS NOT NULL THEN
        LOCK TABLE note_index, document_chunks IN ACCESS EXCLUSIVE MODE;
    END IF;
    FOREACH holder IN ARRAY ARRAY['note_index', 'document_chunks'] LOOP
        IF to_regclass(holder) IS NULL THEN
            CONTINUE;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass(holder) AND attname = col AND NOT attisdropped
        ) THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN %I vector(%s)', holder, col, width);
        END IF;
        idx := holder || '_' || col || '_idx';
        IF to_regclass(idx) IS NULL THEN
            EXECUTE format(
                'CREATE INDEX %I ON %I USING hnsw (%I vector_cosine_ops)', idx, holder, col
            );
        END IF;
    END LOOP;
END
$$;
