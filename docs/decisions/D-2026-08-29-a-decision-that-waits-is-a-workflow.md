# D-2026-08-29-a-decision-that-waits-is-a-workflow — the durable wait

**Status:** accepted · **Date:** 2026-08-29 · Second of the eight infrastructure findings from the
2026-08-28 audit (F2). Retracts a factual claim in
`D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use`; that ADR's *decision* stands.

## Context

**Nothing in this system could wait.**

```
$ grep -rn "workflow.signal\|wait_condition\|workflow.update" src/
(no matches)
```

Temporal has been here since Phase 1 and was used exclusively for compute that starts and finishes.
Every human decision was modelled as refuse-and-retry: the plan gate refuses, the turn ends, a
person clicks `POST /sessions/{id}/plan/decision`, a row lands in `plan_approvals`, and a *later*
turn proceeds. That is right for a chemist inside a conversation and cannot represent a process that
outlives one.

The consequence ran in both directions, and the bench half is the one that is easy to miss.
`science/bo/objectives.py` registers exactly two objectives — a literature benchmark and a computed
log S — and both are `Callable[..., Awaitable[float]]`. A registry of functions is what a *simulated*
campaign needs and exactly what a chemist's campaign is not: BO's value to a process chemist is
proposing eight conditions, waiting a week for the plates, and proposing eight more. The engine
could do that; the infrastructure could not suspend for the week. For a project leader the gap is
the whole job — a gate review, a CRO deliverable, a stability pull and a change control are all
long-lived multi-party processes with deadlines, escalations and the possibility of no answer.

### The retraction

`D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use` declined Temporal's LangGraph plugin on
two grounds. The first is sound: this system uses no `interrupt()`. The second is not —

> `agent/interaction_tools.py::start_approval` calls `client.start_workflow` directly. The hold is
> *already a Temporal workflow*.

