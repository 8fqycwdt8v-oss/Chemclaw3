# Task — ungate knowledge, and delete the PR-gate

Carries `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`, which decided the axis and
explicitly did not claim the code shipped. Owner's call this session, put as a question because the
ADR did not foresee it: **every `propose_note` caller is knowledge**, so ungating leaves the gate
with no subject at all. Chosen: ungate everything and delete the gate rather than leave ~2,232
lines unreachable (`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`).

## Blast radius, measured

- **15 src files** import the gate; **~17 test files** exercise it.
- 348 files mention it in prose; **11 of 11 eval probe files** grade the agent on gate behaviour.

## The write path

`settings.notes_path` is `note_repo_dir / knowledge_dir` — one location for read and write, so a
file written there is readable by `load_notes` *immediately*. That is what makes "global the moment
it is learned" true without a new store.

- [x] 1. `kg/record.py`: `record_note(note, writer, ...)` replaces `pr_gate.propose_note`. Renders
      the subject note, its dependencies and its retirements; writes them; returns the reference.
      **Dependencies are written before the subject**, so a note never appears in the graph before
      what it cites — the invariant that replaces "one PR is one reviewable unit" (D-133).
- [x] 2. `kg/submission.py` → the write vocabulary: `NoteFile` kept, `NoteSubmission` → `NoteWrite`
      (no branch/title/body), `NoteSubmitter` → `NoteWriter`. The injection seam stays; tests need it.
- [x] 3. `kg/git_submitter.py` → `GitNoteWriter`: keep the repo guard, the lock and the error
      classification; drop the per-note branch, the worktree and the force-push. Commit on the
      checkout's own branch and push.
- [x] 4. The 9 call sites: `graph_tools` (x2), `memory_jobs` (x2), `observation_jobs`,
      `report_workflow` (x2), `backfill_corpus`, `memory/interaction`.

## The deletions

- [x] 5. `kg/proposal.py`, `kg/proposal_store.py`, `api/routes/proposals.py`,
      `cli/reconcile_proposals.py`, the `Proposal*` schemas, `_visible_proposal`/`VisibleProposal`,
      the knowledge-merged webhook. **`_is_reviewer` stays** — `routes/jobs.py` uses it too.
- [x] 6. Migrations 027/036/058 keep their files with `RETIRED` headers and the tables stay empty,
      the forward-only rule `D-2026-08-14` already paid for with `audit_anchors`.
      `durable/retention.py`'s `note_proposals` refusal goes with the reason it stated.
- [x] 7. Metrics whose subject is gone (`chemclaw_notes_proposed_total` and the proposal-state
      series), the `proposal_*` settings, and `make proposals-reconcile`.

## The one real correctness question

- [x] 8. **D-161's support count was "distinct *merged* notes" and there is no merge any more.**
      `mine_interactions` counts `interaction` notes as support; ungated, those are agent-written
      with no human step, which is the self-confirming loop migration `025`'s CHECK exists to stop,
      one level up. Decide and state it: either support counts only human-authored notes, or the
      thresholds mean something new and `observations.py` says so. **Not a detail to discover
      while editing** — it is the reason D-161 wrote a CHECK rather than a convention.

## Then

- [x] 9. Tests: delete `test_note_proposals*.py` and `test_pr_gate.py`; rewrite the gate assertions
      in the other ~14; add the direct-write tests including the dependency-ordering invariant.
- [x] 10. Probes: 11 files grade gate behaviour. Regrade against what the system now does.
- [x] 11. Prose: `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md`, package READMEs, the skills that
      teach the gate, the system prompt and `make prose-validate`.
- [x] 12. ADR + ledger + BACKLOG; `make lint type test` green with Postgres up.

## Review

**The fork the deciding ADR had not foreseen, and how it was resolved.** All nine `propose_note`
callers write knowledge, so ungating did not shrink the gate's subject — it removed it, leaving
2,232 lines with no caller. That is not a call to make while editing: shipping them dormant is what
`D-2026-08-15` deleted 1,442 lines over, and deleting throws away tested machinery including #323's
reviewer history from hours earlier. Put to the owner; answer was delete.

**Three things the change turned over, each argued rather than assumed:**

1. **Write order replaces D-133.** A PR merged every file at once; a direct write can be read
   mid-flight. Dependencies → subject → retirements, each citing the one before it, with the
   accepted window (a note and its replacement both current) stated against the rejected one (a
   dangling `superseded-by`).
2. **The cache is now busted where the gate deliberately did not bust it.** Both correct for their
   own design — the gate wrote where no reader scanned. The test that asserted "leave the cache
   alone" now asserts the opposite and keeps both earlier readings in its docstring.
3. **A regression the suite caught.** The gate's linked worktree had its own index, so staged
   residue structurally could not reach a note's commit. One shared index removed that guarantee
   and a plain `git commit` swept the stray in. Fixed in the *code* (path-limited commit and
   path-limited idempotence check), not in the test.

**D-161's anti-feedback rule was restated, not repaired.** `load_notes` now returns agent-written
notes, so "support counts *merged* notes" would have become the description of a self-confirming
loop. It is not one: `project_of` admits only reaction records, so support is real experiments plus
the chemist's own confirmation. The property doing the work was never the merge — it was the kind of
thing counted, and the merge was a second human step on top of a human act that had already
happened.

**What was found only because a validator exists.** `make prose-validate` caught seven stale
references including one in a *merged* ADR, which must never be edited — the sanctioned remedy is
`_RETIRED_METRIC_NAMES`, empty until now, and this is its first entry. `test_docstring_paths`
caught 22 files with dangling module pointers. Neither would have been visible by reading.

**Cost accepted and written down rather than smoothed over**: a wrong machine-written claim is now
served until contradicted, and a write that dies between two files leaves the first on disk in the
tree readers scan (bounded by the write order — the survivor is always a dependency).

**Verification.** `make lint`, `make type` (448 files), `make prose-validate`, `make skill-validate`
green. Full `make test` reported in the commit, with Docker and Postgres up so the DB-backed tests
run rather than skip — the first run of this change reported 475 skips against a normal 63, which
was Postgres being down and is exactly the trap `CLAUDE.md` warns about.
