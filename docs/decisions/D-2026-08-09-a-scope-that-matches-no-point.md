# D-2026-08-09-a-scope-that-matches-no-point — The group moved to the cutting and the scope stayed at the document

**Status:** accepted · **Date:** 2026-08-09

## Context

`ExternalVectorDocumentIndex.search_dense` is a three-step composition: the catalogue names what is
eligible, the vector store ranks within that scope, the catalogue resolves the ids that come back.
The scope is passed to `VectorStore.search` as a set of *group* keys, and a point's group is the
only thing that set can be compared against.

`D-2026-08-08-a-vector-store-is-not-a-catalogue` established the second step. A later change —
`c8ce657`, "The chunking joined the key, and the external index did not notice" — correctly moved a
point's identity and its group from the document to the *cutting*, because eligibility is decided
per cutting: `_ELIGIBLE` requires `f.chunking_key = c.chunking_key`, so a share that cuts a document
at its own size must never be served another share's cutting of the same text. `_points_for` began
writing `group=group_key(doc_id, chunking_key)`, i.e. `doc-1@400:40`.

`_eligible_documents`, which computes the other side of that comparison, went on running
`SELECT DISTINCT doc_id FROM document_files …` and returning `doc-1`.

## The measurement

The two sides were disjoint, so the intersection was empty for every query:

```
point group written to the store : 'doc-1@400:40'
scope _eligible_documents returns: 'doc-1'
intersection                     : EMPTY -> every scoped search returns []
```

`_eligible_documents`' own docstring records that an empty scope must return no hits rather than
search unscoped — correct, and here it meant the backend answered **every** dense query with `[]`.
Not degraded recall: total retrieval outage on the external vector store, on `main`.

## Why no test saw it

Three tests cover this path and all three stayed green, for two distinct reasons that are worth
separating because only one of them is about stubs.

1. Two of them (`test_an_unfiltered_search_is_still_scoped_to_its_own_source`,
   `test_a_source_with_no_eligible_documents_returns_nothing`) monkeypatch `_eligible_documents`
   with a fake returning bare doc ids. A stub cannot break a contract with its own caller: it
   returns whatever it was written to return, and it was written when doc ids were right. **A stub
   pins the caller's behaviour and simultaneously blinds the test to the stubbed function's
   contract** — which is the entire content of a scoping bug.
2. The third (`test_the_catalogue_is_consulted_even_when_nothing_is_filtered`) does exercise the
   real query, but asserts only that *a* scope was computed and that the SQL mentions the source.
   It never asked what shape the scope has, so it could not notice the shape changing underneath it.

The failure mode is also perfectly camouflaged: a scope that is too narrow returns no hits, and so
does a corpus with nothing eligible. There is no observable difference between "correctly empty" and
"catastrophically empty" at the call site.

## Decision

1. `_eligible_documents` becomes `_eligible_cuttings` and selects `DISTINCT doc_id, chunking_key`.
2. It builds its return value **by calling `group_key`**, the same function `_points_for` uses.
   The two spellings can no longer drift apart without a visible change to one shared function.
3. The name changes with the meaning. `_eligible_documents` returning cuttings would be the same
   mismatch one layer up.
4. The three tests are repaired to match: the stubs return `group_key` values, and the real-query
   test now pins the *shape* — `chunking_key` in the SQL, `{"doc-a@400:40", "doc-b@4000:400"}` out.

## Consequences

The end-to-end regression is
`tests/test_document_share.py::test_the_external_store_backend_carries_the_chunking_through_every_write`,
which drives a real `InMemoryVectorStore` through `upsert` and `search_dense` and asserts on the
**score**: querying with a chunk's own embedding must score 1.0. That is the assertion a stub cannot
fake and the one that failed here. It failed before this change and passes after.

The residual recorded on `_eligible_cuttings` is unchanged and slightly worse in constant factor: an
unfiltered query over a large share now enumerates cuttings rather than documents, so a corpus held
at two chunk sizes builds a filter twice the size. `docs/planning/BACKLOG.md` already carries the row
and names the fix (a source the store can filter on itself); correctness first.

**The general lesson, and it is the one to carry.** This is the third time this campaign that the
defect sat one step from where a fix was applied — a sibling function, the other half of a contract,
the neighbouring rule. `c8ce657` changed one side of an equality and reviewed that side. The
question a review of *any* identity change has to ask is not "is the new identity right" but "who
else spells this identity, and did they move too".
