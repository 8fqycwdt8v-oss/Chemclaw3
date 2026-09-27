# D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late — a scheduled check-in over a requester's own blocked work, and why it runs no model

**Status:** accepted · **Date:** 2026-09-15 · Adds a Schedule beside the ones
`durable/schedules.py` already plans. Does not change `durable/awaiting.py`'s own notices, which
this sits between. Declines, with its reason, the agent-on-a-timer version the evaluation that
prompted it proposed.

## Context

From the Paperclip evaluation (`tasks/paperclip-ideas-2026-09-15.md`). Two findings led here, and
the first corrected my own earlier claim.

**1. Timers are not forbidden here; timers that *decide* are.** The first pass of that evaluation
said scheduled agent work was ruled out. It is not: `OWNED_SCHEDULE_IDS` held 15 ids and
`planned_schedules()` conditionally plans about a dozen. The actual rule is that function's own
docstring — *"What is left on a timer is ingestion, indexing, eviction and retention: jobs that make
the corpus queryable and none that decide what it means."*

**2. The durable wait already wakes people on a timer, and it wakes the wrong one for this.**
`_awaiting_message` states the split outright: *"while the wait is open it is an ask, and the person
who has to act is `asked_of`; when it expires it is a report, and the person who needs to hear it is
the requester."* That is right for those two notices, and it leaves a gap between them.
`awaiting_max_days` is 90. So a chemist whose Suzuki screen is suspended on a measurement can hear
**nothing at all** about it for three months, and then hear that it expired unanswered.

## Decision

`durable/check_in.py` — a nightly sweep, off by default, that tells each **requester** which of
their own questions are still waiting, before the deadline rather than after it.

Four predicates decide who hears what, and each excludes a population that would otherwise be told
the wrong thing: `state = 'waiting'` (an answered question is not news), `requested_by <> ''` (a row
with no actor cannot be reported *to* anyone), `due_at > now()` (the expired already reached their
requester through the wait's own notice, and repeating it would make this a second, worse copy
arriving nightly), and a `check_in_quiet_days` threshold (a question asked this morning is not news
to the person who asked it, and a check-in that said so would train its reader to skip the next).

It shares the digest's mailbox and nothing else: `CHECK_IN_KIND` is a kind of its own, because a
surface must be able to tell "new knowledge matched your query" from "your own work is stuck" — one
label over both would make the second unfindable among the first.

## It runs no model, and that is the substantive part of this decision

The version the evaluation proposed — and that I argued for — has an agent read the blocked work and
say what it is blocking, over the read-only narrowing (`subagents.helper_profile`, which subtracts
`authz.side_effecting_tools()`). The machinery to do it exists and is better than that narrowing:
`durable/template_activities.run_agent_step` takes a prompt, a profile, and a `write_tools` list
that is a real security narrowing carried across the activity boundary for a stated reason.

**It also takes a `StepIdentity`, and that is what stops this.** A worker has no request context, so
an agent step is run *as* somebody: that identity is stamped ambient before the work runs, and it is
what makes the audit trail name a real person and what makes `enforce_tool_authz` decide against
that person rather than against nobody. A template run has one because a person started it. **A
Schedule has none.** Synthesizing one from a `requested_by` string would be this system granting
itself a chemist's identity, on a timer, to do work that chemist did not ask for.

