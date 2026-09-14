# D-2026-09-14-the-reader-lands-first-and-the-name-follows — `note_proposed` becomes `note_recorded`, in the only order a deployment survives

## Status

Accepted. Second of three steps; the third belongs to `Chemclaw3_ui`.

## Context

`NoteProposedEvent` announced that a note reached the knowledge graph, under the wire name
`note_proposed`. There has been nothing to propose to since
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` removed the gate and the proposal queue
behind it — the note is written, and what a chemist actually reads has said "recorded" the whole
time. The event's own docstring said all of this and kept the literal, because a discriminator two
repositories switch on is a wire contract and not prose.

**The ordering is the whole decision, and there is only one that has no broken state.**

- *Service first.* Every browser holding a bundle that has not been redeployed receives an event
  whose `type` its normaliser does not know. That normaliser rebuilds each event field by field, so
  the event is not merely untyped — it is dropped. That is exactly the failure `shared/events.ts`
  has cost six times.
- *Reader first.* A client that accepts both names is correct against a service sending either, so
  the skew window has no failure in it at all.

`Chemclaw3_ui` shipped the tolerant reader in its Wave 30 merge (`c646469`): `shared/events.ts`
accepts both names, keeps `'note_proposed'` as its *internal* type deliberately (a local rename is
a different day's change and would put a second change in the skew window), and records the third
step in its own `ISSUES.md` with `note_recorded` in `tests/backendContract.test.ts`'s
`AHEAD_OF_BACKEND` map — so that run reports the day this service declares the new name, which is
the day step three is unblocked.

## Decision

This is step two. `NoteProposedEvent` becomes `NoteRecordedEvent` and its `type` literal becomes
`note_recorded`. The fixture is regenerated in the same commit, which is what makes this a
*declaration* the UI's contract check can read (`/openapi.json` carries the union since
`D-2026-09-14-a-contract-the-client-cannot-read-is-a-contract-one-side-remembers`).

`evals/live.py` is this repository's own reader and accepts **both** names for the same window,
with the reason at the call site: a probe run against a service one deploy behind would otherwise
score a knowledge write as not having happened, which reads as a retrieval regression rather than
as a skew.

**There is a third consumer and this ADR's first draft did not count it.** `api/static/app.js`, the
bundled dev page, switches on the same discriminator, and `tests/test_dev_page_events.py` caught it
in both directions the moment the rename landed — `note_recorded` unrendered, and `note_proposed`
handled by a page no turn can reach. It takes the new name **outright rather than both**, and the
difference from `Chemclaw3_ui` is the whole argument in miniature: that page is served by this
process, so it cannot be older than the service sending the event. A second name there would be
tolerance for a skew that cannot happen, and the same test would refuse it as an event no turn
emits. So "reader first" is a claim about *independently deployed* readers, not about every reader.

**Step three is not this repository's**, and nothing here can check it. Removing `note_proposed`
from the UI's reader, and then from `evals/live.py`, is the third release; the UI's `ISSUES.md`
carries who does the first half and `docs/planning/DEFERRED.md` carries the second.

## Consequences

For one deployment cycle both names are live on the wire in the sense that matters: the service
sends one and every reader accepts both. Nothing drops an event at any point in the sequence.

## What keeps it true

- `tests/test_event_contract.py::test_the_wire_contract_matches_what_the_other_repositories_mirror`
  — the fixture and the models agree, so the rename could not land in one and not the other.
- `tests/test_event_contract.py::test_the_published_document_declares_every_event_this_service_streams`
  — `note_recorded` reaches `/openapi.json`, which is what the client's own check reads.
- `tests/test_turn_signals.py::test_a_recorded_note_becomes_a_note_recorded_event` — the producer
  emits it.
