# D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel — three of four declared delivery kinds had no producer, so a report, a finished job and an open question could only be told to somebody already watching

## Status

Accepted.

## Context

`deliver/message.py` bounds `Message.kind` to four values — `digest`, `awaiting`, `job-result`,
`report` — and it is a `Literal` rather than a documented convention for a reason that module states
at length: `FileDeliveryDriver` builds a filename out of it, so an absolute or `../`-bearing value
escapes the outbox entirely with `mkdir(parents=True)` creating whatever it traverses to.

The 2026-09-13 capability audit measured what actually constructed one. **One of the four, in one
place**: `durable/digest.py`'s nightly subscription digest. The other three were a vocabulary.

That is not a cosmetic gap, because of what the three name. Each is the only notice its workflow
could send to somebody who is *not* sitting in a session:

- `awaiting` — a question holding a durable wait open. `AwaitingWorkflow._push` writes to
  `session_events` and its own docstring says the interesting half out loud: *"A wait with no
  session is the ordinary case, not an edge one... what is skipped is a notification with no
  addressee."* The premise is right and the conclusion was too strong: `asked_of` **is** an
  addressee, and a person who is not in a session is exactly the person a question has to travel to
  reach.
- `job-result` — a durable job finishing. A CREST search or a BO round completes hours after the
  chemist stopped watching, and `job.session_id` is empty outright for a run a Schedule or an inbox
  started.
- `report` — the one durable job whose product is a document somebody asked for by name.

So the system could tell a chemist about work it did for them only while they were watching it
happen, which is the state the digest seam was built to end one layer over.

## Decision

**One outbound seam, four producers.** `durable/deliver_message.py` holds
`deliver_message_activity` (the I/O) and `deliver_best_effort` (the workflow-side wrapper), shaped
deliberately like `durable/notify.py`'s session push-back, which is its sibling and not its
alternative: the mailbox is the durable handover and the channel is the courtesy on top. Each of
`AwaitingWorkflow._push`, `ConnectorJobWorkflow._finish`, `ReportWorkflow.run` and `DigestWorkflow`
now constructs one `OutboundMessage`.

`durable/digest.py::deliver_digest_activity` is **gone**, generalised rather than copied. Its two
hard-won properties are what the new module is built around:

1. **The enablement check runs inside the activity.** `delivery_enabled()` reads `settings`, and a
   workflow that branched on it decided whether to emit a command at all — so enabling a channel and
   restarting a worker made an in-flight run replay a command its history does not contain.
2. **The `Message` is constructed inside the activity**, which is why `OutboundMessage` exists at all
   and is deliberately looser: plain `str` fields, no `min_length`, and `kind` is not the `Literal`.
   `Message`'s constraints have to fail where something can catch them. A workflow that built a
   `Message` with an empty `recipient` raises `ValidationError` in *workflow* code, which no
   best-effort wrapper can catch — it guards the activity, not the argument — so the notice that must
   never fail the job becomes the thing that fails it. That inversion is on record twice already:
   `AwaitingWorkflow._push` carries a guard for it (measured: every sessionless wait failed before
   it) and the digest's activity carried a comment for it (an empty subscription owner failing the
   activity non-retryably and aborting every subscriber after it).

The `Literal` therefore stays the bound where the bound matters, and `OutboundMessage` is the wire.
A bad value crossing it is a caught `ValidationError` and a counted degradation rather than a
workflow task that retries forever.

The degraded subsystem label follows the seam: `digest_delivery` → `message_delivery`.

## Consequences

- **Nothing changes in a shipped deployment.** `delivery_enabled()` is false until
  `CHEMCLAW_DELIVERY_CHANNELS` names a channel, and the shipped value is empty. This makes the
  capability real for a deployment that has opted in; it does not opt anybody in.
- **`awaiting` goes out on every reminder, not only on the opening notice.** That repetition *is* the
  escalation property the module's docstring describes, and `payload["reminders"]` is what lets a
  reader tell the fourth from the first.
- **A duplicate is now a duplicated ticket rather than a duplicated digest.** `deliver/driver.py`
  already said this in the future tense while the three kinds had no producer; delivery is
  at-least-once by construction (`BAD_DATA_RETRY` re-runs an activity after a landed POST), which is
  why the webhook payload carries a dedup handle. That was built for this and is now load-bearing.
- **An entitlement string may reach a driver as a recipient.** `asked_of` is documented as "an actor
  id or an entitlement, or '' for anyone entitled", and `Message` states that resolving a recipient
  to an address is the driver's job. Empty is skipped before the activity is even scheduled; a
  driver that cannot resolve an entitlement reports it on `chemclaw_delivery_failures_total` like any
  other undeliverable address.

## What keeps it true

- `tests/test_outbound_delivery.py::test_every_declared_delivery_kind_has_a_producer` — the finding
  as an assertion, in both directions, AST-scanned over `src/`. It is an *absence* test in the shape
  `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` established: three producers
  existing today is not the guarantee, because what shipped was a `Literal` nobody was obliged to
  satisfy.
- `tests/test_outbound_delivery.py::test_each_kind_is_produced_by_the_workflow_that_owns_that_event`
  — named, because the kinds are not interchangeable.
- `tests/test_outbound_delivery.py::test_a_kind_outside_the_vocabulary_never_reaches_the_outbox` —
  that the looser wire model did not loosen the path bound.
- `tests/test_outbound_delivery.py::test_an_unaddressable_message_is_counted_rather_than_raised`
  and `::test_a_message_with_no_addressee_never_schedules_the_activity`.
- `tests/test_degraded.py::_EXPECTED_SUBSYSTEMS` — `message_delivery` stays enumerable.
