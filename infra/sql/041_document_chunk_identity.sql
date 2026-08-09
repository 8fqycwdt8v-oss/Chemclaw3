-- A chunk row is identified by its content *and* its boundaries
-- (D-2026-08-08-a-derived-index-must-record-what-derived-it).
--
-- 040 recorded which chunking cut each row and left the primary key at `(doc_id, ordinal)`, which
-- made two shares that chunk differently fight over the same rows. `doc_id` is the hash of the
-- parsed text and is shared across sources **by design** — the same report in four project folders
-- is one set of chunks, which is what makes a TB share affordable (037) — while `chunking_key`
-- comes from the per-share binding. So a share mounted at 20000:200 re-cut a document another
-- share had already cut at 400:40, took ordinal 0 through `ON CONFLICT`, and 040's tail-drop
-- deleted ordinals 1..15 as though they were its own stranded tail. The victim never repaired: its
-- file row still carried its own chunking and its `mtime_ns:size` had not moved, so its gate read
-- "unchanged" forever. Measured in the reference index: the fine share then served one chunk of
-- 6259 characters in place of its own sixteen of at most 400.
--
-- With the chunking in the key, two cuttings of one document coexist and neither can overwrite the
-- other, while four copies at *one* chunking still share one set of chunks and one embedding call.
-- The tail-drop is gone with it: within a single `(doc_id, chunking_key)` the cutting is a pure
-- function of the two, so it can never produce fewer rows than last time. What supersedes a cutting
-- now is that no file row names it any more, and `upsert` deletes exactly those, in the same
-- transaction, for the documents it wrote — the same predicate the sweep uses.
--
-- NULL becomes '' rather than staying nullable: a primary-key column cannot be NULL, and '' is a
-- chunking no binding can produce (`chunk_chars:chunk_overlap_chars` always has digits either side
-- of the colon). Both of the crawl's gates therefore still read a pre-040 row as superseded,
-- exactly as 040 promised — and the row stays *searchable* until the crawl replaces it, instead of
-- vanishing on upgrade, because the file row keeps the matching '' until then.
--
-- **This one is not free.** `ADD PRIMARY KEY` builds a unique index under an ACCESS EXCLUSIVE lock
-- (the migrator's 5 s `lock_timeout` bounds waiting for the lock, not the build). On a share-sized
-- `document_chunks` that is seconds to a minute, once. Applied by `make db-migrate`.
UPDATE document_files SET chunking_key = '' WHERE chunking_key IS NULL;
UPDATE document_chunks SET chunking_key = '' WHERE chunking_key IS NULL;

ALTER TABLE document_files ALTER COLUMN chunking_key SET NOT NULL;
ALTER TABLE document_chunks ALTER COLUMN chunking_key SET NOT NULL;

ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS document_chunks_pkey;
ALTER TABLE document_chunks ADD PRIMARY KEY (doc_id, chunking_key, ordinal);
