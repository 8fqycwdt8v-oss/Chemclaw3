# D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read — the identity a human decides on covers the declaration they are deciding on

`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` bounded what an approval
authorizes: each plan step declares the tools it will call, the decision stamps the union of those
declarations onto `plan_approvals.scope`, and `enforce_plan_approval` reads the scope back off the
**row** rather than off the live plan. That direction is right and it is not sufficient, because the
row is written by reading the live plan *at decide time*, and the only thing standing between the
plan a chemist read and the plan the route reads is a hash that did not cover the declaration.

## What was measured

`api/routes/plan.py::decide_plan` guards on freshness: the posted `plan_hash` must equal
`plan_identity(...)` of the plan the session is proposing now, or it is a 409 and nothing is
recorded. `plan_identity` hashed each step's `content` and nothing else — deliberately, so that
ticking a step does not revoke the approval it is making progress under. A rewrite that keeps every
step's text and widens its `tools` therefore hashed **identically**, and the chemist's own hash still
satisfied the guard.

Driven end to end through `POST /sessions/{id}/plan/decision` on the real app, with the plan read
stubbed only at the checkpointer seam:

| step | observed |
| --- | --- |
| `GET /sessions/{id}/plan` — what the chemist reads | `scope: []`, `plan_hash: d8531b0e50d9e7d0` |
| the model rewrites: same step text, `tools: ['record_knowledge_note', 'watch_for']` | `scope: ['record_knowledge_note', 'watch_for']`, `plan_hash: d8531b0e50d9e7d0` — **unchanged** |
| `POST .../plan/decision` with `plan_hash: d8531b0e50d9e7d0`, `approved: true` | **204** |
| the recorded row | `scope = {record_knowledge_note, watch_for}` |

Both tools then executed under an approval whose card said the plan authorized nothing.

**No concurrency is involved, and that is what makes it reachable rather than theoretical.** An
unapproved plan is not a hold: the turn ends, the approval card is emitted, and the session is free
to take another turn while the card sits on the chemist's screen. `out_of_scope_refusal` — the
sentence a refused call hands the model — says in as many words *"Rewrite the plan so a step declares
`<tool>`, and ask for the new plan to be approved."* A model that follows that instruction by keeping
the step text produces exactly this input, and the re-approval the chemist is being asked for is the
one they already have open.

