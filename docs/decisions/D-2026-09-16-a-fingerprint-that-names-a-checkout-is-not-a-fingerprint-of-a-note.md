# D-2026-09-16-a-fingerprint-that-names-a-checkout-is-not-a-fingerprint-of-a-note — the note reindex diffs a content hash, and two pods at one commit stop re-embedding each other's work

## Status

Accepted. Supersedes the stat-only half of `D-2026-08-02-embed-only-what-changed`; that ADR's
decision — embed only what changed — stands, and only the signal it changed against moves.

## Context

`chemclaw.kg.graph.note_file_fingerprints` returned `note id -> "mtime_ns:size"`, and
`retrieval/vector_index.py::_needs_embedding` re-embeds a note whose current fingerprint differs
from the one stored in `note_index.fingerprint`. An mtime is a property of a **checkout**: a clone
sets a file's mtime when it writes the file. `note_index` is one shared table while the knowledge
tree under it is an `emptyDir` each pod's own sidecar clones, so two pods holding the *identical
commit* produce two entirely disjoint fingerprint dicts.

**Driven, over two real clones of one commit against one index** — the probe is now
`tests/test_note_index_external.py::_corpus_cloned_twice`, built beside
`_corpus_at_two_revisions` rather than reusing it, because the defect needs the pods to *agree*
about the corpus:

```
pod A: {'reaction-a': '1789549087743012992:71', ...}
pod B: {'reaction-a': '1789549087756318697:71', ...}
agree: False            # not one value in common; the keys always matched, which was the trap

embeddings per pass    A: 3   B: 3   A: 3   B: 3   A: 3
```

Over the corpus this repository ships, the same shape: **40 embeddings on every pass, for ever.**
The incremental rebuild `D-2026-08-02-embed-only-what-changed` exists to provide is incremental for
exactly one pod and for no more than one.

**The note count is 40, and neither figure in circulation was it.** `knowledge/` holds 41 `*.md`
files; `knowledge/README.md` has no frontmatter, so `read_note` correctly says it is not a note and
`load_notes` returns 40. 41 is the file count and 39 was stale. `note_file_fingerprints` keys all 41
— deliberately, it is what keeps an unparseable note from being retired — but `changed` is built
from the parsed list, so the endpoint calls are 40.

One thing this measurement corrected in what was already written down: the `BACKLOG.md` row said
"on every **alternating** pass", which is true and reads as though a pod running twice in a row is
spared. It is spared once and never again — pass 5 above is pod A immediately after pod B and still
re-embeds 3, because pod B's pass has meanwhile overwritten every stored fingerprint with its own.
The steady state of two pods is that *every* pass is a full rebuild.

**A second correction went the other way, and it is recorded because it was nearly published.**
This ADR was drafted claiming the row's `kg/graph.py:230` anchor was stale, on the reasoning that
`note_file_fingerprints` began at 210. Checked against the commit rather than reasoned about, line
230 was the `setdefault` call — the defect itself, named exactly. The anchor was *better* than a
`def` line, and a paragraph about stale references had to be deleted from a document whose whole
subject is checking a claim instead of finding it plausible.

A second, quieter consequence of the same defect, found while reading the upsert: a lagging pod's
pass re-embedded every note it *could* see, and `corpus_commit_count = EXCLUDED.corpus_commit_count`
means each of those rows had its stored revision rewritten **downwards** to the lagging pod's. The
D-2026-09-14 guard protects a row whose revision is newer than the pruner's, so the old fingerprint
was quietly eroding the guard that had just been built on top of it. No harm was driven from this —
constructing one needs three revisions and it is not claimed here — but a pod that re-embeds nothing
also rewrites nothing, so it is gone either way.

## Decision

**A note's fingerprint is a digest of the note's bytes.** `note_file_fingerprints` returns
`note id -> "sha256:<hex>"`, computed with one `read_bytes` per file.

- **`_dir_fingerprint` keeps the stat, and that is not an inconsistency.** Its cache
  (`_NOTES_CACHE`, `_GRAPH_CACHE`) is *per process*, so one checkout's mtimes are all it ever
  compares — there is no second party for it to disagree with. It is also paid on interactive query
  latency (DA-5, the floor on a warm read) rather than once an hour. The two functions differ
  because their consumers differ, which is the whole finding stated the other way round.
