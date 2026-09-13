# D-2026-09-13-a-collapse-without-the-batch-keeps-the-oldest-frame — a reduction over a claim is the tailer's argument, not the consumer's loop

`D-2026-09-05-a-push-nobody-claims-is-not-a-push` widened `GET /sessions/{id}/events` to claim
`awaiting-answer` rows, and found that widening the claim does not deliver *the* notification — it
delivers the whole history, because `durable/retention.py` prunes `session_events` only
`WHERE consumed_at IS NOT NULL` and `retention_session_events_days` defaults to 0. Measured there on
one BO campaign opened, chased daily and expired a month ago: **sixteen frames on a single poll**,
fifteen of them `waiting` for a question that is closed. The collapse it added is right. Which frame
it keeps was wrong, and its own comment said so.

## What was measured

The route suppresses an `awaiting-answer` row whose `(request_id, state)` it has already reported on
this connection. That decides one row at a time, and the rows arrive oldest-first, so the surviving
frame is the **first** of each run. Driven against the production tailer with the backlog that ADR
measured — one open, fourteen chases, an expiry:

    reminders on the frames the client received:   [0, 14]
    reminders the client needed:                   [14, 14]

The `waiting` frame the chemist is shown says the question has been chased zero times, a month after
it was opened and fourteen chases later. The rows are consumed on that first claim, so nothing
corrects it on this channel; `GET /pending` answers correctly, which is why this is a wrong number
rather than a lost one.

## The decision

**The reduction happens where the batch exists, which is inside one claim.**
`agent/session_events.stream_new_events` gains a `collapse` argument —
`Callable[[list[SessionEvent]], list[SessionEvent]]` — applied to each claim before any of its rows
is yielded. The route passes `_newest_per_state`.

The alternative the backlog row named first was a **batched yield**: have the tailer yield
`list[SessionEvent]` and let the consumer reduce. That is the cleaner shape and it is declined on a
guarantee. The tailer's `finally` restores an event the consumer never received
(`restore_unconsumed`, COR-4) — with a batch, a client dropping after the third of sixteen rows would
leave thirteen consumed and undelivered, where today each is restored. A reduction argument keeps
row-by-row delivery and its restore semantics exactly, and the only rows that go unrestored are the
ones the caller has declared redundant. It also leaves the eleven fakes that patch this seam
untouched, which is a convenience rather than the reason.

**The surviving row is the last occurrence of a key, emitted at the first occurrence's position**,
and each half of that is a decision:

- *The last object*, not the last payload grafted onto the first row, so `payload` and `event_id`
  stay consistent. A consumer that drops mid-stream then restores the **newest** row rather than a
  stale one whose successors have already been consumed.
- *The first position*, so two different requests keep the order their rows arrived in. Nothing about
  one request's state is news about another's, and a reduction that reordered them would make
  `test_pushback_does_not_collapse_two_different_requests` assert an order that came out of this
  function rather than off the wire.

Rows of every other kind pass through untouched and in place. A `job_completed` and a `job_failed`
are distinct facts about distinct jobs, and folding them is what this must not do.

**The per-connection suppression stays.** The two compose and neither subsumes the other: `collapse`
reduces within one claim, and `awaiting_reported` is what stops a *second* poll re-reporting a state
that has not changed. Its comment, which said the surviving frame was the oldest and that fixing it
needed a boundary the tailer does not expose, is replaced by one that says where the boundary now is.

## What keeps it true

| property | test |
| --- | --- |
| the surviving frame of a collapsed run is the newest — `[14, 14]`, not `[0, 14]` — driven through the **real** tailer with only its claim faked | `tests/test_service.py::test_pushback_reports_the_newest_state_of_a_collapsed_backlog` |
| a month of reminders still reaches the browser as one open notice and one expiry | `tests/test_service.py::test_pushback_collapses_a_replayed_backlog_of_reminders` |
| the collapse is per request: two open questions are two notices, in arrival order | `tests/test_service.py::test_pushback_does_not_collapse_two_different_requests` |
| the expiry push, which carries fewer fields, still survives | `tests/test_service.py::test_pushback_streams_an_expired_question` |
| a dropped consumer still restores the row it did not receive | `tests/test_stream_contract.py`, `tests/test_disconnect_teardown.py` |

The new test drives `stream_new_events` itself and **reads the `collapse` argument out of the route's
own call** rather than supplying one: a test that passed `_newest_per_state` in by hand would be
green against a route that had stopped handing it over, which is the vacuous shape `tasks/lessons.md`
records. It asserts the argument arrived, and then asserts the reminders.

Three mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| the route stops passing `collapse` (the pre-fix state) | red, on the named assertion: *"the route no longer hands the tailer a batch reduction"* |
| the reducer keeps the first occurrence's object instead of the last | red — `[0, 14]`, which is the defect reproduced exactly |
| the tailer ignores `collapse` and yields the raw batch | red |
