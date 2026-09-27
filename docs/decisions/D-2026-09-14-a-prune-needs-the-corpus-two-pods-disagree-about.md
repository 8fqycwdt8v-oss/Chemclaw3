# D-2026-09-14-a-prune-needs-the-corpus-two-pods-disagree-about — the note reindex retires against one pod's disk, and a commit count is the fact two pods share

## Status

Accepted.

## Context

`retrieval/vector_index.py::reindex_notes` calls `retire_absent` with the note ids on *this pod's*
disk. `note_index` is shared by every pod; the knowledge checkout under it is an `emptyDir` that
each pod's own `knowledge-sync` sidecar refreshes on its own schedule. So "absent from my disk"
cannot distinguish a deleted note from a note this pod has not fetched yet, and the existing guards
refuse an *empty* scan rather than a *lagging* one.

**Driven, over two real clones of one corpus against one index** — the probe is
`tests/test_note_index_external.py::_corpus_at_two_revisions`:

```
A (3 notes): 3   index: [reaction-1, reaction-2, reaction-3]
B (lagging): 2   index: [reaction-1, reaction-2]
A again:     3   index: [reaction-1, reaction-2, reaction-3]
```

The `BACKLOG.md` row this closes called that "the single change gating `replicas > 1`". **The
measurement says there are two, and the second is larger.** Look at the counts rather than the
index contents: B's pass re-embedded **2** notes and A's next pass re-embedded **3** — the whole
corpus, every alternating pass, one endpoint call per note. `note_file_fingerprints` is
`mtime_ns:size`, and two clones of one commit carry different mtimes, so the incremental rebuild
`D-2026-08-02-embed-only-what-changed` exists to provide degenerates to a full one the moment two
pods share an index. That is a content-derived fingerprint and its own decision; it stays a
`BACKLOG.md` row rather than riding along here.

The first probe of this was itself wrong and is worth recording, because it is the shape rule 5
warns about: it built the two "pods" as two copied directories. Neither was a git work tree, so the
guard's input was `None` on both sides, the measurement showed no change, and the probe was
measuring nothing.

## Decision

**A prune states how current the corpus it is pruning against is, and a row built from a newer
corpus is left alone.**

- `chemclaw.kg.graph.corpus_revision(notes_dir)` returns `git rev-list --count HEAD`, or `None`.
- `NoteIndex.upsert` takes `corpus_revision`; `NoteIndex.retire_absent` takes `built_before`.
  Postgres stores it in `note_index.corpus_commit_count` (migration 099, additive and nullable).
- `None` on either side is **no constraint**: the prune behaves exactly as it did before. That is
  what every offline corpus, every tarball deploy and every row written before 099 relies on, and a
  guard that refused without evidence would make a fresh deployment unable to remove a note at all.

**A commit count rather than a timestamp or a commit id**, and both alternatives were built and
rejected by measurement:

- *A timestamp* (`git log -1 --format=%cI`) was the first implementation. `%cI` has second
  resolution, so the probe's two commits — made in the same second — compared **equal**, the
  lagging pod retired the newer note anyway, and the guard passed its own probe while doing
  nothing. It also mixes clocks: `updated_at` is the indexing pod's and a commit's timestamp is
  whatever machine wrote it, so the prune's safety would have been a function of clock skew.
- *A commit id* cannot be ordered by a pod that has not fetched the other side, which is precisely
  the pod the guard exists for.
- A **count of commits** is monotone under ancestry — a descendant reaches strictly more commits
  than its ancestor — is one number, and involves no clock. What it cannot order is two genuinely
  divergent branches with equal counts; that is not this deployment (every sidecar fetches one
  remote) and the failure there is the one the prune already had.

Two details of the SQL are load-bearing and both were found by driving it rather than reading it.
The `%(before)s::int` casts: without them Postgres cannot infer the parameter's type and *every*
prune fails to prepare, including one passing no revision. And the three `IS NULL`/`<=` arms are
spelled out rather than folded into `NOT (... AND ...)`: three-valued logic makes `NULL > 5`
unknown and `NOT unknown` unknown, so the compact form silently **protected** every pre-099 row
instead of pruning it.

## Consequences

`replicas > 1` on the background worker is no longer gated by the *retirement* half. It is still
gated by the re-embedding half, which is now a measured row rather than an unnamed consequence.

An operator running `chemclaw-reindex` from a checkout that lags the corpus no longer prunes the
shared index — which was live today, at `replicas: 1`, and is the half of this defect that did not
need a second pod at all.

## What keeps it true

- `tests/test_note_index_external.py::test_a_lagging_checkout_does_not_retire_a_note_it_has_not_fetched`
  — two real clones of one corpus, one index. Mutations: dropping `built_before` at the call site
  fails it; making `corpus_revision` return `None` unconditionally fails it.
- `tests/test_note_index_external.py::test_a_note_deleted_from_the_corpus_is_still_retired` — the
  guard may only decline where it cannot judge. Mutation: protecting every row with a stored
  revision fails it, which is what distinguishes this change from deleting the prune.
- `tests/test_note_index_external.py::test_the_postgres_predicate_protects_a_row_from_a_newer_corpus`
  — the same guard through the shipped SQL, including the NULL arm. Mutation: removing the
  predicate fails it.
- `tests/test_note_index_external.py::test_a_corpus_outside_a_work_tree_has_no_revision` — the
  `None` path every offline corpus takes.
- `tests/test_migrations_are_additive.py` — holds 099's shape.