- **The digest carries its algorithm as a prefix.** No `mtime_ns:size` string can equal a `sha256:`
  one, so every row written before this reads as changed exactly once: the corpus is re-embedded one
  final time on upgrade, the same one-time cost migration 035 paid to introduce the column. **No
  migration is needed** — `note_index.fingerprint` is `TEXT` and only the value's shape moves.
- **A file that will not open keeps an entry**, and this is the one place the change is not a
  straight substitution. `scan_notes_dir` drops a file whose *stat* fails, correctly — that file is
  gone. Hashing opens a second, wider window: a permission change or an I/O error leaves a file that
  is very much still there, and letting it drop would have put it outside `reindex_notes`'s `keep`
  set and retired its index row — the same 40-rows-per-40-broken-notes failure that union was built
  to prevent, reintroduced through a different door. `UNREADABLE` is a constant, so it keeps the
  note in `keep` without costing an embedding per pass while the fault lasts. This was found
  reviewing the diff rather than by a failing test, which is why it has one now — and the test
  then corrected the sentence that introduced it: the docstring claimed the note is "re-embedded
  exactly once" when the file opens again, and driving it showed a fault that heals to the same
  bytes costs **nothing**, because the stored digest was never wrong. A stat-based signal could not
  have reached that answer, since the repair moves the mtime. Writing a behaviour down and then
  measuring it disagreed twice in one commit, which is the argument for measuring it.

**The document crawl keeps `mtime_ns:size` and is not an oversight.**
`ingest/documents/crawl.py` fingerprints the same way, and the reason it is right there is the
reason it was wrong here: the share is a PersistentVolumeClaim **mounted** by every pod
(`D-2026-08-06-a-share-is-mounted-not-called`), so two pods reading it read one filesystem and see
one mtime. A checkout is copied per pod; a mount is not. That is an argument rather than a
measurement — there is no share to mount in this lane — and it is written down so the next reader
does not "fix" it by symmetry.

**Merged migration comments are not edited.** `035`, `039` and `099` describe what each did on the
day it ran, and `ALTER TABLE ... ADD COLUMN fingerprint TEXT` is unchanged. Editing them would be
the `git log`-versus-live-state confusion this repository already refuses for ADRs.

### What it costs

Hashing is not free and the trade is stated rather than implied. Median of 200 scans each, one run,
warm page cache, both arms going through the same `scan_notes_dir` so the comparison is the read and
nothing else:

| corpus | stat only | hashed | added |
| --- | --- | --- | --- |
| the shipped `knowledge/` (41 files, 34 kB) | 0.468 ms | 0.810 ms | **+0.34 ms** (1.7x) |
| 1,000 notes × 2 kB | 8.098 ms | 16.975 ms | +8.88 ms (2.1x) |
| 5,000 notes × 2 kB | 45.31 ms | 94.44 ms | +49.1 ms (2.1x) |
| 20,000 notes × 2 kB (40 MB) | 219.7 ms | 427.6 ms | +207.9 ms (1.9x) |
| 1,000 notes × 32 kB (32 MB) | 8.412 ms | 46.63 ms | +38.2 ms (5.5x) |

The last row is the one worth reading: the added cost tracks **bytes**, not files — 1,000 large
notes cost more to hash than 5,000 small ones — so the term that matters is corpus size on disk, and
at these sizes it is bounded by page-cache bandwidth rather than by syscalls.

Against that: **40 embedding calls removed from every pass** on the shipped corpus, at an hourly
Schedule, for ever. D-2026-08-02 declined this cost when the alternative was an embedding call; the
trade has not changed sign, it was measured on one side only. `reindex_notes` already offloads the
scan with `asyncio.to_thread`, and the scan runs once per pass on a job that is about to embed.

## Consequences

