# D-2026-09-05-an-approximate-neighbour-list-is-a-different-answer — exactness is a deployment's choice, and the answer says which it got

**Context.** Fingerprint similarity search is the query behind "have we ever made something like
this?". `science/fingerprints/store.py`'s class docstring claimed it used the HNSW index. It did
not: the `definition = …` and `1 - (bits <%> q) >= threshold` predicates prevent the index from
ordering, so the planner takes a sequential scan. Measured at 200k rows that is **17.6 ms and
exact**, ~0.088 µs/row — so **~880 ms at 1M compounds and ~5.9 s at 10M**, and Pistachio on
Databricks is the named first integration.

Restructuring so the index orders is **1.25 ms, 14x faster**. It also returned a **different result
set for 22 of 60 queries** at `ef_search=200` with a 10x over-fetch. That difference is **ties, not
recall**: Tanimoto over sparse bit vectors puts many rows at identical similarity, the exact
`ORDER BY distance, id COLLATE "C"` breaks those ties across the *whole* table, and no truncated
candidate set can reproduce a whole-table tie-break. A separate attempt to keep exactness by
hoisting the three distance computations into one subquery was measured and is **slower** — 28.7 ms
against 18.0 ms.

So this is not a performance decision with a right answer. It is a choice between two different
answers to a chemist's question, and the two are not distinguishable from the result.

**Decision.** Exactness is a **per-deployment setting**, `fingerprint_search_exactness`, defaulting
to `exact`.

1. *Default exact, because of which failure is worse.* The approximate arm's failure mode is a
   silent "no precedent found" for a structure that is on file — the one outcome this module is
   arranged against. A site at Pistachio scale can trade that away knowingly; no site should get it
   by default.

2. *Two paths that read as two paths*, rather than one path with a flag threaded through it. The
   approximate arm over-fetches (`fingerprint_approximate_overfetch`, default 10x) and re-ranks, and
   raises `hnsw.ef_search` (`fingerprint_approximate_ef_search`, default 200) because pgvector's own
   default of 40 is far too narrow for a page of 100 at that over-fetch.

3. **The answer carries which arm produced it.** `approximate` travels in the payload for the same
   reason `index_empty` does, and the *wording changes with it*: the approximate arm never reports a
   "genuine" absence, and its neighbour list is described as what the index proposed rather than as
   the nearest. This is the half that makes the setting safe to offer at all — the sibling fleet's
   rule that a result carries its method and its caveat (`props` returns both beside every value),
   applied here because a neighbour list without its method is not something a chemist can put in a
   report.

**What was fixed on the way and is not part of the choice.** Three docstring sentences claimed the
HNSW index was used and were false for as long as the predicates have been there; a test now pins
exactness on the default arm so the trade cannot be taken by accident. And the review that opened
this found a larger defect next door that had nothing to do with either arm: `all_records`'
`ORDER BY id COLLATE "C"` could not use the primary key, so a 200k-row scan was an external merge
sort spilling **136 MB to disk at 2,228 ms** — in front of the 990 ms of RDKit everyone was looking
at. One collation-matched btree makes it **10.7 ms**.

**Deliberately left open.** The agreement rate between the two arms is measured at one corpus and
one shape (22 of 60 differing in ties). A site turning `approximate` on at 10M rows should measure
its own, and the setting's docstring says so rather than implying the 10x over-fetch is a universal
constant.
