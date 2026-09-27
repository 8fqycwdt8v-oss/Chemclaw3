# D-2026-09-14-a-contract-the-client-cannot-read-is-a-contract-one-side-remembers — the turn-event union reaches the OpenAPI document

## Status

Accepted. Closes the `BACKLOG.md` row "nothing checks the client half of a wire contract, and it
has drifted twice", for this repository's half of it.

## Context

`tests/fixtures/turn_events_contract.json` pins the `Event` union against the models in
`api/events.py`, so a change to the wire format fails a build **here**. That is the right tripwire
and it is the only one: `Chemclaw3_ui/shared/events.ts` mirrors the same union by hand and has been
wrong **nine times** — six missing members, three missing fields — and its normaliser rebuilds each
event field by field, so an unmirrored field is not merely untyped over there, it is *deleted in
transit*.

`D-2026-09-04-a-contract-has-two-halves-and-a-server-test-sees-one` said this needs "one artefact
both sides read". One already exists: `D-2026-09-07-a-contract-check-that-cannot-reach-the-contract`
put `/openapi.json` behind the gate precisely so `Chemclaw3_ui/scripts/check-openapi.mjs` could
fetch it.

**Measured on 2026-09-14, that artefact declares almost none of this contract.** Against the
fixture's 17 members and 10 error codes, the served document named **2 members** (`plan` and
`answer`, and only because a non-streaming route happens to reference their models) and **0 error
codes**. The reason is mechanical: both SSE routes return `text/event-stream`, a body FastAPI
cannot infer from a return annotation, so the union never reached `components.schemas` at all. The
client's contract check was fetching a document that was silent about the thing it needed to check.

## Decision

**What the backend declares is published where the client reads it, and held to the fixture.**

1. `api/events.py::event_schemas()` renders the union with `TypeAdapter(Event).json_schema(...)` —
   19 components, `ref_template` pointing into `components/schemas` so every `$ref` resolves in the
   merged document. `TypeAdapter` rather than a hand-built `oneOf` because the union is already
   discriminated on `type` and pydantic emits the `discriminator` mapping a generator needs.
2. `create_app` wraps `app.openapi` to merge those components in. Wrapped rather than merged in the
   `/openapi.json` handler because FastAPI caches the generated document on `app.openapi_schema`:
   a handler-side merge would run per request against an already-frozen dict.
3. Both streaming routes declare `TURN_EVENT_REF` in their 200 `text/event-stream` response.
   Presence alone would leave the union an orphan component; the `$ref` is what binds it to a
   route for a client generator. Both routes, because the push-back stream carries the same union
   and a client rendering it with a different type is the hand-mirroring this is ending.

**The fixture stays the basis of the assertion, and that is the load-bearing choice.** The new test
reads the fixture — which the existing test already holds against the models — rather than
`api/events.py` directly, so one number governs both halves and a new member cannot satisfy one
check while failing to reach the other.

**What this does not do.** It does not check `Chemclaw3_ui`. Nothing in this repository can, and
pretending otherwise is the failure `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it`
is about. What it does is give the client's own check something true to read, which is the half
that was missing.

## Consequences

`components.schemas` grows from 54 to 73. A generated TypeScript client now has a `TurnEvent` union
with a discriminator, so the next member added here appears in a generated type rather than in a
hand-written file somebody has to remember.

## What keeps it true

- `tests/test_event_contract.py::test_the_published_document_declares_every_event_this_service_streams`
  — every fixture member and error code appears in the document `create_app()` actually serves.
  Mutation: not installing the merge wrapper fails it with all 15 members named.
- `tests/test_event_contract.py::test_both_streaming_routes_point_at_the_union_they_stream` —
  exactly two routes declare an event-stream 200 and both `$ref` the union. Mutation: dropping the
  `$ref` from one route fails it.
- `tests/test_event_contract.py::test_the_wire_contract_matches_what_the_other_repositories_mirror`
  — unchanged, and still the thing the new test derives its expectation from.
