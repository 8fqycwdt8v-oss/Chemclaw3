# D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes — what two adversarial reviews found in the outbound delivery seam, and the three factual errors in the records that shipped with it

## Status

Accepted. Corrects `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` and
`D-2026-09-14-a-bundle-this-tree-does-not-declare-is-still-reachable` without editing either; both
decisions stand and both made claims the code does not support.

## Context

Wave 1 merged as `aabf4fb`. Two fresh-context reviewers were then given the diff and told to prove
a defect with a script rather than argue it. Between them they returned twenty-three findings, of
which one was production-breaking and had been argued *against* on the page of the ADR that shipped
it.

## What the reviews found

**A new `await` in a live workflow path is a replay break, and the seam put one in two.** Temporal
replays a workflow by matching the command sequence its code emits against the sequence its history
records. `AwaitAnswerWorkflow._push` runs before `_wait_until`, so a wait opened on the previous
release holds `TimerStarted` where the new code emits `ActivityTaskScheduled` — measured,
`[TMPRL1100] Nondeterminism error: Activity machine does not handle this event`. That workflow is
declared `failure_exception_types=[Exception]`, and a `NondeterminismError` is an
`ApplicationError`, so it does not park: it **fails the wait**, past the cleanup clause, leaving a
`pending_requests` row `waiting` with no run that will ever settle it — for up to
`awaiting_max_days`, which ships at 90. `DigestWorkflow` is the same class with the opposite
failure: it emitted a *different activity type* at a position its history already held, declares no
`failure_exception_types`, and therefore parks its workflow task in an unbounded retry — and being
a Schedule under `ScheduleOverlapPolicy.SKIP`, one wedged run then skips every subsequent nightly
digest **silently**.

`grep -rn "workflow.patched\|get_version" src/` returned nothing before this fix: the tree had no
versioning at all, which is why nobody reached for it. `AwaitAnswerWorkflow.run`'s own comment
already stated the rule it broke — *"a timeout that changed between runs is tolerated and a timer
that appears or vanishes is not"* — and the shipping ADR argued the enablement check belongs inside
the activity **precisely so** a restart cannot replay a command its history lacks. The argument was
on the page while the commit did the thing it forbids, at deploy time rather than at enable time.

**A reservation went stale by a whole step, under a test that could not see it.**
`finish_headroom` sums what the wrapper may spend after its child returns; the seam added a sixth
post-child activity and left the sum at five, leaving 930 s of permitted spend outside the ceiling
whose only job is to cover it — re-opening the reaped-before-it-can-report failure that function was
rewritten to end. `tests/test_template_job_step.py` asserted `finish_headroom() >= <the five steps
transcribed>`, and a `>=` catches a step whose bound *moves* while being blind to a step being
*added*, because an added step makes the assertion truer.

**A guard written to prove a producer exists proved only that a constructor was typed.**
`test_every_declared_delivery_kind_has_a_producer` scanned for `OutboundMessage(kind=...)` and never
asked whether anything sent it. Driven: deleting the `await deliver_best_effort(...)` from
`AwaitingWorkflow._push`, and binding the report's message to an unused local, both leave a declared
kind that nothing delivers **and the test green**. The same scan read only `ast.Name`, so an
ordinary qualified spelling turned it red — the mirror defect, and
`tests/test_activity_queue_bound.py` records fixing exactly that in its own walk one file over.

**Three budgets and one key were wrong in ways that manufacture duplicates or lose messages.** The
digest's delivery budget was tightened 10× (300 s → 30 s) and 4× (3,600 s → 900 s) by inheriting
`durable/notify.py`'s shape, which bounds one small database insert, where this walks the enabled
channels *serially* over the network — three webhook channels at their own 10 s reach the whole
budget, and a `start_to_close` expiry is retryable, so the retry re-POSTs to channels that already
took the message. `message_id` excluded `correlation_id` on the reasoning that it must not be
*rendered* to a recipient, which is true of the payload and does not follow for a key: measured, two
distinct runs of one job for one chemist produced the identical id, so a compliant receiver drops the
second real result by design. And `enabled()` raising on an unresolvable channel name sat *outside*
`deliver`'s per-channel `try`, so one typo cost every healthy channel its message — the exact
inverse of that function's headline promise.

