# D-2026-09-19-a-pod-wide-cap-is-not-a-fair-one — admission gains a per-actor dimension, and keeps refusing the fleet-wide one

## Status

Accepted, 2026-09-19. Adds a second **per-process** dimension to admission control. It does **not**
re-open SCALE-1: `D-2026-08-01-a-per-process-cap-multiplied-by-a-number-nobody-wrote-down` and
`D-2026-08-01-a-cheap-request-is-still-a-request` declined making a limit *fleet-wide*, and that
decline stands with a trigger attached below.

Ships **off** in code (`CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS_PER_ACTOR=0`) and **on** in the chart
at 4 against 12 permits.

## Context

`POST /sessions/{id}/messages` has run under four guards that compose exactly: the per-session
in-process lease and the durable cross-process claim (both 409), the admission semaphore
(queued/shed on the open stream, D-166), and the budget (429). Three of the four are per *session*
or per *process*. The fourth, `api/budget.py`, is per actor and counts **cumulative** turns and
tokens over a window.

Nothing counted an actor's **concurrent** turns. The semaphore is one
`asyncio.Semaphore(service_max_concurrent_turns)` for the whole pod and it does not know who is
asking, so one principal opening that many sessions holds every permit and every other chemist on
the replica is shed `at_capacity` for as long as those turns run.

**This was already measured, from one direction, and the fix taken then was narrower than the
finding.** `chemclaw.api.detach`'s module docstring records it: POSTing and hanging up one fresh
session per permit left every permit on the replica held and every other chemist's turn shed as
`queued` then `error`, for up to `service_turn_timeout_seconds`. The answer was to release the
*permit* at a detach — correct, and it addresses the hang-up variant only. A client that keeps
reading holds its permits for the whole run, and needs no malice to do it: a chemist with a dozen
tabs open is the same arithmetic.

**The guard that looks like it should cover this does not reach it.** `api/rate_limit.py` is per
principal, and the chart sets 120 requests/minute with a burst of 30 — two orders of magnitude above
twelve concurrent turns. A principal can hold the whole replica while spending twelve requests, so
the rate limiter never fires. Rate and concurrency are different quantities and only one of them
was bounded per actor.

**The tree already ships this guard's twin, one route over.** `api/routes/streams.py` bounds
concurrent event streams **twice** — per user (`service_max_event_streams_per_user`) and pod-wide
(`service_max_event_streams_total`) — and states the reason in as many words: *"one bound does not
imply the other… 50 chemists each within their per-user cap is still 250 forever-polling tasks on
one event loop."* Turns had only the second bound. Before this change
`service_max_event_streams_per_user` was the only per-actor concurrency cap anywhere in the tree,
and it covers streams rather than turns.

The gap was found by inspection during a harness audit, not carried on a register: there is no
`BACKLOG.md` row, no `DEFERRED.md` row and no ADR declining it. Nobody should go looking for one.

## Decision

`service_max_concurrent_turns_per_actor` bounds how many turns one principal may have in flight on
one process. Three sub-decisions carry the weight, and two of them answer an adjacent precedent the
*opposite* way — which is why this is a record rather than a commit message.

### 1. The count is derived from the lease map, not kept in a counter

`TurnLease` gains `actor`; `_actor_turns_in_flight` scans `app.state.active_turns`. No new
`app.state` field, no accessor, no release closure.

Copying `streams.py` literally — a `dict[str, int]` keyed by `oid` with a `_release_actor_slot()` —
is the obvious design and is wrong for **this** route. `api/state._claim_turn_slot` documents the
window: *"a client gone after the streaming response is handed off but before its generator is first
advanced. An async generator that never started runs no `finally` at all, so a latch then answered
409 for the pod's whole lifetime."* That is exactly why the session slot is a lease with an expiry.
An integer counter has no expiry, so the same window would leave an actor's count inflated until the
pod restarted — a permanent 429 for one human, reachable by a flaky mobile network, produced by the
guard meant to protect them. Reading the lease inherits the expiry, the identity-checked release and
the existing sweep, and adds no second synchronisation primitive to a module whose last two defects
were both races.

`_start_turn_lease` carries `actor` across its restamp. That call runs at the hand-off, so dropping
the field would leave every lease anonymous from the moment a turn actually streams: the cap would
count nothing while reading, in review, exactly as it does now.

