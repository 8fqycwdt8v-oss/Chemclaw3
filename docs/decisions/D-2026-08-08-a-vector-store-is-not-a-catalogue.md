# D-2026-08-08-a-vector-store-is-not-a-catalogue — only the dense half is pluggable, and the rest stays in Postgres

**Status:** accepted

## Context

Production will use an external vector database — probably Qdrant, not settled — and the requirement
is that any of them attaches easily. The question is whether the system as built admits one.

**What was already right.** Embeddings touch a very small surface: exactly two `vector(1536)` columns
in the whole schema, `note_index.embedding` (`infra/sql/012`) and `document_chunks.embedding`
(`infra/sql/037`). The ~40 other Postgres modules are relational and stay regardless, and the
molecule/reaction fingerprint store is bitstring Tanimoto rather than pgvector, so it is not in
scope either. Both indexes already sit behind `runtime_checkable` Protocols with two implementations
each — an in-memory reference beside the Postgres one — and every consumer takes its index by
injection. That discipline was paid for to make the loop testable, and it is the same property that
makes a third implementation possible.

**What was missing.** Provider selection. `default_document_index()` returned
`PostgresDocumentIndex()` unconditionally; there was no config token and no factory, unlike
`embedding_provider` one layer over. A tree-wide search found no mention of Qdrant, Weaviate, Milvus
or Pinecone anywhere in `src/` or `docs/`: this had never been designed for.

**The thing that actually decided the design.** `DocumentIndex` is not a vector interface. It has
ten methods and only three are vector work:

| Vector | Relational bookkeeping | Lexical |
| --- | --- | --- |
| `search_dense`, `store_embeddings`, the chunk half of `upsert` | `fingerprints`, `touch`, `prune_stale`, `clock`, `known_documents`, `stale_chunks`, the file half of `upsert` | `search_lexical` |

An adapter asked to implement all ten against Qdrant would be fighting it. Three of them do not
translate at all: `search_dense` resolves its citation with a correlated subquery against
`document_files`, and a vector database has no joins; `clock()` deliberately reads the *backend's
own* `now()` so the mark and the sweep cannot disagree across worker/database skew, and Qdrant
exposes no clock; `search_lexical` is `ts_rank` over a GIN-indexed `tsvector`.

## Decision

### 1. The seam is the dense half; the catalogue stays in Postgres

`chemclaw.retrieval.vectors` declares a three-method `VectorStore` — `upsert`, `search`, `delete` —
and nothing else. That is what a vector database is *for*. The file table, the fingerprint diff, the
mark-and-sweep, the `embedding_key` that makes a model swap self-healing, and the lexical leg do not
move.

The Protocol module imports no client, exactly as `ingest/eln/warehouse/driver.py` does, so
everything above it is exercised in CI against a fake with no server running and no vendor package
installed.

### 2. The pgvector default is not routed through the seam at all

`PostgresDocumentIndex` is untouched in behaviour. Its search is one statement that ranks, filters
and resolves the citation together, and splitting that into three round trips to buy an abstraction
it does not use would make the common deployment slower for nothing. So `pgvector` is not a
`VectorStore` implementation — it is the *absence* of one, `default_vector_store()` says so by name,
and `default_document_index()` is what chooses between the two shapes.

What the base class grew is two hooks, both one-liners: `_require_vector_column` and `_chunk_vector`.
`ExternalVectorDocumentIndex` is a subclass overriding four methods, and five of the ten are
inherited untouched — which is the honest expression of "everything except the dense half is
identical".

### 3. A point carries an id, a vector and one grouping key — and no other metadata

The id addresses the point (`doc_id#ordinal`); the group names what it is a piece of (`doc_id`).
Tags and dates stay in the catalogue.

**Denormalizing them into the payload is the textbook shape and is wrong here.** A tag belongs to a
*path* and a chunk belongs to *content*, so one report filed in two project folders has two tag sets
and one set of chunks — that asymmetry is the whole reason `document_files` and `document_chunks` are
separate tables, and it is what makes the corpus affordable at TB scale. Storing the union of the tag
sets on the chunk would let a tag filter match a chunk whose *other* copy carries the tag: a silent
wrong answer, bought to save a round trip.

A group is different in kind. It is the point's own identity rather than an attribute of some other
row, so it cannot go stale against anything.

### 4. Eligibility is a scope applied before the top-k, never a filter applied after it

The catalogue names the eligible documents and that set is handed *into* the search. This is the
shape `NoteIndex.search_dense(within=...)` already has, and it exists for a measured reason: filter
after the cut and a narrow tag over a wide corpus returns nothing at all, because the k nearest
vectors all belonged to something else. `docs/planning/BACKLOG.md` already records that defect
against pgvector's post-filtering, and Qdrant's filterable HNSW is a large part of why an external
store is worth attaching.

**A scope is computed for every search, including an unfiltered one.** The first cut skipped it when
a query carried no tag and no date window, on the reasoning that an unfiltered query has no
restriction to express. That was wrong, and the review of this ADR's own branch measured it: the
`source` is a restriction and it is *always* present. Every enabled share writes into one collection
— `vector_store_document_collection` is a single setting — so a search that sent no scope took the
top-k across all shares, and `_resolve` then dropped every hit belonging to another source, because
`CITATION_SQL` filters on `%(src)s` and returns NULL for it. The caller silently received fewer than
`top_k` hits, or none. `PostgresDocumentIndex` never had this: `_ELIGIBLE` carries
`f.source = %(src)s` inside the ranking statement. Worse, the inherited `search_lexical` *is*
correctly scoped, so RRF would have fused a correct lexical list with a source-polluted dense one.

