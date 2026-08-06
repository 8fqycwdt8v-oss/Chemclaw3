-- The document corpus index: a mounted file share, chunked and embedded so its reports, decks and
-- spreadsheets are answerable as cited evidence (D-2026-08-06-a-share-is-mounted-not-called).
--
-- Two tables rather than one, because a classical file share is full of duplicates — the same
-- report copied into four project folders — and embedding each copy would be paying four times for
-- one document. So identity is the *content*: `document_chunks` is keyed by `doc_id`, the stable
-- hash of the parsed text, and `document_files` maps every path that yielded that content to it.
-- Four copies collapse to one set of chunks and one embedding call, which at TB scale is the
-- difference that makes the corpus affordable. It is `chemclaw.cli.backfill_corpus`' rule ("the id
-- is derived from the content, not the filename") and D-011's ("persisted once, never recomputed")
-- applied to embeddings.
--
-- `fingerprint` is the "mtime_ns:size" stat signature the file had when it was last parsed — the
-- same trick `note_index` (035) uses, so an unchanged file is never re-read, let alone re-embedded.
-- A crawl of 500k files that changed nothing costs one `scandir` pass and no LLM endpoint calls.
--
-- Derived and rebuildable: the share is the source of truth and nothing here is ever written back
-- to it. Dropping both tables and re-running `chemclaw.ingest.documents.sync` reconstructs them.
--
-- The embedding width (1536) is coupled to `settings.embedding_dim` exactly as `note_index` is;
-- changing it is a new migration, not an in-place edit. Applied by `make db-migrate`.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_files (
    -- Keyed by (source, path), not path alone: two mounted shares can hold the same relative path
    -- (`Projects/report.pdf` is not an unusual name), and a global key would silently let the
    -- second share's crawl overwrite the first's row and then sweep it.
    source      TEXT NOT NULL,
    path        TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    modified_at TIMESTAMPTZ,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, path)
);

-- The primary key already serves every by-source lookup (the fingerprint diff, the sweep); this is
-- the other access pattern: the retriever resolves a hit's citation path by `doc_id`.
CREATE INDEX IF NOT EXISTS document_files_doc_id_idx ON document_files (doc_id);

CREATE TABLE IF NOT EXISTS document_chunks (
    doc_id     TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    -- The structural coordinate the parser gave this chunk ("page 3", "slide 7", "sheet Yields").
    -- Carried through to the citation, because a chemist checking "the table on page 3" needs the
    -- page to survive both parsing and chunking.
    coordinate TEXT NOT NULL DEFAULT '',
    embedding  vector(1536),
    lexeme     tsvector,
    PRIMARY KEY (doc_id, ordinal)
);

CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunks_lexeme_idx
    ON document_chunks USING gin (lexeme);
