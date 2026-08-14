# D-2026-08-13-the-challenge-panel-is-generated-per-task-not-declared — An answer is attacked by agents briefed for it, and a team is attacked unconditionally

Why a fixed set of reviewer personas is the wrong shape, why work split across agents is challenged
whatever the confidence score says, and where the mesh gate finally opened.

## What was missing

`agent/verifier.py` scores a finished answer for citation faithfulness and stamps
`AnswerEvent.review_required`. It is a judge over the transcript: it reads what the turn produced
and asks whether the claims are supported by what the tools returned. **It calls no tools.** So a
claim the turn never gathered evidence for is *unverifiable* to it rather than wrong — the honest
limit `_deterministic_result` states at length — and "this citation resolves" is as far as it can
get toward "this citation does not support this sentence".

Nothing anywhere spawned a second agent to go and check. `docs/planning/DEFERRED.md` recorded the
other half: `review_required` is wired to nothing, so a weakly-grounded answer is *marked*, never
re-checked.

## Decision

**A finished answer is put to a panel of independently-briefed agents, and the panel's angles are
generated for that answer rather than declared in this repo.**

`agent/challenge.py::draft_briefs` asks one structured call for the angles worth taking against the
answer in hand; each brief becomes one challenger's whole instruction. Every challenger runs on
`data/profiles/challenger.yaml` — read-only, checked against the answering agent by
`reject_widening` — and is compiled by `build_langgraph_agent`, never as a bare deepagents
`SubAgent` dict (`D-2026-08-13-a-subagent-is-spawned-for-isolation-not-for-a-tool-it-lacks` says why
that is a security property).

**A fixed persona list was the first design and it is wrong in both directions.** An evidence lens,
a safety lens and a methodology lens spend a model call on a hazard review of a turn that touched no
substance, and have no lens at all for the failure mode peculiar to *this* question. The panel is an
**ensemble, not a capability partition**: every member holds the same read-only surface and differs
only in what it was told to doubt. That is also why the members are not in `SPECIALISTS` and not
gated by `agent_teams_enabled` — coupling a review mechanism to a separately-measured delegation
feature would make one deployment decision silently change the other.

## The trigger is the shape of the turn

- **Two or more helpers ran → challenged, unconditionally.** Work split across agents is exactly the
  case where no single context saw the whole thing: each helper reported on its piece, the
  supervisor stitched the pieces together, and the seams are where a contradiction survives. A check
  that scores one finished answer against one turn's evidence has no instrument for that, whatever
  its confidence comes out at.
- **One helper or none → challenged only when already flagged.** Here the existing checks keep their
  meaning and the panel is the second opinion they cannot give themselves.

The count is `team.delegations()` — a counted tally, not a shape read off the message list, for
`agent/loop_cap.py`'s reason: the alternative to counting is an inference, and the inference is what
breaks quietly. **No model decides whether to be challenged**; the trigger is arithmetic on a tally
and a boolean, and `can_jump_to` makes the branch a real graph edge rather than a decision the graph
ignores — the defect `enforce_loop_cap` documents at length.

## What an upheld objection does

A **quorum** rather than any-one-objects: a persona instructed to find fault will find fault, and
agreement between independently-briefed angles is what separates a defect from one challenger's
enthusiasm. A corroboration with no stated rationale does not count toward it, by the rule
`runner_answer` already applies — a flag a reviewer cannot act on is not a finding.

On quorum, in order:

1. **Revise, while attempts remain.** The objections go back as a `HumanMessage` and the graph jumps
   to `model`. The message is the mechanism, not a state field: jumping alone would re-run the model
   over exactly the input that produced the answer under objection, and produce it again. It also
   leaves the revision's reason in the thread an auditor can read.
2. **Surface it, once they do not.** The objections ride out in `unsupported_claims`, `challenged`
   is stamped, and `durable/answer_review.py` opens a hold so the human decision outlives the
   session. Bounded by `challenge_max_attempts` against a counted state field, because a model and a
   panel can disagree forever.

`AnswerReviewWorkflow` is a new workflow rather than a reuse of D-032's
`InteractionApprovalWorkflow`, whose terminal action is the PR-gate activity that *proposes a note*.
That workflow's question is "should this become part of the record"; this one's is "was this answer
sound". Reusing it would make an approval here propose a note nobody asked to save.

## `interrupt()` is available and deliberately not used

`langgraph.types.interrupt` exists in the installed version and the checkpointer under it already
ships. It is the right primitive for stopping mid-turn to ask the chemist — and resuming it needs a
front-door route sending `Command(resume=…)` plus a surface that renders the question, neither of
which exists. An interrupt nothing can resume is a turn that hangs, which is strictly worse than the
marked answer plus a durable hold this ships. The `docs/planning/DEFERRED.md` row stays, with the
trigger it already had: a surface that renders a hold.

**That row is not deleted**, and this is the one place this change deliberately stops short. Its
trigger needs *both* a surface rendering the hold *and* a deployment deciding an unverifiable answer
must be withheld rather than flagged. This narrows the gap — a hold now exists and carries a real
decision — without closing it.

## The mesh gate is met, not sidestepped

`docs/archive/plans/parity-plan.md:239-241` gated the conversational multi-agent mesh behind: *"a use
case needs >1 specialist persona within one turn that role-scoped skills (D-052) cannot express."*
A challenge panel is N independently-briefed personas inside one turn, and independence *between*
agents is not expressible as a skill — a skill is judgment offered to one agent, and the whole point
here is that the reviewers cannot see each other's reasoning. The trigger fired; the gate opens for
this use case.

`langgraph-supervisor` and `langgraph-swarm` are not adopted, and the reason is scope rather than
safety: both solve *runtime routing*, and this design has no runtime routing decision on either side
— the panel fans out unconditionally over generated briefs.

## Consequence

Off by default (`challenge_enabled`), for the reason `agent_teams_enabled` is off: a panel that
over-flags is worse than no panel, and which one this deployment gets has not been measured.
`chemclaw_challenge_degraded_total` is what makes that measurable rather than believed — every
degraded path returns a *non-corroborating* verdict so an unreachable endpoint cannot hold an
answer, which means a dead panel and a clean answer look identical without it.

## Result

`make lint type test` green. Tests: `test_challenge.py` (surface attenuation and read-only-ness, the
governance-chain guard, panel bounding, per-member failure containment, the angle stamped by the
dispatch, quorum clamping), `test_challenge_gate.py` (the team-vs-flagged trigger, the declared jump
edge, the critique reaching the thread, and termination under an always-corroborating panel),
`test_answer_review.py` (three distinguishable terminal states, first-ruling-wins).
