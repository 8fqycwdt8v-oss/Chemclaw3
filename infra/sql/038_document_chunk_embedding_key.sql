-- Which embedding configuration produced each stored document vector
-- (D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it).
--
-- `document_chunks` was written by whatever `embedding_provider`/`embedding_model`/`embedding_dim`
-- happened to be configured at the time, and recorded none of it. The crawl diffs on the file's
-- `mtime_ns:size` fingerprint, which does not move when the *model* changes — so pointing a
-- deployment at a better embedding model re-embedded nothing, left the table holding a mix of two
-- models' vectors, and made every cosine comparison between them meaningless. Nothing failed;
-- retrieval simply started returning confident nonsense.
--
-- `chemclaw.core.embeddings.embedding_config_key()` already keyed the *in-memory* cache on exactly
-- this, for exactly this reason (D-011). This column is the durable half: a chunk whose key is not
-- the current one is stale, and `chemclaw.ingest.documents.sync.reembed_stale` re-embeds it from
-- the `content` already stored beside it — so a model swap heals itself on the next scheduled run
-- without touching the file share at all.
--
-- NULL for every row written before this migration reads as "unknown" and is therefore always
-- treated as stale — a one-time re-embed of the existing corpus after upgrade, never a wrong skip.
-- That is the same argument `035_note_index_fingerprint.sql` makes for its own added column.
--
-- The index is what keeps the stale scan cheap: it is a `WHERE embedding_key IS DISTINCT FROM $1`
-- over a table that is expected to hold millions of rows, run on every crawl, and finding nothing
-- is the normal case. Applied by `make db-migrate`.
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_key TEXT;

CREATE INDEX IF NOT EXISTS document_chunks_embedding_key_idx
    ON document_chunks (embedding_key);
