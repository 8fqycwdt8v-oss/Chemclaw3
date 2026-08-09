# D-2026-08-08-a-rollback-that-is-not-a-schema-step — a rollback that is not a schema step

**Status:** accepted · **Date:** 2026-08-08 ·
**Supersedes:** [D-2026-08-04-the-schema-only-goes-forward](D-2026-08-04-the-schema-only-goes-forward.md)
(amends its rule and its check; the forward-only, no-down-path decision stands unchanged)

## Context

`infra/sql/041_document_chunk_identity.sql` fixes silent data destruction
(D-2026-08-08-a-derived-index-must-record-what-derived-it): `document_chunks` was keyed on
`doc_id`, a *content* hash shared across sources by design, while `chunking_key` comes from a
*per-share* binding — so two shares holding one document at different chunk sizes fought over the
same rows, one share's re-chunk deleted the other's, and the victim never repaired. 041 makes a
chunk row's identity `(doc_id, chunking_key, ordinal)`.

It failed `tests/test_migrations_are_additive.py`, and the failure was **right for the wrong
reason**:

```
041_document_chunk_identity.sql contains a destructive statement
('ALTER TABLE document_chunks DROP')
```

The match is `ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS document_chunks_pkey` — a
primary-key rebuild. It destroys no data. Meanwhile the statement three lines above it,
`ALTER TABLE document_files ALTER COLUMN chunking_key SET NOT NULL`, is what actually ends the
rollback D-2026-08-04 rests on, and the check did not look at it. One pattern, wrong in both
directions.

## Measured, not argued

A scratch database (Postgres 16 + pgvector 0.8.0), migrations 000→040 applied, populated by the
**previous image's own statements** (`origin/main`, which writes no `chunking_key` anywhere), then
041 applied. The previous image's document-index statements were then run verbatim, in the order
`cli/sync_share.py` runs them.

| Previous-image operation | Result after 041 |
| --- | --- |
| retriever search + citation join (`SELECT`) | works — 3 chunks over 2 file rows |
| `known_documents` gate (`SELECT`) | works |
| `touch` unchanged paths (`UPDATE … indexed_at`) | works (`UPDATE 1`) |
| `upsert` a `document_files` row | **ERROR** `null value in column "chunking_key" … violates not-null constraint` |
| `upsert` a `document_chunks` row | **ERROR** `there is no unique or exclusion constraint matching the ON CONFLICT specification` |
| net effect on stored rows | 2 files / 3 chunks before → **2 files / 3 chunks after** |

Both errors, and one more fact than the review had: the file upsert fails for an *existing* path
too, because the NOT NULL is checked on the proposed tuple before the conflict is resolved. The
previous image cannot write the document index at all. It can still read all of it, and — because
the crawl raises inside `DocumentIndex.upsert` and `prune_share` runs only after a whole drain
returns — it never reaches its own sweep. **Nothing is deleted.**

### Could 041 be restructured so the previous image keeps working?

Measured rather than assumed, because it is the option that would need no exemption.

**Keep a unique `(doc_id, ordinal)` beside the new key**, so the previous image's
`ON CONFLICT (doc_id, ordinal)` still resolves. The first thing 041 exists to permit — a second
share cutting the same document at its own chunking — is then rejected:

```
ERROR: duplicate key value violates unique constraint "document_chunks_old_identity"
DETAIL: Key (doc_id, ordinal)=(doc1, 0) already exists.
```

The constraint that makes the previous image's upsert resolve **is** the constraint that permitted
the data destruction. The two cannot both hold.

**Fold the chunking into `doc_id`** instead of into the key columns, so the pre-041 primary key
survives untouched. This one does let the previous image run — and buys it by making the rollback
destroy the index. The previous image's crawl rewrites file rows with the bare content hash it
knows, and its orphan sweep is
`DELETE FROM document_chunks c WHERE NOT EXISTS (SELECT 1 FROM document_files f WHERE f.doc_id = c.doc_id)`.
Measured on a corpus of one document cut two ways: **17 chunk rows before, 0 after.** It also
requires editing `040`, which is merged and immutable here, and it retires `doc_id`'s meaning as
"the hash of the parsed text" — the property 037 built the two-table split, the cross-source
deduplication and the citation join on.

