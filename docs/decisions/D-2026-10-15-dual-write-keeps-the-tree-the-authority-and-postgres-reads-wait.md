# D-2026-10-15-dual-write-keeps-the-tree-the-authority-and-postgres-reads-wait — Under `kg_store=dual` a note is written to the tree first and Postgres second, a Postgres failure is loud and never the chemist's, and `kg_store=postgres` is refused until the reads flip

**Status:** accepted · **Date:** 2026-10-15

Takes the order `D-2026-10-14-the-notes-live-in-postgres-and-git-leaves-the-knowledge-path` set
(importer and dual-write, then the read flip after seven clean comparison days) and fixes the three
things it left open: what a Postgres failure does to a write, who repairs a divergence, and what
stops a deployment from reading Postgres before the readers can.

## Context

`kg_store=git` writes the notes tree and reads it. `PostgresNoteStore.put` exists and nothing calls
it. To fill Postgres from the tree, keep it current while chemists keep writing, and know when the
two agree well enough to read from it, a write has to reach both and a comparison has to say
whether they match. The two stores do not share a transaction: the tree's commit is pushed to a
remote and cannot be rolled back by a database. Whatever is chosen is therefore a choice about which
failure the chemist is told about and which is left for an operator.

Measured on the committed corpus: the 41 shipped notes import into Postgres and compare with zero
differences. Only 29 of them have the same file bytes as their canonical render (the rest are laid
out by hand, `D-2026-10-14-...`), which is why the comparison is on canonical notes and edges and
never on a digest.

## Options

**What a Postgres failure does to a write**

1. **Both must succeed.** The call fails if either store fails. The tree has already committed and
   pushed by then, so the chemist is told a write failed that is in the corpus, a retry re-commits a
   no-op, and the availability of a write becomes the product of two stores'.
2. **Postgres first, then the tree.** Postgres can be rolled back, the tree cannot, so the tree goes
   last and the failure of the unproven store gates the authoritative one. Every Postgres fault
   then stops chemists writing during the very window the store is being proven, and a tree failure
   after a Postgres success leaves Postgres ahead of the authority, which nothing can repair
   without deleting a note.
3. **The tree first, Postgres best effort, loud.** The tree's failure is the call's failure. A
   Postgres failure is logged at ERROR with the note id, counted on
   `chemclaw_kg_dual_write_failed_total` (an alert and a dashboard panel read it), and left for the
   comparison. Postgres is never ahead of the tree, so the repair has one direction.
4. **An outbox.** The tree write records an intent a worker applies to Postgres until it succeeds.
   It repairs by itself, which is what makes it wrong here: a transition that exists to count clean
   days must see its divergences, and the outbox is a table, a worker and a retention rule for a
   mode that is removed with the tree.

**Who repairs a divergence**

1. **The scheduled comparison repairs.** Every night ends clean, so no night is a finding.
2. **The scheduled comparison reports; an operator repairs** (`make kg-validate-compare
   ARGS=--repair`). A repair copies the tree's version of a differing note into Postgres, leaves
   what it cannot decide (a note only Postgres holds) and says why, and is a `put`, so repeating it
   changes nothing. Recording the same note again is a repair too.

**What stops a Postgres-only read before it can work**

1. **A flag, `kg_postgres_reads_accepted`.** One more setting for an operator to set by hand while
   several readers still take a notes directory (`conflict_index`, the retrievers' `notes_dir`, the
   `exists()` guards) and would answer from an empty tree.
2. **The settings refuse `kg_store='postgres'`** with the reason, and the pull request that moves
   the reads removes the check. The write arm and the read arm exist and are tested; no deployment
   can reach them early.
3. **No `postgres` value until the flip.** Nothing to misuse, and the arms are written and reviewed
   at the flip with no prior test of them.

## Decision

**The tree first, Postgres best effort and loud (3); the scheduled comparison reports and an
operator repairs (2); the settings refuse `postgres` (2).**

Why the failed-write order cannot be the inverse: `record_note` awaits the tree's writer and only
then calls `put`, so Postgres is written only after the tree has the note. A tree failure raises
before Postgres is asked; a cancelled call between the two leaves the tree ahead, which the
comparison finds. The one exception is a batching writer (`backfill_corpus`), which returns before
its commit, so a backfill under a non-git store writes each note on its own.

Same-id writers on two pods can reach Postgres in the other order from the tree (tree A, tree B,
Postgres B, Postgres A). That window is one `put` wide and needs two writers of one id; it is not
locked, because a lock spanning the tree and the database would hold a pooled connection for every
write for a mode that exists to be removed. The comparison finds it and the repair ends it, and a
test drives it.

Differences are reported only if they persist across `kg_compare_settle_seconds` (default 330, above
the 300 s clone sync), so a note another pod has written, whose clone has not synced, is not a
finding. A retirement's added sentence is kept by Postgres when it extends the stored body (the
file writer keeps it); a copy that does not extend it was read before someone's edit and only its
window and `superseded-by` edge are applied.

## Consequences

- With `kg_store=git`, behaviour is unchanged and Postgres is never touched.
- A note a person edits in the repository is not written through `record_note`, so it reaches
  Postgres only by `make kg-import` or `--repair`; until then it is a difference, and the window
  of clean days needs one of them after each such edit.
- A deployment on `dual` gets a `kg-compare` Schedule (`KgCompareWorkflow`), two alerts
  (`ChemclawKnowledgeDualWriteFailing`, `ChemclawKnowledgeStoresDiffer`) and a dashboard panel. The
  seven clean days before the flip are days with neither alert.
- The compare reads both corpora and both graphs whole, so its memory is the corpus twice. That is
  not measured at 20,000 notes (`cli/kg_store_bench` is where to measure it); a keyset-paged pass
  is the change if it matters.
- `kg_store=postgres` cannot be set until the flip's pull request removes the check; the check is
  one validator in `core/config/kg.py`.

Revisit when: `chemclaw_kg_dual_write_failed_total` moves on more than one day of a seven-day window
with no database fault to explain it (the failures are then a defect of `put`, not of the
environment, and an outbox is worth its table), or the comparison's own run time exceeds
`kg_compare_timeout_seconds` on the production corpus.

## What keeps it true

- `tests/test_kg_dual_write.py`: both stores equal after writes of every shape (dependencies,
  retirements, a refutation, a person's note, a repeat); a Postgres failure leaves the commit, bumps
  the counter, logs at ERROR, is found by the comparison and fixed by the repair; a tree failure
  leaves Postgres untouched; two writers of one note end repaired; `git` mode never touches
  Postgres; the `postgres` arm writes Postgres only; the settings accept `dual` and refuse
  `postgres`.
- `tests/test_kg_compare.py`: a negative control for each difference class, the hand-laid file that
  is not a difference, the settle pause, repair and what it refuses, the command's exit codes, and
  the committed `knowledge/` corpus compared clean after import.
- `tests/test_kg_import.py`: id order, idempotence, arrival dates from a dated git history, a
  person's note as a subject, every skipped file named, the dry run, one note in memory at a time.
- `tests/test_kg_compare_workflow.py`, `tests/test_schedules.py`, `tests/test_deploy_chart.py`: the
  activity, the workflow under Temporal's sandbox, the Schedule that exists only under `dual`, and a
  consumer for each new metric.
- `tests/test_note_store.py` runs on `put` for both stores.
