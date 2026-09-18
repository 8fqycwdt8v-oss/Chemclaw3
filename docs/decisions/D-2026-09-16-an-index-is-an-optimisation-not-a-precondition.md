# D-2026-09-16-an-index-is-an-optimisation-not-a-precondition — two budgets, a per-corpus lock, and the loop back as the floor

`D-2026-09-16-a-library-already-in-the-closure-is-a-declaration-not-a-dependency` adopted RDKit's
`rdSubstructLibrary` for `find_substructure_matches`, and the adoption is sound: 8-25x on every query
class measured, with `useChirality` diffed rather than assumed. Three things it did are not, and the
first turns a slow answer into no answer at all.

## What was found

**R1 — the build was charged against the match budget, so a large corpus failed permanently.**
`index_for` built the index under `substructure_match_timeout_seconds`, and `_build` caches nothing
when it is abandoned, so a corpus whose build outran that bound could never produce an index and
therefore never produce an answer. Measured on RDKit's own `first_5K.smi` at the shipped 5.0 s:

| corpus | this branch | the per-record loop it replaced |
| --- | --- | --- |
| 4,999 records | build 1.2-2.6 s, answers | 0.37-0.47 s |
| 19,996 records | **TimeoutError on 3 of 3 attempts** (gave up indexing at 11,776 / 13,440 / 14,976) | 2.01 s, 2,684 hits |

It is reachable by following this tree's own advice: `search.py` logs "raise
CHEMCLAW_SUBSTRUCTURE_SCAN_MAX_RECORDS or narrow the corpus" when the cap truncates, and neither
`.env.example` nor `core/config/fingerprints.py` said that raising the cap without raising the
timeout converts truncated-but-useful answers into 100% failure. The message the chemist got was
wrong in the same direction — *"substructure match for 'C(=O)N' exceeded 5.0s over 19996 molecules;
narrow the pattern"* — over a scan in which the pattern had never been matched once, naming the one
remedy that could not help.

**R2 — one process-global lock refused a query whose index was already cached.** The lock was taken
on every call, hit or miss, and held across the whole build. Driven with two threads, the second
query's corpus already in the map: **TIMEOUT at 1.0014 s** against its 1.0 s bound. Before this
module existed the two ran in separate `to_thread` workers and neither waited. It is worse under
ingest, where the key is a digest of the labels, so one new molecule mints a new corpus and every
concurrent query serializes behind one build.

**R3 — the cold query is ~3x the code it replaced**, which the module docstring stated and nothing
acted on.

## The decision

**The build has its own budget.** `substructure_index_build_timeout_seconds` (3.0 s) bounds
building; `substructure_match_timeout_seconds` bounds answering. How long an index may take to build
and how long a chemist waits for an answer are different questions and were sharing one number.

**A missing index is skipped, never fatal.** `index_for` returns `None` — for a caller already out
of time, for a corpus too large for the build budget, for a peer's build this caller cannot wait out
— and `_match_record_by_record` answers exactly as this function did before the index existed. The
floor an optimisation has to beat is also the floor it must fall back to. The 19,996-record corpus
now answers in ~1.5 s on 3 of 3 attempts.

**The budget is a projection rather than a stopwatch.** Every `_BUILD_CHECK_STRIDE` records the
build extrapolates its own measured rate over the whole corpus and refuses the moment the projection
exceeds the budget, so an unindexable corpus costs ~0.05 s to identify instead of the whole 3.0 s —
which is what leaves the fallback scan its own bound intact, and why no memory of the refusal is
needed to keep the cost off later queries.

**One lock per corpus, not per process.** `_BUILDS` maps the corpus digest to a `_BuildSlot`; the
module-wide `_GUARD` is held for a map operation and never across a build. Single-flight is
preserved (a second caller waits on *that* corpus's slot and then finds the entry) and a cache hit
answers in 0.0059 s while an unrelated corpus builds. The slot is reference-counted and dropped by
its last holder, because a digest is a corpus *generation* and a lock per generation kept forever is
the unbounded-growth shape `core/bounded.py` exists for.

**R3 is accepted rather than fixed, and the numbers are why.** The three paths over 4,999 records at
the shipped `fingerprint_max_top_k`, best of three: no index 334-379 ms, cold 1,176-1,225 ms, cached
10-26 ms — behind by ~0.85 s once, ahead by ~0.35 s thereafter, in front after **2.3-2.6 queries** on
one corpus generation. The two designs that would remove the one-off — building in a background
thread, or building only on the *second* miss of a digest — both make "one build serves every later
query" unobservable from a single call, so the concurrent-miss and rebuild-on-change assertions in
`tests/test_molfp.py` become races rather than facts. The case this does not cover is named rather
than hidden: an ingest that rewrites the corpus between every query never reuses an index, and what
answers that is the database-side `pattern_bits` screen still open in `docs/planning/DEFERRED.md`,
not a longer-lived in-process index over a slice that is already stale.

**What it costs**, stated because it is a real behavioural change: the fallback keeps *parsing*
after the hit cap so that `unreadable` — and the `scan_truncated` it folds into — describes the
corpus on both paths rather than depending on which one ran. For a broad query that fills the cap
early, that is 334 ms against the 40 ms an early-stopping loop would take. It is paid only when
there is no index.

## What keeps it true

- `tests/test_molfp.py::test_a_corpus_too_large_to_index_is_searched_record_by_record_instead_of_refused`
- `tests/test_molfp.py::test_a_build_that_cannot_meet_its_budget_costs_the_query_a_fraction_of_that_budget`
- `tests/test_molfp.py::test_a_query_whose_index_is_cached_is_not_blocked_by_an_unrelated_corpus_building`
- `tests/test_molfp.py::test_concurrent_misses_on_one_corpus_build_one_index` (the single-flight half, unchanged)
- `tests/test_molfp.py::test_the_refusal_names_the_scan_that_ran_out_of_time_and_a_remedy_that_works`
- `tests/test_molfp.py::test_a_caller_with_no_time_left_builds_nothing_and_is_told_what_ran_out`
- `tests/test_config.py::test_env_example_documents_every_field` (the new budget is documented where an operator reads)