**Restore the pre-041 primary key as a down-step**, once the newer image has run:

```
ERROR: could not create unique index "document_chunks_pkey"
DETAIL: Key (doc_id, ordinal)=(doc1, 0) is duplicated.
```

A schema down-path for 041 is not merely undesirable, it does not apply: it requires deleting one
share's chunks first, which is the destruction 041 exists to stop.

## Decision

**Two rules, because the measurement found two different things, with two different answers.**

1. **A migration may not destroy data.** No exemption, ever — rollback cannot bring rows back.
   `DROP TABLE`, `DROP COLUMN`, `RENAME`, `TRUNCATE`, `DELETE FROM`. This is D-2026-08-04's rule,
   unchanged, with `DROP CONSTRAINT` and `DROP INDEX` removed from it because neither removes a row.
2. **A migration may not leave the previous image unable to write — except by explicit review.**
   `SET NOT NULL` on an existing column, and dropping or replacing a key. The data survives; what
   ends is "deploy the previous image", and whether that is acceptable is a judgement about one
   migration rather than a rule. An exempted migration is listed in
   `_REVIEWED_ROLLBACK_BREAKS` with **the exact statements reviewed** and the ADR carrying its
   rollback procedure. The list is exact-matched, so adding a break to an exempted migration fails
   as loudly as adding one to any other.

**041 takes that exemption.** The alternative is shipping a schema whose primary key permits one
share to delete another's chunks, and the rollback it costs is a degradation rather than a loss.

**The check's scope is stated rather than implied.** It flags *unconditional* breaks — statements
that fail every write regardless of what is in the table. A `CHECK` constraint or a
`CREATE UNIQUE INDEX` on an existing table narrows what may be written but rejects only some rows;
four merged migrations (014, 016, 017, 037_bo_suggestion_provenance) do exactly that. Flagging them
would be over-reach dressed as rigour, and leaving it unsaid would be the omission D-2026-08-04 was
written against, so it is a row in `docs/planning/BACKLOG.md` with a trigger.

## The rollback procedure for 041, in plain words

"Deploy the previous image" is no longer the whole answer for the document index. It is still the
whole answer for everything else — 041 touches `document_files` and `document_chunks` and nothing
else.

1. **Deploy the previous image.** Document search keeps working, over every row indexed up to the
   moment of rollback. Citations resolve. No other subsystem is affected.
2. **Stop the share sync.** Its every run will now fail with
   `null value in column "chunking_key" … violates not-null constraint`. Remove the share from
   `CHEMCLAW_DATA_SOURCES` (or disable its schedule) so a known, harmless failure is not paged
   repeatedly. Nothing is lost by stopping it: it could not index anything either way.
3. **Expect a frozen index, not a damaged one.** New and changed files on the share are not indexed
   until the newer image is deployed again. Files already indexed stay searchable. The crawl aborts
   before it reaches its sweep, so it deletes nothing — measured above, 2 files / 3 chunks before
   and after.
4. **Do not try to restore the pre-041 primary key.** It fails outright once two chunkings exist,
   and forcing it means deleting one share's chunks — the destruction 041 exists to stop.
5. **Rolling forward again needs no repair.** The newer image's crawl resumes: the pre-041 rows
   carry `chunking_key = ''`, which both gates read as superseded, so they are replaced by the
   first crawl and stay searchable until they are.

## Consequences

**The gate now says what it means**, so its green means something it did not before: no merged
migration destroys data, and exactly one is known to end the previous-image rollback, with the
statements read and the procedure written.

**An exemption costs an ADR.** That is the point — the exemption's entire content is the paragraph
above, and the failure mode this replaces is discovering during an incident that the rollback plan
of record does not apply.

**A future migration of this shape is cheap to review and hard to sneak in.** The check names the
statement and the reviewed set is exact, so the conversation starts at "what does an operator do
instead" rather than at "is this destructive".