Two backends disagreeing about what a search means is a worse failure than either being slow, so the
fast path is gone and the cost moves into the residual below.

### 5. The vendor client is late-bound and is not a dependency

`qdrant-client` is deliberately absent from `pyproject.toml`. The adapter imports it at first use and
raises `VectorStoreConfigError` naming the package when it is not there, rather than an `ImportError`
surfacing from inside a worker. A store nobody configured must not weigh on every pod. This is the
construction `ingest/eln/warehouse/snowflake.py` established and the reason its own engine could ship
proven against a fake driver.

## Consequences

- Attaching a vector database is a config change: `CHEMCLAW_VECTOR_STORE_PROVIDER=qdrant` plus a URL.
  Adding a *different* one is an adapter module implementing three methods and a name in
  `registry.py` — no core edit.
- Every existing deployment is byte-for-byte unaffected. The default is `pgvector`, the default index
  class is unchanged, and the two new hooks return exactly what the code they replaced did.
- The `document_chunks.embedding` column stays and holds NULL under an external store. Not dropped,
  because the schema is shared with the default deployment and a migration removing it would fork
  the two.
- `require_schema_vector_width()` is now inert for an external store. It exists to pre-empt a
  pgvector dimension error, and there is none to pre-empt when nothing writes the column; enforcing
  it would refuse a perfectly good 768-wide deployment over a column it does not use.
- The write ordering survives the split, which is what makes it safe across two systems. Vectors
  first, then the catalogue commit: a crash between them leaves orphaned points that the next run
  overwrites by id and the sweep eventually deletes, whereas a committed file row whose vectors never
  arrived would look *unchanged* to every later crawl and be invisible forever. The original
  argument was about ordering rather than atomicity, so it did not need re-deriving.
- **Nothing has run against a real Qdrant.** The adapter is proven against a fake client and a fake
  models namespace — which is a claim about the calls it makes, not about a server agreeing with
  them. The `BACKLOG.md` row states it, exactly as the warehouse ELN connector's does.
- `note_index` is deliberately left on pgvector. It has an open item of its own (no embedding-model
  identity, no chunking), any move is a full re-embed anyway, and generalizing the seam to a second
  consumer before the first has run against a live server would be designing against a guess.

## The chunking joined the key, concurrently, and the composition did not notice

Recorded because it is the failure mode this design is most exposed to, and it happened within a day
of the seam landing.

`D-2026-08-08` (the chunking-key work) made a chunk's identity `(doc_id, chunking_key, ordinal)` —
`infra/sql/041` — on a branch running beside this one. Neither change was wrong alone, and both
merged cleanly, because they touched different lines. Together they were wrong in four places at
once, all of them in the external index:

- the vector point was addressed `doc_id#ordinal`, so **two cuttings of one document collided on a
  single point** — a re-tuned `chunk_chars` would have had the finer cutting silently overwrite the
  coarser's vectors while the catalogue held both sets intact;
- `store_embeddings` addressed rows by `(doc_id, ordinal)`, so a re-embed stamped the current
  embedding key onto the *superseded* cutting's rows as well;
- the sweep carried its own hand-written `NOT EXISTS`, which the base had already extended with the
  chunking — two spellings of "orphan" across two stores;
- and `upsert`'s per-write cleanup deleted the previous cutting's rows in Postgres while their
  vectors stayed in the store forever.

**No test failed.** The offline suite covers the seam, the adapter and the point-id contract; the
external index's Postgres statements run only where a database does, which this ADR's own backlog row
already said. The gap it named is exactly the gap that let this through.

Two things changed as a result. The point id is now the *whole* primary key, and a scope groups by
`(doc_id, chunking_key)` rather than by document — because `_ELIGIBLE` joins on the cutting, so a
share must never be served another share's cutting of the same text. And `CLAIMED_SQL` and
`_forget_vectors` exist so the subclass cannot hold a second, drifting copy of "what is an orphan"
or silently skip reclaiming what the base deleted.

The general lesson is about subclassing across a concurrent change: five inherited methods are five
places a base can move underneath you, and the compiler sees none of it when the change is to a
*key* rather than to a signature. mypy caught exactly one of the four (a tuple width); the rest were
found by reading.

## Alternatives rejected

**Implement all ten `DocumentIndex` methods against Qdrant.** It is the shape that looks generic and
it forces the catalogue into a store with no joins and no clock — so either the citation join gets
denormalized (§3, a correctness regression) or the mark-and-sweep loses the single-clock property
that keeps it from deleting live files.

**Route pgvector through the same seam for symmetry.** It reads better and costs the default
deployment two extra round trips per search to exercise an indirection it does not need. Symmetry is
not worth a latency regression on the path everyone is on.

**Denormalize tags and dates onto the vector payload.** Faster, standard, and wrong for this corpus —
§3.

**Add `qdrant-client` as a real dependency.** Simpler to test and it puts a client on every pod for a
store most deployments will not use, which is the cost the warehouse seam already declined to pay.
