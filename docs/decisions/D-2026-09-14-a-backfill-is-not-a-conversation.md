# D-2026-09-14-a-backfill-is-not-a-conversation — batching the one path nobody is waiting on

**Status**: accepted

## Context

`BACKLOG.md` carried the cost: a note write is one commit and one push, measured over the ORD
backfill at 103 records per 3.1 minutes, so a real deployment's first sync is days. It stated the
remedy as a distinction rather than a patch — *"a backfill and an incremental sync still want
different write shapes (one commit per batch for the first, one per note for the second)"*.

`D-2026-09-13-the-lock-is-not-the-bound-the-commit-is` measured the curve and **declined batching**:
327.3 ms per note at one, **31.6 at ten**, **8.5 at fifty**, with the cluster advisory lock at 14.4 ms
(4.8%) — so the lock is not the lever and batching is. It declined on the *product*: "a queued note
is one a chemist cannot read yet, which is what `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`
bought by deleting the PR-gate."

**That argument is right and it is about the conversational path.** It does not reach
`cli/backfill_corpus`, an operator command over a directory of existing documents: nobody is mid-turn
waiting for one of them to appear, and the person running it is waiting for the whole run to finish.

## What was measured

Against a real bare remote on an empty corpus, through `record_note` and `GitNoteWriter` unchanged:

| write shape | 50 notes | per note | |
| --- | ---: | ---: | ---: |
| one commit per note (**before**) | 7.05 s | **140.9 ms** | |
| ten to a commit | 0.79 s | 15.8 ms | 8.9x |
| fifty to a commit (**shipped default**) | 0.24 s | **4.8 ms** | **29.4x** |

The local remote is the floor — the sibling ADR's 327.3 ms is the same shape at a 10,000-note corpus
against a real one, where the saving is larger, not smaller.

## Decision

`BatchingNoteWriter` is a `NoteWriter` that holds N writes and commits them as one.
`cli/backfill_corpus` is its only caller; `backfill_commit_batch_size` defaults to 50, where the
measured curve flattens.

**It adds no git code, and that is the design.** `GitNoteWriter` carries the fetch, the
fast-forward, the rebase of unpushed commits, the dead-writer residue recovery and the cluster lock.
A second implementation of any of that is a second chance to get it wrong on the one path every note
in the system takes. So the batch is *one ordinary `NoteWrite`* — N writes' files concatenated, one
message — handed to the writer it wraps. `record_note` stays the one write path, untouched.

**Files are concatenated and never deduplicated by path.** Two notes may name the same dependency,
and `NoteFile` carries `overwrite=False` for a dependency against `True` for a subject; applying
them in order is exactly the sequence the unbatched path applies, while deduplicating on the first
entry would let a do-not-clobber copy win over the subject note's own content.

**A pending note's reference is the empty string**, and that is the second reason this is not a
drop-in for the conversational path: the note is not on the branch until the batch flushes, so a
chemist could not read it and the model could not cite it.

## Consequences

- A 4,251-document backfill goes from ~10 minutes to ~20 seconds against a local remote, and from
  ~23 minutes to ~50 seconds at the sibling ADR's real-remote rate.
- One commit in the notes repository now touches up to 50 files. Lower the setting if that is
  awkward for whoever reads that repository's history.
- The caller must `flush()`. `backfill_corpus` does, and logs the final batch's commit.

## What keeps it true

- `tests/test_backfill_batching.py::test_a_batch_of_notes_lands_in_one_commit` — six notes at three
  to a commit are **two** commits *and* six files on disk. Driven: flushing every note reddens it,
  and dropping a batch's files reddens it.
- `::test_an_unflushed_batch_is_not_on_the_branch` — the property that makes this wrong for the
  conversational path, asserted rather than argued.
- `::test_flushing_nothing_commits_nothing` and `::test_a_batch_size_that_batches_nothing_is_refused`.
- `::test_the_shipped_backfill_batches_and_nothing_else_does` — exactly one module may construct
  one, so the declined decision cannot arrive by import.
