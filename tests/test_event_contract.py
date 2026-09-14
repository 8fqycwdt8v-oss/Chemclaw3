"""The turn-event contract cannot change silently — because another repository mirrors it by hand.

`api/events.py` is a contract two surfaces read: this service produces it and `Chemclaw3_ui`
renders it. (`Chemclaw3_mock` stands in for external MCP tools, not for this front door — this
docstring and the failure message below claimed it held "its copy of the same contract" until
2026-08-27, when a grep of that tree found **zero** occurrences of any event name. A mirror that
does not exist cannot be updated, and telling someone to update it sends them searching another
repository for a file that was never there.) Nothing mechanical connects the mirrors that do
exist. The consequence is
recorded at length in the UI's own `shared/events.ts`, which has now been wrong **nine times** — six
missing *members* (`capability_degraded`, `tool_failed`, `job_failed`, `evidence_source`, `handoff`,
and one more before them) and three missing *fields* (`plan.plan_hash`, `tool_failed.reason`,
`evidence_source.failed`). Its normaliser rebuilds every event field by field, so an unmirrored
field is not merely untyped over there — it is **deleted in transit**, and the consumer receives a
well-formed event with the qualifying half removed.

Every one of those nine was added here, on a green build. That is the root cause: this side can
change the contract and stay green, so the only thing standing between a new field and a surface
that silently drops it is whether the author remembered two repositories they were not editing.

This is the tripwire. It does not — cannot — check the other repositories; it makes the *moment of
change* loud on the side where the change happens, and names what has to follow. A golden file
rather than a rule about field names, because the failure has never been a malformed contract; it
has been a correct one that nobody propagated.

**A diff here is not a problem to suppress.** It means the wire format changed, which is a
deliberate act, and the fixture is updated in the same commit that makes it — with the mirrors.
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

    Rendered rather than schema-dumped: `model_json_schema()` is precise and its *output* is a
    pydantic implementation detail, so a library bump would rewrite this file and teach everyone to
    regenerate it without reading the diff — which is the one thing this fixture must not become.
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

    Cheap, and it is the one way the fixture above could be wrong while looking right: it is keyed
    by discriminator, so a duplicate would silently collapse two members into one entry and the
    contract would record a surface smaller than the one that ships.
    """
    discriminators = [model.model_fields["type"].default for model in typing.get_args(Event)]
    assert len(discriminators) == len(set(discriminators)), (
        f"two events share a `type` discriminator: {sorted(discriminators)}"
    )


def test_the_published_document_declares_every_event_this_service_streams() -> None:
    """The fixture holds this side to its models; the document is what the other side can read.

    Two halves of one contract, and only the first existed. The fixture beside this file makes a
    change to `api/events.py` loud **here** — which is the right tripwire and cannot help
    anybody else, because a golden file in this repository is not an artefact another repository
    fetches. What `Chemclaw3_ui` fetches is `/openapi.json`, and measured on 2026-09-14 that
    document declared **2 of 17** members and **0 of 10** error codes: an SSE body is
    `text/event-stream`, which FastAPI cannot infer from a return annotation, so the union never
    reached `components.schemas` at all
    (`D-2026-09-14-a-contract-the-client-cannot-read-is-a-contract-one-side-remembers`).

    Asserted against the **fixture** rather than against `api/events.py` directly, deliberately:
    the fixture is already held to the models by the test above, so going through it means one
    number governs both halves and a new member cannot satisfy one check by failing to reach the
    other.

    Driven off the real `create_app()` document rather than off `event_schemas()`, because the
    subject is the merge — a component builder that is correct and never called is exactly what a
    schema-side assertion would pass.
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
    """A component nothing references is an orphan a generator will not emit a type for.

    Merging the union into `components.schemas` makes it *present*; the `$ref` on each streaming
    route's `text/event-stream` response is what makes a client generator bind it to the route.
    Both routes, because they stream the same union and only one of them carries a turn — a
    push-back stream a client renders with a different type is the same hand-mirroring this whole
    contract exists to end.
    """
    document = _published_document()
    streams = {
        f"{method} {path}": operation
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
        if "text/event-stream" in (operation.get("responses", {}).get("200", {}).get("content", {}))
    }
    assert len(streams) == 2, (
        f"expected the two SSE routes to declare an event-stream response; found {sorted(streams)}"
    )
    for name, operation in streams.items():
        schema = operation["responses"]["200"]["content"]["text/event-stream"]["schema"]
        assert schema.get("$ref") == TURN_EVENT_REF, (
            f"{name} streams turn events and its 200 response points at {schema} instead of "
            f"{TURN_EVENT_REF}, so a generated client binds no type to it"
        )


def _published_document() -> dict[str, typing.Any]:
    """The OpenAPI document `create_app()` actually serves, built once per call.

    The two startup refusals are satisfied rather than patched out: a loopback bind and an
    explicit loopback-gateway acknowledgement are the same two statements `make chat` makes, and
    turning either guard off to read a document would be testing a configuration this service
    refuses to run in.
    """
    from chemclaw.api.app import create_app
    from chemclaw.core.config import settings

    previous = (settings.service_host, settings.llm_allow_loopback_gateway)
    settings.service_host, settings.llm_allow_loopback_gateway = "127.0.0.1", True
    try:
        return dict(create_app().openapi())
    finally:
        settings.service_host, settings.llm_allow_loopback_gateway = previous