### 2. Refused at the top of the handler with 429 — not on the stream

D-166 moved admission *inside* the stream because a **wait** was invisible: up to
`service_turn_admission_timeout_seconds` with no response at all, which is the one thing a busy
front door and a dead one must not have in common. This is not a wait. It is decided by a dict scan,
it is final for this request, and a retry a millisecond later gets the same answer — so it gets a
status code, like the durable 409 the same route raises before hand-off and like `streams.py`'s own
per-user 429.

Answering with `at_capacity` on the open stream would also name the wrong full resource: the replica
may be nearly idle, and the caller's own turns are the limit. Its `retryable=True` would tell a
retrying UI to keep hammering a condition only that client can clear.

**`Retry-After` *is* sent, and reading the client is what decided it.** On the server's own terms
the honest answer is no number — this process cannot predict when one of the caller's turns ends —
and that is what this refusal shipped with until `Chemclaw3_ui` was read. Its `errorFromStatus`
splits 429 on the **presence** of the header: with one it renders a transient `rate_limited` banner
with a countdown; without one it renders `budget_exhausted`, *"The usage budget for this service is
exhausted"*, which locks the composer and which that module's own comment says nothing in the UI
clears. Both sentences are false here — the cap lifts when one of the caller's own turns ends — so
the purist answer produced a permanent, incorrect lockout for a transient, correct refusal. A
machine-readable `code` is not an alternative: `streamTurn.ts` passes `errorFromStatus` four
arguments and the discriminator is its fifth, so the code is dropped before anything reads it, and
the header is the only channel that reaches a client already deployed. The value is
`service_turn_admission_timeout_seconds` — already this system's answer to how long waiting for a
turn permit is reasonable — rather than a number invented at the call site; a client retrying into
a still-full cap gets the same hint again, exactly as the token-bucket limiter's 429 behaves.

Refusing at the top also skips work a stream-level refusal has already paid for: by
`semaphore.acquire()` the request has written a session title, taken the durable claim, started the
lease clock and spawned a pump. The refusal path is the one an abusive or looping client drives
hardest.

**The check sits above `_claim_turn_slot`, and the line order is the guard.** That claim takes a
reservation with `deadline=math.inf` until `_start_turn_lease` starts its clock, so a raise between
it and the `try` leaks the session's slot with no expiry — 409-bricking that session for the pod's
lifetime. `besides=session_id` excludes the caller's own session, so a double-submit to a running
session still answers the 409 that names what happened rather than a 429 whose code would depend on
how many tabs the chemist has open. It cannot be used to exceed the cap: a 409 creates no lease.

### 3. The slot is **not** released at a detach — deliberately the opposite of the permit

`detach.py` returns the permit when a client hangs up, on the argument that *"admission is fairness
to a waiting client, and a detached turn has none."* That argument does not transfer, and the two
guards answering one event in opposite directions is the design.

The permit rations concurrent demand on the shared model endpoint, and that queue should not be held
for a reader who left. The per-actor slot rations **one principal's share of this replica**, and a
detached turn is still spending it — CPU, model tokens and a store connection — until the loop cap
or `service_turn_timeout_seconds` stops it. `D-2026-09-05-a-lease-is-demand-and-a-permit-is-occupancy`
already says so for the neighbouring gauge.

More sharply: releasing here would hand the cap straight back to the attack it exists to stop. Each
POST-and-hang-up would free both the permit and the actor's slot while leaving a pump running for
the full turn timeout, so the sequence would be unbounded again and the cap would bind only on
well-behaved clients — the population that was never the problem. Deriving from the lease makes this
structural: there is no detach hook to add and no second ending to make idempotent.
`tests/test_detach.py::test_a_detached_turn_still_holds_its_actors_slot` is what goes red if a later
change "tidies up" the inconsistency.

### Off in code, on in the chart

0 disables. The code default is not doctrine here but a measurement: `chemclaw.cli.live_storm`
drives tens of concurrent turns **from one credential**, and its family A exists to measure this
very cap's shedding curve. An on-by-default per-actor cap converts those sheds into 429s and breaks
the one instrument that validates admission control. Same split `budget_enabled` and
`service_rate_limit_per_minute` already take (D-142/REV-16).

