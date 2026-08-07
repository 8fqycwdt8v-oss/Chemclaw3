# D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it — the embedding configuration is part of a vector's identity

**Status:** accepted

## Context

`D-2026-08-06-a-share-is-mounted-not-called` shipped the mounted-share corpus. Reviewing its
vectorization path afterwards turned up two gaps with one root: **the schema stores vectors and
records nothing about which embedding configuration produced them.**

**1. Changing the embedding model silently corrupted the corpus.** The crawl decides what to
re-read by diffing each file's `mtime_ns:size` fingerprint. That does not move when the *model*
changes. So pointing a deployment at a better embedding model re-embedded nothing;
`document_chunks` came to hold a mix of two models' vectors, queries embedded by the new model
were compared by cosine against the old model's, and the comparison is meaningless. Nothing
failed. Retrieval simply started returning confident nonsense, which is the failure mode this
repository has an entire standing rule about.

`core/embeddings.py` already had this right *in memory*: its cache is keyed on
`(provider, model, dim, text)` precisely because "a vector is only reusable for the configuration
that made it" (D-011). The rule existed; it just wasn't durable.

**2. The startup width guard covered `note_index` only.** `core/config/__init__.py` fails startup
when `embedding_dim` disagrees with `note_index`'s `vector(1536)` — but only when a
`NOTE_INDEX_SOURCES` name is enabled. A deployment enabling `sharedrive` *without* `vector` or
`lexical` started cleanly and then had pgvector reject every chunk write, hours later, inside a
worker. The previous ADR's plan said this check would be extended and it was not.

## Decision

### One constant for the schema's vector width

`_NOTE_INDEX_VECTOR_DIM` becomes `SCHEMA_VECTOR_DIM`. There is no coherent deployment in which two
vector columns in one database have different widths — they are written from one `embedding_dim`,
by one provider seam, and compared against queries from that same seam. Two private constants
meaning the same fact is how the two come to disagree
(`D-2026-08-05-one-rule-in-three-places-is-three-rules`).

### The width guard sits on the constructors, not in config

`require_schema_vector_width()` in `ingest/documents/index.py`, called from
`PostgresDocumentIndex.__init__` and `ShareDocumentRetriever.__init__`.

**Not in the config validator, and the reason is structural.** The note-index check can live there
because `vector`/`lexical` are *shipped* names, so `NOTE_INDEX_SOURCES` enumerates them. A document
share's name is chosen by the deployment — `sharedrive` is only the shipped example, and a site
mounts its own manifest folder under any name it likes — so no name set can identify one. Answering
"is a share enabled?" requires importing its retrieve half, and `chemclaw.core` may import no
sibling (`tests/test_layering.py`).

The two constructors are every path that reaches the column: the first query, the first crawl, and
`validate_datasources --construct`. **The residual is stated rather than hidden:** it fires at first
use, not at process start. That is a message naming both numbers instead of a pgvector type error
surfacing from a worker after a deploy that looked clean.

### The embedding configuration is stored with the vector, and stale vectors are refreshed

`embedding_config_key()` is extracted in `core/embeddings.py` as the one definition of "which
configuration makes a vector right now", and the in-memory cache is rebuilt on top of it. Migration
`038` adds `document_chunks.embedding_key`; NULL reads as unknown and therefore stale, exactly the
argument `035_note_index_fingerprint.sql` makes for its own added column.

`reembed_stale()` refreshes chunks whose key is not current, and
`DocumentShareSyncWorkflow` drains it **before** the crawl — a vector made by a superseded model is
*wrong now*, being compared against freshly embedded queries, whereas a document not yet crawled is
merely absent.

**It reads the database, never the share.** The chunk's text was already stored beside its vector,
so a model swap is a database-to-database operation: no crawl, no mount, no parse. It also makes
progress on a run where every mount is unavailable. `tests/test_document_share.py` deletes the share
tree before re-embedding, so if that ever stops being true a test says so.

`known_documents()` is keyed on the configuration too, not merely on presence — otherwise a copy of
an already-indexed document arriving under a new path would inherit the old model's vector, leaving
one document in the corpus that nothing else is comparable to.

## Consequences

- A model change now heals itself on the next scheduled run, bounded by
  `document_reembed_batch_size` and sharing the workflow's existing `continue_as_new` budget.
- `DocumentShareSyncWorkflow.run` returns `DocumentSyncOutcome(shares, reembedded)` rather than a
  bare list, so a run reports the refresh it performed.
- One breaking-ish signature change inside the package: `DocumentIndex.upsert` and
  `known_documents` take the key. Both are a week old and have no external callers.
- A cosmetic defect from the previous change is fixed in passing: the document-share config block
  had been inserted *between* the vendored-dataset comment and the field it describes.

## Alternatives rejected

**A `--full` flag on the sync CLI.** The cheapest possible fix, and wrong for the same reason the
defect was invisible: it is a manual step nobody performs at the moment they change a setting, and
the failure it guards against raises no error. A remedy that depends on someone already knowing
about the problem does not close a silent problem. The same objection kills "document that you
should drop the two tables and re-run".

**Storing the key on `document_files` instead.** Embedding is a property of a document's chunks,
not of a path — four paths share one document's vectors, so the key would have four places to
disagree.

**Making the file fingerprint include the embedding key.** It would force a re-*read* and re-*parse*
of every file on the share for a change that only affects the vector, and would fail entirely while
a mount was down. The whole value of keeping `content` in the table is that none of that is needed.
