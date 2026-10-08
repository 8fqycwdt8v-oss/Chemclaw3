# `schema/api/` — the API contract the UI is generated from

`openapi.json` is the OpenAPI 3.1 document `create_app()` serves at `/openapi.json`, committed so a
change to the HTTP surface or the turn events shows up as a diff in review. Core owns it; the UI
(`Chemclaw3_ui`) generates its types from a pinned copy and does not hand-mirror them.

```
make openapi            # regenerate; offline, builds the app in-process and starts nothing
```

`tests/test_openapi_contract.py` fails when the file differs from what the app would serve, and says
which command to run. Output is sorted and indented, so two runs produce the same bytes.

## What it contains

- Every route, with its request and response model under `components.schemas`.
- **The turn events.** `TurnEvent` is a `oneOf` of 21 models with `discriminator.propertyName:
  type`; each `text/event-stream` response (`POST /sessions/{id}/messages`, `GET
  /sessions/{id}/turn/stream`, `GET /sessions/{id}/events`) points at it. A frame's SSE `event:`
  name equals the body's `type`. Fields with a default are optional in the schema: a reader must
  tolerate their absence in an older stored event, though the current serialiser always sends them.
- `ProtocolReadout`, `ProtocolReceipt`, `ArmRow`: the payload of the protocol read tool, rendered by
  the UI from a stored tool result rather than returned by a route.

## Version

`API_CONTRACT_VERSION` in `src/chemclaw/api/contract.py` is the only place the version is written;
it becomes `info.version`. Bump it in the same commit as the regenerated file.

| Change | Bump |
| --- | --- |
| A route, field, event kind or enum member removed or renamed; a field's type narrowed or changed; a field newly required | major |
| A route, field, event kind or enum member added; a field loosened to optional | minor |
| Descriptions or examples only | patch |

A major bump is a coordinated release with the UI. The order that never breaks a live browser for an
added field is: UI reader first, then core; for a removal, core stops sending last.

## What this does not pin

The bytes of each event on the wire are pinned separately by `tests/test_event_wire_golden.py`
against `tests/fixtures/turn_event_frames.json`, which has no regeneration switch: a wire change is
edited into the fixture by hand, with the bump.