**The `awaiting` kind reached nobody for its most important caller.** `asked_of` is documented as
"'' for anyone entitled", `request_external_input` tells the model empty is *the right default when
you do not know the name*, and `connectors/bo/workflows.py` never sets it — on the longest-lived
wait in the tree, the one whose whole point is a chemist running plates over a week. Measured across
every real producer: with `asked_of` empty, the opening notice and every reminder sent nothing.

**And the failure half of a durable job still told nobody.** The `job-result` copy was added to
`_finish`, which is the success path alone; `_notify_failure` returns early when there is no session,
which is exactly the Schedule- or inbox-started run the outbound copy exists for.

## Decision

Every finding above is fixed in the commit carrying this record. Three that could not be fixed by
code are corrected here, because a merged ADR is never edited:

1. **"Nothing changes in a shipped deployment" is false**, in
   `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel`. It is true of a fresh
   deployment and false of every live one: any `AwaitAnswerWorkflow` or `DigestWorkflow` in flight
   at deploy time breaks on its first replay. What makes the sentence true now is
   `workflow.patched`, not the shipped code it described.
2. **The `pyexec` operator recipe is incomplete and renders a silently unreachable connector.**
   `D-2026-09-14-a-bundle-this-tree-does-not-declare-is-still-reachable` lists
   `connectors.pyexec` "with its `url:` (which is also what turns it on)". `chemclaw.connectorUrls`
   visits only `enabled && server` entries, so without `server: true` the address is **ignored** and
   the front door falls back to the manifest's `http://127.0.0.1:8899/mcp` — rendered, exit 0, no
   warning, `CHEMCLAW_CONNECTOR_URLS` with no `pyexec` key. The recipe also omits the
   `networkPolicy.egressDestinations` entry. `deploy/README.md` has it right and is the recipe to
   follow; the ADR's four-line summary of it is not.
3. **Two counts in those ADRs are wrong in the direction that understates their own case.**
   "`pyexec` is named in five files" counts 7 lines and 9 occurrences in `up.sh` alone; "refused in
   three places" is two controls and a header comment — `check-openapi.mjs` excuses an omitted
   backend route *generically*, not `/schedules` by name.

## Consequences

- **The tree has versioning now, and a patch is temporary by construction.**
  `tests/test_workflow_versioning.py` holds the two ways a correct patch is silently undone
  afterwards — reusing an id, and deleting the code the off branch still schedules — and carries the
  removal condition for each. `deliver_digest_activity` is back as a named shim with no body of its
  own, delegating to the one seam, removable the day after this ships.
- **A reservation is now an equality rather than a floor.** Reserving too much wedges a long job no
  less than reserving too little reaps it early, so the test fails in both directions.
- **The producer scan starts at the send site and works inwards one hop**, and fails closed: a
  producer hidden behind two hops reads as unproduced rather than being credited on a chain nobody
  verified.
- **An unowned wait now reaches its requester** rather than nobody, with a subject that says nobody
  is named rather than pretending the notice found an owner.
- **One residual is accepted and stated**: `deliver_best_effort` bounds the caller's *failure* and
  not its *delay*. Measured against an unserved queue, a wait whose answer arrived at 3 s was still
  `RUNNING` at 75 s. That is inherent to scheduling an activity at all, it is the same 75 s
  `durable/notify.py` measured for its own seam, and it is now in the docstring instead of only the
  reassuring half.

## What keeps it true

- `tests/test_workflow_versioning.py` — patch ids are unique, the patched-off branch still has the
  code its histories recorded, and that branch is still reachable from the workflow that needs it.
- `tests/test_template_job_step.py::test_the_wrappers_headroom_covers_what_its_post_child_steps_may_spend`
  and its config-restatement sibling — equality, both directions, over six steps.
- `tests/test_outbound_delivery.py` — the send-site scan (three mutations watched: builder-without-
  send red, unused-local red, qualified spelling green), one typo'd channel not costing the healthy
  ones, and a re-run being two messages where a retry is one.
- `tests/test_activity_queue_bound.py` — re-based from 30 to the measured 41 dispatch sites.
