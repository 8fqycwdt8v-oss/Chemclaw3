-- Which chunking configuration produced each stored document row
-- (D-2026-08-08-a-derived-index-must-record-what-derived-it).
--
-- `038` recorded the embedding configuration and closed half of the identity gap. The other half
-- is where the chunk *boundaries* came from: `chunk_chars` and `chunk_overlap_chars` decide what
-- text each vector describes, and neither was recorded anywhere. Both gates the crawl passes
-- through were therefore blind to a change in them — the file's `mtime_ns:size` fingerprint does
-- not move when a setting does, and `known_documents` asked only about the embedding key — so
-- changing `chunk_chars` re-chunked *nothing*. Measured: 2000 → 400 left the stored chunk sizes at
-- `[1248, 1951, 1962, 1962]`.
--
-- Two columns because there are two gates, and a change has to be visible at both: the file row
-- decides whether the document is re-read and re-chunked at all, the chunk row decides whether its
-- text still needs embedding. One without the other stalls: bust only the file gate and the crawl
-- re-parses every file and then skips the chunking, because the content hash is unchanged and the
-- embedding key still matches.
--
-- NULL reads as "unknown", so the first sync after this migration re-parses and re-chunks each
-- share once. That is more expensive than 038's re-embed — it reads the files off the mount — and
-- it is the price of finding out what chunking the existing rows were cut with, which nothing
-- recorded. It happens once, incrementally, under the crawl's existing bounded passes.
--
-- **Deliberately no index**, for the reason 039 gives: both reads are already scoped by a primary
-- key or by the crawl chunk's own path list, so the key is an extra equality on a handful of rows
-- rather than a scan of the table. Applied by `make db-migrate`.
ALTER TABLE document_files ADD COLUMN IF NOT EXISTS chunking_key TEXT;

ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS chunking_key TEXT;
