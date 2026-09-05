# D-2026-09-05-the-gate-is-deleted-not-dormant — knowledge writes directly, and the PR-gate's 2,232 lines go with it

**Status:** accepted · **Date:** 2026-09-05 · **Carries out**
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`, which decided the axis and said in as many
words that it did not claim the code had shipped. This is that code. · **Supersedes the mechanism of
D-005**; the decision D-005 encoded was already superseded by the ADR above.

## Context

The deciding ADR left one question it had not foreseen, and measuring for the implementation raised
it immediately: **every caller of `propose_note` writes knowledge.** All nine of them — the agent
tool, both memory-synthesis fan-outs, observation promotion, both report drafts, the corpus
backfill and the confirmed-answer path. So ungating knowledge does not shrink the gate's subject; it
removes it entirely, leaving `pr_gate.py`, `proposal.py`, `proposal_store.py`, `submission.py`,
`git_submitter.py`, `routes/proposals.py` and `cli/reconcile_proposals.py` — 2,232 lines — with no
caller at all.

That is a fork the ADR did not settle, and both readings are defensible: keep the mechanism dormant
for the skill-promotion subject that ADR *decided* but nobody has built, or delete it and rebuild
when the distiller exists. It was put to the owner rather than resolved here, because "ship 2,232
unreachable lines" is exactly what `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`
deleted 1,442 lines over, and "delete and rebuild later" throws away working, tested machinery
including the reviewer-history work merged hours earlier in #323.

## Decision

**Delete it.** The gate's modules, its review surface, its durable record and its metrics are gone.
`kg/record.py` is the one write path and `kg/git_writer.py` the one mechanism.

### What a write now is

`settings.notes_path` is `note_repo_dir / knowledge_dir` — the single location `load_notes` reads
and this writer writes. So a note is in the graph the moment its bytes land; the commit that follows
is durability and history, not publication. That property is what makes "global the moment it is
learned" true without a new store, and it is why the rest of this decision has the shape it does.

**The write order is load-bearing, and it replaces D-133.** A PR merged every file of a submission
in one commit, so no reader ever saw half of one. A direct write can be read mid-flight, and
`load_notes` runs against whatever is on disk at that instant. `_build_write` therefore writes
**dependencies, then the subject, then the retirements** — each cites the one before it — so a note
never appears in the graph before what it cites. The cost is stated rather than hidden: between the
subject's write and its retirements', a note and its replacement are both current. That is the
lesser of two windows; retiring first would leave `superseded-by` pointing at a note that does not
exist yet, which is precisely what `kg-validate` exists to prevent.

**The cache inversion.** `GitNoteWriter` calls `invalidate_cache()`, and the gate deliberately did
not. Both are right about their own design: the gate wrote to a branch inside `.git/` that no reader
scanned, so busting would have advertised a change that had not happened and paid an O(notes) rescan
for it. These bytes land in the tree readers *do* scan, so a surviving cache is a reader serving a
graph missing the note just recorded, for up to `graph_cache_ttl_seconds`.
`test_a_write_busts_a_readers_cache_because_it_does_touch_their_tree` is the same test that asserted
the opposite, twice, and its docstring keeps both earlier readings.

### A regression the suite caught, and the fix

The gate committed inside a linked worktree with **its own index**, which is why residue staged in
the shared checkout structurally could not reach a note's commit. There is no second index now, so a
plain `git commit` swept whatever else was staged into a commit named after the note.
`_write_and_commit` passes `-- <written paths>`, and the idempotence check is scoped the same way
(`diff --cached --quiet HEAD -- <paths>`) for the same reason — a bare `--cached` reports anything
else staged and would turn a no-op into a commit. Found by
`test_poisoned_index_does_not_leak_into_the_next_write`, which existed to assert the worktree's
structural guarantee and failed the moment the structure changed.

### D-161's anti-feedback rule is restated, not repaired

D-161 says support counts distinct **merged** notes, and `load_notes` now returns agent-written
notes — so read literally, that sentence became the description of a self-confirming loop.

It is not one, and the reason is worth recording because it is not the reason the prose gave.
`mine_interactions` counts a cited id only `if c in project_of`, and `project_of` is built from the
reaction corpus alone; `mine_corpus` emits `reaction-<id>` exclusively. So support is reaction
records — deterministic transcriptions of experiments somebody ran (D-2026-08-25) — plus the
`interaction` note recording a chemist's own confirmation. **Neither is an agent assertion.** The
property doing the work was never the *merge*; it was the **kind** of thing counted, and the merge
was a second human step on top of a human act that had already happened. Migration `025`'s CHECK is
untouched and still forbids the one level below.

### The tool is renamed, because the prompt was making a false promise

`propose_knowledge_note` → `record_knowledge_note`, across 93 files. This is not tidying: the system
prompt told the model on **every turn** that notes "open a PR for human review", and
`chemclaw.cli.validate_prose_contract` exists because this repository has twice decided that prose
promising capability the agent lacks is a gate's job rather than a longer instruction (D-117). The
tool docstring now says what is true — recorded, readable at once, nobody will check it — and says
what follows from that: write only what the evidence carries.

## What this costs, stated plainly

A wrong machine-written claim is now **served until somebody contradicts it**. That is a real
change in failure mode and it is the accepted price of the axis: the alternative was a queue nobody
drains, measured on the one path where it was tried at 4.2–42 person-years per million entries. What
replaces prevention is D-160's provenance on every retrieved chunk, the citations a chemist checks
at the point of use, and `contradicts`/`supersede`/bi-temporal `valid_to`.

A second, smaller cost: a write that dies between two files leaves the first one on disk, uncommitted,
in the tree readers scan. Bounded by the write order — the survivor is always a dependency, never a
subject citing something absent — and asserted rather than glossed.

## Consequences

- Migrations 027/036/058 keep their files with `RETIRED` headers and their tables stay, because the
  schema is forward-only and a deployment that ran the gate holds real sign-offs by real people that
  `agent/leaver.py::_RETAINED` must still find on an erasure request. `durable/retention.py` drops
  its refusal: there is no live record left to protect.
- `chemclaw_notes_proposed_total` becomes `chemclaw_notes_recorded_total`, and
  `chemclaw_note_proposals_total` (the by-state series) is gone with the states.
- `_is_reviewer` stays: `routes/jobs.py` and `routes/protocols.py` use it, and it is the role an
  admin will hold when a skill is proposed — the seam grows a subject again rather than losing its
  only one.
- The 11 eval probe files that graded the agent on saying "pending human review" now grade it on
  saying what is true: recorded, machine-written, weigh it as such.
- **What is still owed** is what the deciding ADR already listed and this one does not touch: the
  local skills tier and the distiller that would write into it. Both wait on
  `D-2026-09-05-a-census-that-counts-only-success-is-blind-to-half-the-signal`'s trigger.
