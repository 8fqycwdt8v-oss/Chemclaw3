"""The turn-event contract cannot change silently — because two other repositories mirror it.

`api/events.py` is a contract three surfaces read: this service produces it, `Chemclaw3_ui` renders
it, and `Chemclaw3_mock` stands in for it. Nothing mechanical connects them. The consequence is
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

from chemclaw.api.events import ErrorCode, Event

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
        "This shape is mirrored by hand in two other repositories, and a field that does not "
        "reach them is DROPPED by their normalisers rather than merely ignored:\n"
        "  - Chemclaw3_ui  -> shared/events.ts (the interface AND normalizeEvent, which rebuilds "
        "every event field by field)\n"
        "  - Chemclaw3_mock -> its copy of the same contract\n\n"
        f"Update both, then regenerate this fixture:\n    {_UPDATE}=1 pytest {__file__}\n"
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
