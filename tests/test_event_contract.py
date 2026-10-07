"""The turn-event contract cannot change silently, because another repository mirrors it by hand.

`api/events.py` is produced here and rendered by `Chemclaw3_ui`, whose normaliser rebuilds every
event field by field, so an unmirrored field is deleted in transit. This side can change the
contract and stay green, so this golden file makes the moment of change loud where it happens.

A diff here is not a problem to suppress: the wire format changed, and the fixture is updated in
the same commit, with the mirrors.
"""

import json
import os
import typing
from pathlib import Path

from chemclaw.api.events import TURN_EVENT_REF, ErrorCode, Event

_FIXTURE = Path(__file__).parent / "fixtures" / "turn_events_contract.json"

# Set to regenerate the fixture instead of asserting against it. Deliberately an environment
# variable rather than a CLI: regenerating is something you do while looking at a failure, not an
# operation this service offers anybody.
_UPDATE = "CHEMCLAW_UPDATE_EVENT_CONTRACT"


def _render(annotation: object) -> str:
    """One field's type, as a short stable string.

    Rendered rather than schema-dumped, so a pydantic bump does not rewrite the fixture and teach
    everyone to regenerate it without reading the diff.
    """
    if isinstance(annotation, type):
        return annotation.__name__
    text = str(annotation)
    for prefix in ("typing.", "chemclaw.api.events."):
        text = text.replace(prefix, "")
    return text


def _contract() -> dict[str, object]:
    """The wire contract as data: every member, its fields and their types, plus the error codes."""
    members: dict[str, dict[str, str]] = {}
    for model in typing.get_args(Event):
        fields = model.model_fields
        discriminator = fields["type"].default
        members[discriminator] = {
            name: _render(field.annotation) for name, field in fields.items() if name != "type"
        }
    return {
        "members": dict(sorted(members.items())),
        "error_codes": sorted(typing.get_args(ErrorCode)),
    }


def test_the_wire_contract_matches_what_the_other_repositories_mirror() -> None:
    """Fail on any change to the event union, naming what else has to change with it."""
    current = _contract()
    if os.environ.get(_UPDATE):
        _FIXTURE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    recorded = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert current == recorded, (
        "the turn-event contract changed.\n\n"
        "This shape is mirrored by hand in Chemclaw3_ui's shared/events.ts — the interface AND "
        "normalizeEvent, which rebuilds every event field by field — so a field that does not "
        "reach it is DROPPED in transit rather than merely ignored.\n\n"
        f"Update the mirror, then regenerate this fixture:\n    {_UPDATE}=1 pytest {__file__}\n"
    )


def test_every_member_is_reachable_from_the_union_by_its_discriminator() -> None:
    """No two members share a `type`, or a consumer switching on it would be ambiguous.

    The fixture is keyed by discriminator, so a duplicate would silently collapse two members.
    """
    discriminators = [model.model_fields["type"].default for model in typing.get_args(Event)]
    assert len(discriminators) == len(set(discriminators)), (
        f"two events share a `type` discriminator: {sorted(discriminators)}"
    )


def test_the_published_document_declares_every_event_this_service_streams() -> None:
    """The published OpenAPI document declares every event this service streams.

    The fixture makes a change loud here; `/openapi.json` is what the UI fetches. An SSE body is
    `text/event-stream`, which FastAPI cannot infer, so the union is merged into
    `components.schemas` explicitly. Asserted against the fixture so one list governs both halves,
    and driven off the real `create_app()` document because the subject is the merge.
    """
    document = json.dumps(_published_document())
    contract = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    missing_members = [name for name in contract["members"] if f'"{name}"' not in document]
    assert not missing_members, (
        f"the published OpenAPI document declares no {missing_members} — the events this service "
        "streams are absent from the one artefact the client's contract check can fetch, which is "
        "how `shared/events.ts` came to be wrong nine times"
    )
    missing_codes = [code for code in contract["error_codes"] if f'"{code}"' not in document]
    assert not missing_codes, (
        f"the published OpenAPI document declares no error code(s) {missing_codes}; a client "
        "switching on `error.code` has no way to know the set is closed"
    )


def test_both_streaming_routes_point_at_the_union_they_stream() -> None:
    """Every streaming route's response points at the union it streams.

    The `$ref` on each `text/event-stream` response is what binds the component to the route for a
    client generator. The turn, a participant following it, and the push-back stream all stream the
    same union.
    """
    document = _published_document()
    streams = {
        f"{method} {path}": operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
        if "text/event-stream" in (operation.get("responses", {}).get("200", {}).get("content", {}))
    }
    assert sorted(streams) == [
        "get /sessions/{session_id}/events",
        "get /sessions/{session_id}/turn/stream",
        "post /sessions/{session_id}/messages",
    ], f"expected the three SSE routes to declare an event-stream response; found {sorted(streams)}"
    for name, operation in streams.items():
        schema = operation["responses"]["200"]["content"]["text/event-stream"]["schema"]
        assert schema.get("$ref") == TURN_EVENT_REF, (
            f"{name} streams turn events and its 200 response points at {schema} instead of "
            f"{TURN_EVENT_REF}, so a generated client binds no type to it"
        )


def _published_document() -> dict[str, typing.Any]:
    """The OpenAPI document `create_app()` actually serves, built once per call.

    The startup refusals are satisfied (loopback bind, loopback-gateway acknowledgement, as `make
    chat` does) rather than patched out.
    """
    from chemclaw.api.app import create_app
    from chemclaw.core.config import settings

    previous = (settings.service_host, settings.llm_allow_loopback_gateway)
    settings.service_host, settings.llm_allow_loopback_gateway = "127.0.0.1", True
    try:
        return dict(create_app().openapi())
    finally:
        settings.service_host, settings.llm_allow_loopback_gateway = previous