**Three present-tense docstrings asserted the design was closed**, each written by the session that
built the half it describes: `agent/plan_scope.py`'s header ("a rewrite that keeps every step's text
and adds a tool to it hashes to the same approved plan and gains nothing, because the stamped scope
is the one the human saw"), `enforce_plan_approval`'s ("the model's declaration is never what is
consulted"), and the comment beside the `declared_scope(plan)` argument in `decide_plan` itself —
which sits three lines below the read that falsifies it. All three are true of the *gate* and none of
them is true of the *route*, which is the half that writes the row the gate reads. This is
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` in its other form: not a stale number, a
sound argument about the wrong subject.

## The decision

**`plan_identity` takes the plan's steps and hashes each step's `content` beside its declaration.**
The identity a human decides on is now the whole of what they are deciding, so a widening rewrite is
a *different plan*: the 409 fires, the chemist re-reads a card that now shows
`['record_knowledge_note', 'watch_for']`, and approving is approving that.

Three properties make it the right shape rather than merely a stricter one:

- **`status` stays out.** That is the property the content-only rule existed for and it is kept
  intact: the canonical `TodoListMiddleware` batch — tick step N `completed`, mark step N+1
  `in_progress`, call step N+1's tool — hashes identically, so an approved multi-step plan does not
  revoke itself by making progress. Losing that re-opens the livelock
  `D-2026-08-27-the-approval-follows-the-turn-not-the-hash` closed: the model retries, the identical
  retry trips `refuse_repeated_calls`, and the plan burns its loop allowance.
- **One reading of a declaration, not two.** `plan_scope.step_declaration` is the primitive under
  both the recorded scope and the identity. Had the identity read `tools` differently from
  `declared_scope` — kept a value the scope narrows, or narrowed one the scope keeps — there would
  again be a pair of plans that authorize differently and hash alike, which is this defect with an
  extra step. It sorts and deduplicates, because neither a step's declaration order nor a repeat in
  it changes what the step may call, and an identity that moved on a reorder would revoke a live
  approval for a change nobody can see.
- **The callers hand over steps, not text.** Every site that computes an identity — the gate's
  in-turn read and its batch read, the decision route, the inbox, the CLI's `/approve`, the streamed
  `PlanEvent`, the job's plan link — now passes the steps themselves. `plan_state.session_todos`,
  whose only purpose was to answer the narrow shape, is **deleted**: a reader that dropped `tools`
  was a reader that could only produce a hash matching no decision, which is the failure
  `plan_state`'s own header already records for the *rendered* line. `api/graph_stream._todo_contents`
  went the same way, becoming `_plan_steps`.

## What it costs

**Every `plan_approvals` row written before this commit is keyed on the narrower hash and can no
longer be matched.** That is the fail-closed direction and it is cheap, because an approval
authorizes one turn and is spent when that turn ends (D-167): the cost is a chemist pressing approve
again on a plan that is still on their screen. No migration, and deliberately none — a backfill would
have to invent the declaration each old row was taken over.

**A rewrite that drops a step's `tools` is now also a different plan.** The tool's argument schema
makes `tools` required (`plan_scope.ScopedWriteTodosInput`), so a `write_todos` without it never
reaches the plan at all — the model reads a validation error and retries. The residue is a model that
rewrites a step with an *empty* declaration while a job it declared is in flight; that refuses,
which is the correct direction and is the same answer it has always given for a reworded step.

**The refusal sentence changes for the in-batch widening case.** A gated call batched with a
declaration-widening `write_todos` used to find the standing approval and be refused by
`out_of_scope_refusal` ("the approved plan does not list it"); it is now refused by
`plan_approval_refusal` ("the plan it is part of has not been approved yet"), because there is no
approval for the plan the batch writes. One step earlier and for the stronger reason. Both sentences
point at the same remedy.

## What was not done

`plan_identity` is still content-addressed over a list, so two sessions proposing the same plan with
the same declarations share an identity. That has always been true and the store is keyed by
`(session_id, plan_hash)`, so it authorizes nothing across sessions.

The declaration bound remains unbounded in size (a `BACKLOG.md` row): 50,000 names validate, land in
`plan_approvals.scope` and size a refusal that `_refusal_message` clips. Widening the hash does not
change that and the hash is not where it should be fixed.

## What keeps it true

| property | test |
| --- | --- |
| a rewrite that widens a declaration while the decision card is open does not pass the route's freshness guard, and records nothing — driven through the real `POST .../plan/decision` | `tests/test_plan_scope.py::test_a_rewrite_that_widens_a_declaration_does_not_pass_the_freshness_guard` |
| the identity moves with a step's declaration, does **not** move with its `status`, and does not move on a reorder within one step | `tests/test_plan_scope.py::test_the_plans_identity_moves_with_its_declaration_and_not_with_its_progress` |
| widening a declaration after approval still refuses the widened tool — the effect assertion, now standing on an identity that has also moved | `tests/test_plan_scope.py::test_widening_a_step_after_approval_does_not_widen_the_approval` |
| the canonical tick-and-act batch still passes on its standing approval, carrying each step's declaration | `tests/test_plan_gate.py::test_ticking_a_step_beside_the_steps_own_call_is_allowed` |
| the decision route still records exactly what the card displayed, for an unrewritten plan | `tests/test_plan_scope.py::test_the_decision_route_records_what_the_plan_declared` |
| the streamed `PlanEvent`'s hash is the one a decision must be posted against, and is not a hash of the rendered checklist | `tests/test_langgraph_stream.py::test_a_streamed_plan_carries_the_hash_a_decision_must_be_posted_against` |
| the inbox row names the identity the gate would ask about | `tests/test_plan_inbox.py::test_an_undecided_plan_is_listed_with_the_conversation_that_holds_it` |
| the CLI's `/approve` records against the identity the gate reads | `tests/test_cli.py::test_approve_records_and_arms_a_real_plan` |
| a job's plan link carries the same identity the approval row is keyed on | `tests/test_plan_link.py::test_the_link_is_the_first_in_progress_step_and_the_plans_own_identity` |
| `session_plan` answers the steps whole, and tells an unreadable plan from an empty one | `tests/test_plan_state.py` |

Four mutations were driven, each watched failing before this was written, and each restored from a
`.bak` rather than from git:

| mutation | result |
| --- | --- |
| `plan_identity` hashes `content` only again (the pre-fix expression) | 3 red in `test_plan_scope.py`, including the route repro |
| `plan_identity` also hashes `status` | 2 red — the identity test and the canonical tick-and-act batch |
| `decide_plan` derives its identity from step text only | 2 red — the repro, and the route's own scope test |
| `step_declaration` returns the declaration in input order instead of sorted | 1 red — the reorder arm |

The repro itself was measured against the unmodified pre-fix tree before any of this was written:
**204, with `{record_knowledge_note, watch_for}` recorded.**

**One test was deleted rather than repaired.**
`tests/test_langgraph_agent.py::test_both_engines_hash_a_plan_to_the_same_identity` asserted
`plan_identity([t["content"] for t in todos]) == plan_identity(titles)` over a `todos` built from
`titles` on the line above — a value compared with itself — and its subject was parity with MAF,
which has not existed since M13. It would have survived this entire change green. The property worth
having in its place is what the identity *is* taken over, which is the second row of the table above.
