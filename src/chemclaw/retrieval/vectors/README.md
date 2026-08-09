# `chemclaw/retrieval/vectors/` — where dense embeddings live

**Responsibility:** the seam that lets a deployment keep its vectors somewhere other than Postgres,
without any caller knowing. Decision record:
`docs/decisions/D-2026-08-08-a-vector-store-is-not-a-catalogue.md`.

| Module | What it is | Imports a client? |
| --- | --- | --- |
| `base.py` | the `VectorStore` Protocol, its records and its two error types | no |
| `memory.py` | the in-memory reference — exact cosine, what the tests measure against | no |
| `qdrant.py` | the Qdrant adapter; the vendor client is late-bound | only when called |
| `registry.py` | `vector_store_provider` → an implementation, imported inside its own branch | no |

That last column is the layout, not an accident — the same rule
`ingest/documents/retriever.py` follows about document parsers, and the same construction
`ingest/eln/warehouse/driver.py` uses for the warehouse: the Protocol module imports nothing, so
everything above it is exercised in CI against a fake, on a machine with no vector database running
and no vendor package installed.

**`qdrant-client` is not a dependency of this repository.** A store nobody has configured must not
weigh on every pod, so the adapter imports it at first use and, when it is absent, says which
package to install rather than raising `ImportError` from inside a worker.

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

**The residual, stated:** a filtered query builds its scope in Postgres and sends it to the store,
and that set is unbounded in principle. It is bounded in practice by what a filter is *for* — a tag
exists to narrow — and an unfiltered query, the common case, sends no scope and pays nothing. The
row is in `BACKLOG.md`; the fix trades away correctness the two-table design buys, so it waits for a
corpus that actually feels it.

## Adding another store

One module implementing three methods, one name in `registry.py`, one value in the
`vector_store_provider` literal. No core edit, and nothing else in the tree learns the name.