**`workers.background.replicas` stays 1, and the reason is now different in kind.** Every reason
previously written down is closed: the D-069 checkout lock became a Postgres advisory lock, the
reindex's retirement half became revision-bounded (D-2026-09-14), and its re-embedding half is this
ADR. What remains is not a known race — it is that **nobody has ever run two workers on this queue.**
`values.yaml` argues the rest of `background-jobs` is indifferent to the worker count (Schedules
under `SKIP`, one ELN cursor writer, retention re-checking inside its own `DELETE`, the outbox on
`FOR UPDATE SKIP LOCKED`); each is plausible and none has been observed with two workers polling.
That is prose, and this repository's own rule is that prose is evidence about its author. So it stays
pinned, `BACKLOG.md` carries what to drive, and the distinction between "known unsafe" and
"unmeasured" is written into the values file where the next reader will be deciding.

**`strategy: Recreate` does not relax.** It had two justifications stated together for long enough
to read as one. The corpus race is gone; the replay guarantee — one code version resumes every
unfinished history (`D-2026-09-09-a-replay-control-needs-an-archived-history-not-a-patch`) — never
depended on the replica count and still holds.

**Three chart comments and one test docstring were stale before this commit touched them**, all
naming a reason a later commit had closed. The PDB template had named three different reasons for
the same pin in succession, each one closed by a commit that did not think to come and edit a
PodDisruptionBudget. It now points at `values.yaml` instead of restating it —
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` applied to a justification rather than to
a number.

An operator gains one thing beyond the pods: an edit that moves neither mtime nor size is now
visible. `git checkout` onto a revision whose file is the same length, with timestamps restored from
an archive, produced an identical `mtime_ns:size` for different bytes — a **stale skip**, where the
index serves the old text for ever. That is the failure direction worse than re-embedding too much,
and nothing had ever asserted against it.

## What keeps it true

- `tests/test_note_index_external.py::test_two_clones_of_one_commit_fingerprint_every_note_identically`
  — the root cause at its smallest, as full dict equality rather than key equality, since the keys
  always agreed. Mutation: restoring `mtime_ns:size` fails it.
- `tests/test_note_index_external.py::test_two_pods_sharing_one_index_re_embed_nothing_on_an_unchanged_corpus`
  — the cost, as the count that pays for it, over five alternating passes. Mutation: restoring
  `mtime_ns:size` fails it at pass 2 with 3 where 0 is required.
- `tests/test_note_index_external.py::test_a_second_pod_still_embeds_a_note_the_first_has_not_seen`
  — the change may only stop work that was redundant, which is what separates it from deleting the
  diff. Mutation: restoring `mtime_ns:size` fails it, at 4 where 2 is required.
- `tests/test_note_index_external.py::test_the_fingerprint_survives_the_round_trip_through_postgres`
  — the shipped backend, because a 71-character value in a column that held ~25 is the thing an
  offline test cannot see truncated. It **skips** without a database, so a run that skipped it is
  not evidence about the column.
- `tests/test_note_index_external.py::test_a_note_that_will_not_open_is_kept_rather_than_retired`
  — the `keep` set end to end, through `reindex_notes` rather than through the dict, including the
  two heal arms that corrected this ADR's own first account of them. Mutation: dropping the
  `UNREADABLE` marker fails it.
- `tests/test_graph.py::test_note_file_fingerprints_sees_an_edit_that_moves_neither_mtime_nor_size`
  — the stale skip, with the stat pair asserted *equal* in the same breath, so the probe cannot
  quietly stop probing what it was written for.
- `tests/test_graph.py::test_a_note_that_will_not_open_keeps_its_entry_rather_than_vanishing` —
  the widened window, staged as a directory wearing a note's name rather than as `chmod 0o000`,
  which skips on a root runner and drove nothing. Mutation: dropping the marker fails it.
- `tests/test_graph.py::test_note_file_fingerprints_agrees_with_the_parse_on_a_duplicate` — the
  first-in-path-order rule D-2026-08-16 established, now discriminating on bytes rather than on a
  `sleep` that widened two mtimes.
- `tests/test_deploy_chart.py::test_the_singleton_worker_is_a_singleton_across_a_rollout_too` —
  `Recreate`, re-argued for replay alone.
