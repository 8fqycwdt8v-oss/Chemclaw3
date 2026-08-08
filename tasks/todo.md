# Task — a pluggable vector store, so the corpus is not married to pgvector

**Ask:** production will use an external vector database (probably Qdrant, not settled). The seam
must be generic enough that any of them attaches without a core edit.

**Finding that shaped the plan** (from the investigation before it): embeddings touch a very small
surface — exactly two `vector(1536)` columns in the whole schema — and both indexes already sit
behind `runtime_checkable` Protocols with an in-memory reference beside the Postgres one. What is
missing is *provider selection*: `default_document_index()` hardcodes `PostgresDocumentIndex()`, and
nothing in the repo has ever mentioned an external vector store.

`DocumentIndex` is not a vector interface. Only 3 of its 10 methods are vector work; the rest is
relational bookkeeping (fingerprints, marks, sweep, the backend's own clock) plus a lexical leg that
is Postgres FTS. An adapter implementing all ten against Qdrant would be fighting it.

## Design

**The narrow seam is the dense-vector half, and the catalogue stays in Postgres.** A vector database
stores vectors; the file table, the fingerprint diff, the mark-and-sweep and the citation join are
relational work that Qdrant has no joins for and no clock to offer.

**The Postgres default is not rewired.** `PostgresDocumentIndex` keeps its single-statement
search — rank, eligibility and citation in one SQL round trip — because splitting it would make the
default deployment slower to buy an abstraction it does not use. The composition exists only on the
path that needs it.

**Eligibility is a scope passed *into* the vector search, not a filter applied after it.** This is
the `NoteIndex.search_dense(within=...)` pattern already in the codebase, and it is what keeps
recall correct: filtering after the top-k returns nothing when the k nearest all belong to another
tag. Residual stated rather than hidden — a filtered query over a very large corpus computes a
doc-id scope in Postgres first, and that set is unbounded in principle. An unfiltered query (the
common case) passes no scope and pays nothing.

**Not chosen: denormalizing tags/dates onto the chunk payload.** It is the textbook vector-DB shape
and it is wrong here: tags are per *path*, chunks are per *content*, and one document in two folders
has two tag sets. Storing their union would let a tag filter match a chunk whose other copy carries
the tag — a silent correctness regression traded for a round trip.

## Steps

- [x] Study the driver-seam precedent (`ingest/eln/warehouse/driver.py`, `connect.py`)
- [x] `retrieval/vectors/base.py` — the `VectorStore` Protocol, records, errors; imports no client
- [x] `retrieval/vectors/memory.py` — the in-memory reference
- [x] `retrieval/vectors/qdrant.py` — the adapter, vendor client late-bound, proven against a fake
- [x] `retrieval/vectors/registry.py` — provider selection from config
- [x] `retrieval/vectors/README.md` + the `ARCHITECTURE.md` row
- [x] `ExternalVectorDocumentIndex` — Postgres catalogue + `VectorStore`, implementing `DocumentIndex`
- [x] Config section + `default_document_index()` selecting on it
- [x] Tests: the seam, the adapter against the fake, provider selection, the point-id contract
- [x] ADR + ledger row + `.env.example` + the operator guide + BACKLOG residuals
- [x] `make lint type test` green — 3518 passed, 143 skipped

## Review

**What shipped.** A three-method `VectorStore` seam, an in-memory reference, a Qdrant adapter whose
vendor client is late-bound and is *not* a dependency, provider selection on
`CHEMCLAW_VECTOR_STORE_PROVIDER`, and `ExternalVectorDocumentIndex` — a subclass of the Postgres
index overriding four methods and inheriting five untouched. The default deployment is unchanged in
behaviour; `pgvector` never enters the seam.

**Two things went differently from the plan, both worth recording.**

*The scope had to become a group, not an id set.* The first cut passed the catalogue's eligible
`doc_id`s to a store whose points are keyed `doc_id#ordinal` — they would have matched nothing, and
every filtered search would have returned empty. A point now carries one grouping key and the scope
matches on that. This is also the only place a *generic* seam had to learn something about its
caller's shape, and one field is the smallest version of it.

*A defaulted field produced a real bug in the length of one edit.* `VectorPoint.group` falls back to
the point's own id, which is right for anything embedded whole and wrong for a chunk. Two call sites
built points — the crawl and the re-embedding drain — and only the first passed the group, so a
re-embedded chunk would have been filed under `doc-abc#3`, answering unfiltered questions and
vanishing from filtered ones, with nothing raised. Fixed by making one builder both callers use;
pinned by two tests, both mutation-checked (they fail when the group is removed).

**What is not proven.** Nothing has run against a real Qdrant, and the external index's three
Postgres statements run only where a database does — both are `BACKLOG.md` rows with triggers, not
claims. The fake agrees with the adapter about the calls it makes; a server agreeing with them is a
different statement.
