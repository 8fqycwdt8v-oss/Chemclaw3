-- The text a substring search over notes matches against, stored with the note
-- (D-2026-10-14-the-notes-live-in-postgres-and-git-leaves-the-knowledge-path).
--
-- `kg.search.search_text` defines what a note's searchable text is: its id, type, structure, tags,
-- source, recorded setpoints and body. Every search leg matches `term in text.lower()` as a
-- substring (`ester` finds `polyester`), which an `ILIKE` over `body` and `frontmatter` does not
-- equal and a `tsvector` would change (word stems instead of substrings, as `085` explains for
-- job records). So the store writes the lowercased haystack here, with each revision, and the
-- search selects with `search_text LIKE '%term%'`: the same predicate, served by the trigram index.
--
-- Additive and expand-only: the column defaults to '' and nothing reads it before the Postgres
-- store, which also writes it. `kg_notes` holds no row when this runs (nothing wrote it before
-- the store that writes this column), so there is nothing to backfill; a change to
-- `search_text()` is a re-put of the notes, which the importer already is.
--
-- A term shorter than three characters yields no trigram and scans the table, as `085` states for
-- job records; at the size of a note corpus that is milliseconds, and two-letter terms (`Pd`) are
-- real in chemistry.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE kg_notes ADD COLUMN IF NOT EXISTS search_text TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS kg_notes_search_text_trgm_idx
    ON kg_notes USING gin (search_text gin_trgm_ops);
