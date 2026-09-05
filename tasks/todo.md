# Task — the reviewer sees what was already decided about this note

Follows the WikiSkill review (PR #323) and the owner's two questions: how does an admin avoid
drowning in a flood of near-identical proposals, and how do local and global stay convergent.

## The honest scoping

Most of the ideated mechanisms — promotion thresholds on skills, cluster review, supersession of
local variants, the divergence census — are downstream of a **distiller that does not exist** and
is blocked on an empty corpus (`make trajectory-census`: 0 sessions, neither arm greenlit). Building
them now is `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`, which this repository
deleted 1,442 lines over. They stay recorded, not built.

**One of them is real today**, because the review queue it concerns already exists and already
carries proposals: `GET /proposals`, `GET /proposals/{id}`, `POST /proposals/{id}/decision`.

**The gap.** A reviewer opening a proposal is shown the note's bytes and nothing about what has
already been decided about that note. `rejected_version(note_id, content_hash)` refuses a re-proposal
of *byte-identical* rejected content, so the exact-repeat case is closed — but a **changed**
re-proposal of a note a colleague already rejected arrives with the prior rejection and its stated
reason invisible. The reviewer re-derives the judgment, or merges what was already refused. That is
the largest single ablation in the reviewed framework (+15.0pp for a proposer that sees rejection
history), and this tree already *retains* the history — `note_proposals.reason`, and
`durable/retention.py` refuses to prune the table — while nothing reads it back.

## Plan

- [x] 1. `ProposalStore.history(note_id, scope)` on the protocol and both backends, oldest-first.
      Scope is the same actor rule the visibility gate uses, applied to *other people's* versions.
- [x] 2. `ProposalHistoryEntry` schema — the decision, never the content. `ProposalDetail` gains it.
- [x] 3. Wire it in `get_note_proposal` under the same reviewer/owner rule as visibility.
- [x] 4. Tests: oldest-first, the viewed version excluded, a non-reviewer sees only their own prior
      versions, empty on a first proposal, and that a *changed* re-proposal surfaces the rejection.
- [x] 5. ADR + ledger + BACKLOG; record the deferred mechanisms with their trigger.
- [x] 6. `make lint type test` green, Postgres up so the DB-backed tests actually run.

## No migration

`note_proposals_note_idx ON note_proposals (note_id, submitted_at DESC)` (migration 027) is already
exactly the index this query sorts by — checked rather than assumed
(`D-2026-08-27-an-index-must-match-the-sort-it-serves`).

## Review

**What shipped.** `ProposalStore.history(note_id, actor)` on the protocol and both backends, returned
by `GET /proposals/{id}` as `history`. 6 new tests in `test_note_proposals.py` and 1 in
`test_note_proposals_postgres.py` — the second because the ordering across separate transactions and
the actor predicate are things only the database decides, and that predicate fails *open*.

**Two things were reused rather than invented**, both after checking:
`ProposalSummary` already carries exactly what a history entry is and carries no body, so a
`ProposalHistoryEntry` would have been the same fields under a second name; and migration 027 already
indexes `(note_id, submitted_at DESC)`, so there is no migration.

**One surprise worth keeping.** The first draft of the HTTP test sent
`{"state": "rejected", ...}` to the decision route, which takes `{"approved": false, ...}` — the
route returned 422, the rejection never happened, and the assertion then failed against a
`superseded` row instead. That is the test catching a wrong assumption about a shape rather than a
bug, which is the right way round, but it is a reminder that a route's payload is worth reading
rather than guessing.

**The scoping decision is the one to re-check if this is ever extended.** History follows
`_is_reviewer`, exactly as the listing does. A future "show me every note like this one" would cross
that boundary by construction and needs the rule restated, not inherited.

**Verification.** `make lint`, `make type` green. `test_note_proposals` 42 passed,
`test_note_proposals_postgres` 8 passed (ran rather than skipped — Docker up, Postgres migrated),
`test_decision_log` / `test_repo_map` / `test_deferred_register` green. Full `make test` reported in
the commit.