That is not a question `write_tools=[]` answers. A narrowing bounds what an actor may do; it does
not supply the actor. `require_actor`'s reject-if-absent core rule and
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` both exist to refuse exactly this shape,
and `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` is what happens when a
trail names an actor nothing can produce.

So the sweep reports and interprets nothing. What it sends is what the requester themselves wrote —
their own `subject` and `rationale` — plus how long it has been open and how long is left. That is
actionable without a model, costs nothing per firing, and keeps this inside the line
`planned_schedules()` draws without having to argue around it.

**The claim is asserted, not promised.** `test_the_sweep_runs_no_model` walks the module's AST for
`run_agent_step`, `AgentStepInput`, `StepIdentity` and `build_langgraph_agent`. Its first draft
scanned `source.split('\"\"\"')[2]` — measured at **18% of the file** — so a violation anywhere below
the first class docstring passed; planting one at the bottom proved the AST version catches what the
slice version missed.

## The mailbox needed a reader, and the first version of this shipped without one

Caught before merge, and worth recording because it is the defect this ADR's own settings comment
cites. `CHECK_IN_KIND` was written into the digest mailbox and **nothing claimed it**:
`read_digests` claims `DIGEST_KIND` only, so a check-in would have landed nightly in a mailbox
nobody opens while the sweep reported success — `D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`
a second time, and `D-2026-09-15-a-watch-that-nothing-evaluates-is-a-promise-a-deployment-cannot-keep`
a second time, in a commit that cites the latter.

Both halves passed their own tests. The activity grouped correctly and the route returned digests
correctly; what nothing asserted was that the thing written could be read.
`test_the_mailbox_the_sweep_writes_is_one_a_reader_can_open` is that assertion, end to end.

`GET /check-ins` is the reader — **a route of its own rather than a second list on `/digests`**,
because that route answers `list[Digest]`: folding these in would mean either changing its response
into an object, which breaks a client reading it today, or widening `Digest` with fields that have
nothing to do with a standing query. A check-in and a digest ask the reader for different things.
They share a mailbox and nothing else.

## The failure stance is the opposite of its neighbours'

`CheckInWorkflow` is in `_MUST_FAIL`, where every other nightly sweep on the queue is in
`_MAY_PARK`. Those park because nobody is waiting on them and a bug should wait for a fix. Here
**silence is the output**: a check-in that delivers nothing is indistinguishable from "nothing of
yours is blocked", which is the good state — so a parked run reads as reassurance. Under
`ScheduleOverlapPolicy.SKIP` one parked run then skips every subsequent night, and the requester
goes back to hearing nothing until expiry. That is the defect this sweep exists to fix, reproduced
by its own failure mode. A failure reaches an operator through `ScheduleHealth.last_outcome`; a
park reaches nobody.

## Off by default

`check_in_enabled = False`. It delivers to a mailbox and an outbound channel, so a deployment that
has configured neither would run a nightly sweep to write where nobody reads — which is what
`digest_enabled` shipping off for the *wrong* reason already cost once
(`D-2026-09-15-a-watch-that-nothing-evaluates-is-a-promise-a-deployment-cannot-keep`). Gated on a
flag rather than on a registry, unlike its four neighbours in `planned_schedules()`, because there
is no manifest to ask: the table it reads is one every deployment has, and what a deployment chooses
is whether its people want to hear about it.

`agent-check-in` is in `OWNED_SCHEDULE_IDS`, which is the prune namespace: an id missing from it is
a Schedule the applier is not authorised to delete, so turning the feature off would strand it
firing forever — the failure `commitment-mirror` and `result-publish` were each registered late to
avoid.

## What this does not do

- **It does not cover a finished job whose result nobody read.** Unconsumed `session_events` for
  sessions nobody reopens is a real, separate accumulation with its own `DEFERRED.md` row, and
  folding it in here would give one sweep two subjects and two growth stories.
- **It does not chase the person who owes the answer.** That is `awaiting.py`'s reminders, which
  already run and already address `asked_of`.
- **It does not interpret.** See above. A chemist reading "open 9 days, 5 left" knows more than
  they did; they do not learn anything this system inferred.
- **`Chemclaw3_ui` does not call `/check-ins` yet.** The backend is complete and the route is
  reachable; until the UI reads it, a deployment sees check-ins only through a configured outbound
  channel. That is a companion-repo change and is queued in `docs/planning/BACKLOG.md` rather than
  left to be discovered, which is what `check_in_enabled`'s own comment means by naming what a
  deployment needs before turning it on.
- **It has no archived replay history.** `tests/test_workflow_replay.py` names it as uncovered with
  the reason: this control catches today's code refusing a history the *shipped* code wrote, and
  this workflow has never shipped, so a fixture recorded now would be the self-certifying shape
  `recorded_workflow_histories.py` rejects. It earns one at its first change.
