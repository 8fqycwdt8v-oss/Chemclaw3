# `chemclaw/retrieval/vectors/` — where dense embeddings live

**Responsibility:** the seam that lets a deployment keep its vectors somewhere other than Postgres,
without any caller knowing. Decision record:
`docs/decisions/D-2026-08-08-a-vector-store-is-not-a-catalogue.md`.

| Module | What it is | Imports a client? |
| --- | --- | --- |
| `base.py` | the `VectorStore` Protocol, its records and its two error types | no |
| `memory.py` | the in-memory reference — exact cosine, what the tests measure against | no |
| `qdrant.py` | the Qdrant adapter; the vendor client is late-bound | only when called |
| `databricks.py` | the Mosaic AI Vector Search adapter; same late binding | only when called |
| `registry.py` | `vector_store_provider` → an implementation, imported inside its own branch | no |

That last column is the layout, not an accident — the same rule
`ingest/documents/retriever.py` follows about document parsers, and the same construction
`ingest/eln/warehouse/driver.py` uses for the warehouse: the Protocol module imports nothing, so
everything above it is exercised in CI against a fake, on a machine with no vector database running
and no vendor package installed.

**Neither vendor client is a dependency of this repository.** A store nobody has configured must
not weigh on every pod, so each adapter imports its client at first use and, when it is absent, says
which package to install rather than raising `ImportError` from inside a worker.

## What an operator must create

*Qdrant*: a collection created with **cosine** distance and a payload index on `group`. Qdrant
returns the configured distance's score and `VectorMatch.score` is a cosine, so a collection built
with `Distance.DOT` or `Distance.EUCLID` would return numbers in another range and every fusion
above would silently mis-rank.

*Databricks*: a **Direct Vector Access** index — a Delta Sync index computes its own embeddings from
a source table and cannot be upserted or deleted into, which is the whole of what this seam writes.
Its schema must declare `id` (the primary key), `embedding` (the vector column) and `group_key`
(`group_key` rather than `group` because `GROUP` is a SQL keyword and the column is queryable from
Databricks SQL). `vector_store_document_collection` is then a **three-level Unity Catalog name**, not
a bare word, and `vector_store_endpoint_name` names the endpoint serving it — an index is addressed
by the pair.

**Databricks does not return a cosine, and the adapter converts.** It ranks by `1/(1 + d²)` over
Euclidean distance. That conversion is exact only for unit vectors, so `databricks.py` normalises on
write *and* on query and then inverts it (`cos = 1.5 − 0.5/score`). Normalising is also what makes
its L2 ordering agree with pgvector's cosine ordering in the first place — without it the two
backends would disagree about which document is nearest and nothing would fail.

## What moves to the store, and what does not

Only the dense half. The catalogue — `document_files`, the chunk text, the fingerprint diff, the
mark-and-sweep, the `embedding_key` that makes a model swap self-healing, and the lexical leg —
stays in Postgres. A vector database has no joins to resolve a citation with, no clock for a sweep
to measure against, and no `ts_rank`. `ingest/documents/external_index.py` is the composition, and
it is a *subclass* of the Postgres index precisely because five of the ten `DocumentIndex` methods
are unchanged.

**The pgvector default is not routed through this seam at all.** Its search is one statement that
ranks, filters and resolves the citation together, and splitting that to buy an abstraction it does
not use would make the common deployment slower. `pgvector` is therefore not a `VectorStore`
implementation — it is the absence of one, and `default_document_index()` is what chooses.

## Two properties worth reading before changing anything

**A point carries an id, a vector and one grouping key — nothing else.** Tags and dates stay in the
catalogue. Putting them in the payload is the textbook shape and wrong here: a tag belongs to a
*path*, a chunk belongs to *content*, and one report in two project folders has two tag sets and one
set of chunks. Their union in a payload would let a tag filter match a chunk whose *other* copy
carries the tag.

**Eligibility is a scope applied before the top-k, never a filter applied after it.** Filter
afterwards and a narrow tag over a wide corpus returns nothing, because the k nearest vectors all
belonged to something else — the recall defect `docs/planning/BACKLOG.md` already records against
pgvector's post-filtering, and a large part of why an external store is worth attaching.

**A scope is computed for every search, including an unfiltered one**, because the `source` is a
restriction and it is always present. Every enabled share shares one collection, so a search that
sent no scope took the top-k across all of them and then silently dropped the other sources' hits
while resolving. Skipping the scope query for an unfiltered search was a real bug, and it is the one
optimization not to reintroduce here — `tests/test_vector_store.py` pins it from both ends.

**The residual, stated:** that scope is built in Postgres and sent to the store, so an unfiltered
query over a million-document share builds a million-id filter. It is a ceiling on how far this
composition scales as written, and `BACKLOG.md` names both the fix and why it is not small (content
dedup means a share inheriting an already-embedded document writes no point, so there is nothing to
hang a `source` payload on).

## Adding another store

One module implementing three methods, one name in `registry.py`, one value in the
`vector_store_provider` literal. No core edit, and nothing else in the tree learns the name.

Two things a second adapter is easy to get wrong, both silent: **the score must be the cosine this
seam's contract promises**, whatever the vendor ranks by — check the vendor's own definition rather
than assuming — and **a blocking client must cross `asyncio.to_thread`**, because a retriever runs
inside a `gather` and one synchronous call stalls every other leg of the fan-out.
