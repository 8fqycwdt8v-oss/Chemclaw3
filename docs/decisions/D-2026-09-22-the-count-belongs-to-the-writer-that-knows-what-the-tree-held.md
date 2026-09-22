# D-2026-09-22-the-count-belongs-to-the-writer-that-knows-what-the-tree-held — a batched note count

**Status:** accepted · **Date:** 2026-09-22 · **Supersedes** the counting rule in
[`D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes`](D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes.md),
whose `notes: int` field, `written` derivation and unit-not-commits decision all stand — only its
`len(batch)` clause is replaced. Closes the `BACKLOG.md` row *"A batched flush books the whole batch as
notes recorded when any one of them committed"*.

## Context

`D-2026-09-14` fixed a 50x **under**count: fifty notes in one commit moved
`chemclaw_notes_recorded_total` by 1.0, because `record_note` incremented on a boolean. It replaced the
flag with a count and wrote the rule into its Decision section: *"0 for the idempotent no-op **and** for
a batch that has not flushed, `len(batch)` on a flush that committed."*

`len(batch)` is right at both ends of the range and wrong in the middle. Measured at HEAD against a real
bare remote, at the shipped `backfill_commit_batch_size` of 50: 49 notes byte-identical to what the tree
already held plus one new one committed **once**, touched **one** file, and moved the counter by **50**.
Through the real CLI at a batch of 4: three identical, one new, `git diff-tree --name-only` reports one
path, the counter moves by 4.

So it is the same magnitude of error as the one that ADR fixed, in the other direction — and it fails
that ADR's *own* guard sentence, which is the part worth quoting because it already forbids this:

> **The batch reports 0 when the inner write was a no-op.** Re-running a backfill over documents
> already in the corpus is the frequent legitimate case; a `len(batch)` that ignored it would turn
> "notes recorded" into "notes offered", which is the attempt-counting the metric was declared to avoid.

`len(batch)` does not ignore the *all*-no-op case. It ignores the partial one, which is the ordinary
shape of a re-run backfill that picked up one new document.

**Neither of the two tests that name this counter could see it.** One writes seven brand-new documents
and asserts 7; the other re-runs over an unchanged corpus and asserts 0. `len(batch)` is correct at both.
That is the same coverage shape `D-2026-09-14` itself called out about its predecessor — "each half was
covered alone" — reproduced one layer in.

**The row's premise, and why it is wrong.** The row recorded this as not-a-defect because *"the honest
number is not available at that layer and making it available is a contract change"*: the inner writer
"knows only 'something was committed'", and a changed-file count "counts *dependency* notes and
retirement rewrites too, which is a third meaning of the field". Measured, both halves are false:

- `_write_and_commit` already builds `prior` — the pre-write bytes of **every** planned target, for the
  rollback — one line before it asks git the same question as a boolean. The per-file answer is
  `prior[path] != file.content.encode("utf-8")`.
- Dependencies and retirements are separable by flags, not by inspection. `record._build_write` tags a
  dependency `overwrite=False` and a retirement `amendment=True`, and emits exactly **one** subject per
  `NoteWrite`. Driven over a write carrying one subject, one dependency and one retirement: git's
  changed-file count says three, the flags say one subject, and the honest answer is the subject.

## Decision

**The count is computed where the facts are, and `BatchingNoteWriter.flush` becomes a pass-through.**

- `_changed_subjects(planned, prior)` returns the **distinct** repo-relative paths of the subject notes
  whose bytes this write changes — `overwrite and not amendment`, compared against what the target held.
  Distinct, because two writes for one note id in a batch are two planned entries and one note.
- `_write_and_commit` passes that count to `_push`, which puts it on the `WriteOutcome`.
- `flush` returns the inner outcome unchanged. There is nothing left for a batch to know that its writer
  does not, so `len(batch)` disappears rather than being corrected.

`WriteOutcome` gains no field: `notes` already means "notes that reached the graph", and this makes it
true of a mixed batch. Every `written` reader is untouched.

## Consequences

**Two paths keep a weaker count, and both exist to keep `written` a fact about the commit.**

The first is the one `_push` exists for. Reaching the push with nothing committed *now* means an
earlier attempt's commit is still unpushed and this call is what lands it — the stranded-note failure
that method's docstring records, where reporting nothing told the caller the write had failed while the
note sat on one pod's disk. Those bytes are this write's own, rewritten byte-identically, so
`_changed_subjects` is empty there, and `notes` falls back to the *planned* subject count.

The second was found by review, after the first version of this change shipped without it. A write
whose **subject** is byte-identical while a *dependency* or a *retirement* changed does commit, and
`_changed_subjects` counts only subjects — so it returned 0, and `written` went false on a write that
committed and pushed. Driven on real git: re-recording an identical note whose dependency file had gone
missing returned `notes=0 written=False` where the previous implementation returned `notes=1
written=True`. That breaks `WriteOutcome`'s own stated contract (`notes=0` means "nothing was
committed") and undercounts the metric this change exists to correct, in a case the old code got right.
So the committed path is floored at 1. The cost is one over-count in that narrow case, which is
precisely what shipped before, and the 50x batch overcount is untouched by it: 49 identical plus one
new still reports 1.

The residual is narrow and recorded rather than hidden: a batch whose every file is byte-identical **and**
whose earlier push failed reports the whole batch, of which some notes were already on the remote. It
needs both conditions at once, and the alternative — reporting 0 — is exactly the stranded-note failure
that path was written to close. `_subject_count` is the second helper for that reason, and its docstring
says which question it answers.

**`written` and "how many" are one field answering two questions, and that is now visible rather than
latent.** `D-2026-09-14` refused to store both, on the ground that "storing both would be the same fact
twice, which is how they would drift". That reasoning holds for the ordinary path and is what makes the
paragraph above necessary: the push-retry path is the one place where the two genuinely differ, and it is
handled by choosing the count rather than by adding a field.

**A drifted figure in the superseded ADR, corrected here rather than there** (a merged ADR is never
edited): it says "the four in `git_writer.py` say which case they are". Before this change there were
**six**, one of which relied on the `notes` default of 1 rather than saying so. After it there are
**five**, and every one passes `notes=` explicitly — the defaulted construction is the one this
change replaced. A review caught the first version of this paragraph quoting the *pre-change* count
inside the document that changes it, which is the same defect class as the figures it corrects.

**What holds it:** `tests/test_backfill_batching.py::test_a_batch_that_changed_some_of_its_notes_counts_only_those`
drives the partial batch through the real CLI against a real bare remote and checks the metric delta
against `git diff-tree --name-only`, because a count alone cannot distinguish "counted the right notes"
from "counted a number that happens to match". Driven against the previous implementation, it fails.
`test_a_dependency_and_a_retirement_in_a_batch_are_not_counted_as_notes` pins the separation the row
believed impossible.

**Unchanged:** the conversational path is one note per write and was already exact as a count of
*subjects* — which the floor above is what preserves. It is not, and has never been, a count of note *files* — a write carrying two dependency
notes puts three files in the graph and moves the counter by one. That is the right reading of "notes
recorded" and is now what the code computes rather than what it happened to return.
