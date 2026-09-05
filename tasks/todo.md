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

- [ ] 1. `kg/record.py`: `record_note(note, writer, ...)` replaces `pr_gate.propose_note`. Renders
      the subject note, its dependencies and its retirements; writes them; returns the reference.
      **Dependencies are written before the subject**, so a note never appears in the graph before
      what it cites — the invariant that replaces "one PR is one reviewable unit" (D-133).
- [ ] 2. `kg/submission.py` → the write vocabulary: `NoteFile` kept, `NoteSubmission` → `NoteWrite`
      (no branch/title/body), `NoteSubmitter` → `NoteWriter`. The injection seam stays; tests need it.
- [ ] 3. `kg/git_submitter.py` → `GitNoteWriter`: keep the repo guard, the lock and the error
      classification; drop the per-note branch, the worktree and the force-push. Commit on the
      checkout's own branch and push.
- [ ] 4. The 9 call sites: `graph_tools` (x2), `memory_jobs` (x2), `observation_jobs`,
      `report_workflow` (x2), `backfill_corpus`, `memory/interaction`.

## The deletions

- [ ] 5. `kg/proposal.py`, `kg/proposal_store.py`, `api/routes/proposals.py`,
      `cli/reconcile_proposals.py`, the `Proposal*` schemas, `_visible_proposal`/`VisibleProposal`,
      the knowledge-merged webhook. **`_is_reviewer` stays** — `routes/jobs.py` uses it too.
- [ ] 6. Migrations 027/036/058 keep their files with `RETIRED` headers and the tables stay empty,
      the forward-only rule `D-2026-08-14` already paid for with `audit_anchors`.
      `durable/retention.py`'s `note_proposals` refusal goes with the reason it stated.
- [ ] 7. Metrics whose subject is gone (`chemclaw_notes_proposed_total` and the proposal-state
      series), the `proposal_*` settings, and `make proposals-reconcile`.

## The one real correctness question

- [ ] 8. **D-161's support count was "distinct *merged* notes" and there is no merge any more.**
      `mine_interactions` counts `interaction` notes as support; ungated, those are agent-written
      with no human step, which is the self-confirming loop migration `025`'s CHECK exists to stop,
      one level up. Decide and state it: either support counts only human-authored notes, or the
      thresholds mean something new and `observations.py` says so. **Not a detail to discover
      while editing** — it is the reason D-161 wrote a CHECK rather than a convention.

## Then

- [ ] 9. Tests: delete `test_note_proposals*.py` and `test_pr_gate.py`; rewrite the gate assertions
      in the other ~14; add the direct-write tests including the dependency-ordering invariant.
- [ ] 10. Probes: 11 files grade gate behaviour. Regrade against what the system now does.
- [ ] 11. Prose: `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md`, package READMEs, the skills that
      teach the gate, the system prompt and `make prose-validate`.
- [ ] 12. ADR + ledger + BACKLOG; `make lint type test` green with Postgres up.

## Review

(filled at the end)