Neither that module nor that function has ever existed in `src/`. `grep -rn start_approval src/`
returns nothing; the plan gate is a Postgres row (`agent/plan_approval_store.py`) and a refusal
(`agent/plan_gate.py`). The decision to decline the plugin is unaffected and is not superseded — but
the reason recorded in the upstream-capability register was false, and that register is what future
dependency bumps are judged against. This is the `set_current_specialist` shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` names: a claim with no
producer. The register row is corrected in the same change.

## Decision

**One primitive: `chemclaw.durable.awaiting.AwaitAnswerWorkflow` — a question, a deadline, an
escalation, and an answer that may never come.**

One, deliberately, with several callers rather than one shape per caller. A BO round awaiting
measurements, a gate awaiting a committee, a stability pull awaiting a timepoint and an effect
awaiting an approval are the same object, and building the second one separately means a second
deadline, a second escalation and a second set of races.
`tests/test_awaiting.py::test_the_tree_has_exactly_one_durable_wait` scans the package for a second
`workflow.wait_condition` and fails on one.

### Four properties, each of which is the decision rather than the implementation

**1. The answer is unsigned, so it is attribution and never authorization.**
`D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned` found the durable layer lifting a
role set out of an unsigned workflow payload and stamping it into the contextvar a privileged gate
reads — full impersonation, with the audit trail then naming the impersonated user. A signal is the
same channel: anyone who can reach the broker can send one. So `Answer` carries `answered_by` and a
payload, and **nothing else**; who may answer is decided at the front door
(`api/routes/pending.py::_may_answer`) before the signal is sent, the same reason a plan decision
and a proposal decision are routes rather than tools. An absence test pins that `Answer` grows no
role-shaped field.

The requester is deliberately not privileged by being the requester: "I asked the QA lead to approve
this" must not also mean "and I may approve it myself".

**2. Expiry is an outcome, not a failure.** A wait that raised on its deadline would be *retried* by
Temporal rather than reported to the person who asked. `AwaitOutcome.state` is `answered`, `expired`
or `cancelled`, and the requester's mailbox is told when nobody answered — which is the one ending
nobody is watching for.

**3. First answer wins, and an expiry racing a click is not decided by commit order.** The workflow
keeps the first signal and ignores the rest (ignored rather than rejected: a signal has no reply
channel, so raising would fail the workflow task and retry the send forever). The store's
`settle_request` transitions only `WHERE state = 'waiting'` and *returns whether it was the one that
settled*. Both halves are load-bearing — the workflow's guard is per-run, and the two writers here
are two processes.

**4. It projects itself into a table, because Temporal cannot answer "what is waiting on me".**
The broker knows every open run and knows nothing about the subject line, the requester or the
reason, and listing per user means a visibility query against a self-hosted broker.
`pending_requests` is that projection, written by the workflow's own activities exactly as
`job_records` projects a finished job. A `CHECK` refuses an `answered` row with no timestamp and no
actor, the rule `note_proposals` already applies to a decision.

### The first caller: a measured BO campaign

`objective_name="measured"` is a name deliberately **absent** from the objective registry, because
there is no function to register. `BoCampaignWorkflow._evaluate` takes one branch — compute through
the activity, or suspend on a child `AwaitAnswerWorkflow` — so the seed and every round behave
identically. A child workflow rather than an activity: an activity has a start-to-close budget and a
heartbeat, and a week is neither.

An **expired** round ends the campaign with what it has rather than continuing. Proceeding would fit
a surrogate to a batch nobody ran and propose the next round from it, which is
`require_direction_matches_objective`'s inverted campaign wearing a different costume — every number
correct and the recommendation meaningless.

The launch-time direction check is skipped for a measured objective rather than defaulted: it has no
registered direction to disagree with, and comparing against an invented one would either pass
vacuously or refuse a campaign for disagreeing with nothing.

### `ALLOW_DUPLICATE`, which is the policy `start_approval` was said to be missing

D-2026-08-25 recorded that a decided hold could be restarted under the same id because no
`id_reuse_policy` was set, and that the two obvious answers are both wrong: expiry *completes*
normally, so `REJECT_DUPLICATE` and `ALLOW_DUPLICATE_FAILED_ONLY` would each make a lapsed question
unaskable forever while its button still rendered. `ALLOW_DUPLICATE` is right precisely because
expiry is an ordinary ending — asking again after a deadline passed is a new ask — and
`WorkflowAlreadyStartedError` still joins a *running* one, so asking twice for the same thing is one
wait.

## Two defects the tests found, both of which would have been silent

**A wait with no session failed outright.** `notify_session_best_effort` guards the *activity*, not
its argument, and `SessionEventInput.session_id` is `min_length=1` — so building the push-back for a
sessionless wait raised `ValidationError` in workflow code, which no `except ActivityError` can
catch. Every wait raised by a workflow rather than by a turn (a resumed campaign, an effect approved
from an inbox) would have failed on the path where nobody is listening, which is the exact inversion
of best-effort. `_push` returns early when there is no addressee; the request stays open, in the
inbox, on its deadline.

**`workflow.wait_condition` raises on timeout rather than returning.** The escalation loop treated
the timeout as a return value, so the first chase would have failed the entire wait. Caught
explicitly, with the `while` and the `due_at` check deciding which of the two events it was.

Neither is visible without a broker, which is why these tests drive the time-skipping server rather
than calling the workflow body.

## Consequences

- `request_external_input` is an expensive action (`CORE_EXPENSIVE_ACTIONS`). The resource it spends
  is not this deployment's — it is a person's week or a lab's queue — which is a stronger argument
  for the gate rather than a weaker one: a report nobody wanted costs tokens, and four reactions
  nobody wanted costs four reactions.
- There is no tool that *answers* a wait, and there must not be. A model able to settle its own
  request could ask itself for approval and grant it in the next tool call.
- `CHEMCLAW_AWAITING_MAX_DAYS` (90) is a ceiling, not a default: a wait is a workflow run held open
  on the broker, and an unbounded one is a resource nobody reclaims.
- A campaign that names `measured` and is never answered ends `expired` with a partial history.
  That is a real outcome a chemist will meet, and the note it produces says so.
