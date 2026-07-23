-- Derived note index for hybrid retrieval (plan F10-A2). One row per knowledge-graph note,
-- holding a dense embedding and a lexical `tsvector` so a note can be found by semantic
-- similarity or by term match — the two entry points that graph traversal and structural
-- fingerprints do not provide. This is a *derived* index: the git-markdown graph stays the
-- source of truth (D-004); `report.vector_index.reindex_notes` (re)builds these rows and they
-- can be dropped and rebuilt at any time.
--
-- The embedding width (1536) is coupled to `settings.embedding_dim` and the embedding model's
-- output size, exactly as the fingerprint bit width is coupled to `ecfp_bits`: changing it is a
-- new migration, not an in-place edit. Cosine distance (`<=>`) ranks the dense search, accelerated
-- by an HNSW `vector_cosine_ops` index; `ts_rank` over the GIN-indexed `tsvector` ranks the lexical
-- search. Applied by `make db-migrate`.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS note_index (
    note_id    TEXT PRIMARY KEY,
    embedding  vector(1536),
    lexeme     tsvector,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS note_index_embedding_idx
    ON note_index USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS note_index_lexeme_idx
    ON note_index USING gin (lexeme);