The chart sets 4 against 12: one actor takes at most a third of a replica, three distinct chemists
are needed to fill a pod, and 4 is already more parallelism than the shipped UI generates by hand
(the browser holds one turn per open session view, and the per-user stream cap is 5). No startup
validator — a `per_actor ≥ pod_cap` configuration enforces nothing but is not an outage, so
`tests/test_deploy_chart.py` holds the inequality where the posture lives.

### The metric carries no identity

`chemclaw_turns_refused_actor_cap_total` is unlabelled, and `actor`/`oid` is the label that must
never be added. `/metrics` is unauthenticated; an `oid` is an unbounded **caller-chosen** key, and
minting them is precisely how one routes around a per-principal limit, so a labelled series would
stop counting at the cardinality cap exactly when it mattered (D-152). The identity is in the
WARNING beside the increment. It is not folded into `chemclaw_turns_conflict_total{scope=…}`: that
counter's population is 409 conflicts on one session whose remedy is to wait for your own turn, and
this one's is a 429 across sessions — two populations under one denominator nobody can interpret.

## Consequences

**Measured before and after, on the real app through its own ASGI stack.** With the route guard
disabled and the cap configured at 2, alice's third concurrent turn was **admitted and streaming**
(`active_turns=3`). With the guard restored, the same request answered **HTTP 429**. The
before-figure is the behaviour every deployment runs today.

**An actor spread over the fleet still holds the multiple, and that is not papered over.** Per
process, like `service_max_concurrent_turns`, so at `maxReplicas: 6` one principal can hold 6 × 4 =
24 concurrent turns across the fleet. That is strictly better than 6 × 12 = 72 and strictly worse
than a real per-actor guarantee. The honest place for the fleet-wide form is the ingress, which is
what SCALE-1 said and what this record does not disturb.

**A chemist who closes four tabs cannot start a fifth turn until those four finish or time out.**
That is the cost of §3 and it is the same cost `detach.py` already accepted and priced one layer
down, bounded by the same two numbers.

**Nothing in this change needs Postgres, and that absence is the design.** The cap is per-process, so
there is no cross-replica behaviour an integration test could reach; a Postgres test here would be
testing a property this decision explicitly declines to have.

**Revisit when:** the front door sits behind an ingress that can key a limit on the authenticated
`oid` — a Route or Gateway policy, which would land in
`deploy/helm/chemclaw/templates/service-route.yaml` beside the existing `route.ipWhitelist` — or
when a shed-storm review attributes `chemclaw_turns_shed_total` to one actor spread across replicas.
That attribution is only possible from the per-request logs, which carry the actor;
`chemclaw_turns_refused_actor_cap_total` rising while sheds continue is the scrape-side signal that
the per-process half is binding and the fleet-wide half is missing.

## What keeps it true

- `tests/test_turn_fairness.py::test_an_actor_at_the_cap_is_refused_while_another_actor_is_admitted`
  — both halves: the refusal, and a second actor served in the same breath.
- `tests/test_turn_fairness.py::test_a_finished_turn_frees_the_actors_slot` — the release, and that
  `_start_turn_lease` carried `actor` across its restamp.
- `tests/test_turn_fairness.py::test_a_double_submit_to_one_session_is_still_409_not_429` — `besides=`.
- `tests/test_turn_fairness.py::test_the_actor_cap_is_off_in_code` and
  `test_one_actor_can_still_fill_the_pod_when_the_cap_is_off` — the guard is inert at the default
  rather than merely lenient, so `live_storm`'s offered-load sweep still measures what it says.
- `tests/test_turn_fairness.py::test_the_refusal_counter_refuses_an_identity_label` — the one label
  that must never exist.
- `tests/test_turn_fairness.py::test_the_refusal_carries_retry_after_because_the_client_splits_429_on_it`
  — the header, and that its value is the configured admission timeout rather than a literal.
- `tests/test_detach.py::test_a_detached_turn_still_holds_its_actors_slot` — §3, in both directions:
  the permit came back and the slot did not.
- `tests/test_stream_contract.py::test_an_expired_lease_does_not_hold_an_actors_slot`,
  `::test_a_turns_own_session_is_not_counted_against_its_actor`,
  `::test_a_maintenance_hold_is_not_a_turn` — §1's expiry, `besides=`, and `actor=None`.
- `tests/test_deploy_chart.py::test_the_chart_caps_turns_per_actor_strictly_below_the_process_cap` —
  the posture, and that it is strictly below the pod cap rather than decorative.
