# D-032 — Durable async approval hold for captured user answers (Yes/No button seam)

**Context.** `record_confirmed_answer` (D-031) proposes an interaction note synchronously,
inside one agent turn. A chat "save this knowledge? [Yes]/[No]" affordance is *asynchronous*:
the human may click minutes later, after the turn or session has ended, so the pending
candidate must outlive the conversation. The architecture rule is that durability lives only in
Temporal, never in MAF — so the pending state cannot sit in the agent's in-memory session.

**Decision.** Added `workflows/interaction_approval.py`: `InteractionApprovalWorkflow` holds one
candidate (`InteractionCandidate`), waits on a bounded `wait_condition` for a `decide(approved)`
signal — the button click — and only on Yes runs an activity that proposes the note through the
PR-gate. Reject or timeout ends the workflow without proposing (`ApprovalOutcome.status` =
`approved`/`rejected`/`expired`). A `status` query lets a polling UI render the button. The hold
is durable: restarting a worker mid-wait resumes from history. Runs on `background-jobs`;
registered on the background worker.

**Why the button gates the proposal, not the merge.** An approved candidate still lands on a
feature branch for the real human PR review (D-005 unchanged) — a chat click is not an auditable
GxP sign-off. Collapsing the PR-gate into the button was rejected; it would need its own ADR.

**DRY.** The build-and-gate logic moved into `memory.interaction.propose_confirmed_answer` (two
real callers now: the synchronous agent tool and the durable activity), so the inline tool and
the Yes button produce byte-identical PRs. The hold timeout is config
(`interaction_approval_timeout_seconds`, default 7 days), never hardcoded.

**Scope.** Backend seam only — no frontend. It exposes exactly what a future chat UI hooks onto:
start-workflow (surface candidate) → `decide` signal (click) → `ApprovalOutcome` (PR ref).

**Result.** `make lint type test` green: 231 passed / 17 skipped (sandbox-infra only). New tests:
the in-sandbox signal/query state machine + worker registration, and a server-backed test
(CI; skips offline) proving Yes proposes exactly one PR while No and an unanswered (time-skipped)
hold propose none.
