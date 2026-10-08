"""`schema/api/openapi.json` is the published API contract, and this file keeps it true.

`Chemclaw3_ui` generates its types from the committed document, so it must equal what `create_app()`
serves, carry a semver in one constant, and describe every event the runner emits as one
discriminated union. The wire bytes themselves are pinned by `test_event_wire_golden.py`.
"""

import json
import re
import typing
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from chemclaw.agent.session import TurnSession
from chemclaw.api.app import create_app
from chemclaw.api.contract import (
    API_CONTRACT_VERSION,
    CONTRACT_PATH,
    REGENERATE_COMMAND,
    build_document,
    render_document,
)
from chemclaw.api.events import TURN_EVENT_ADAPTER, TURN_EVENT_SCHEMA, Event
from tests.event_samples import SAMPLES
from tests.fakes_turn import Piece, ScriptedTurn

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _kinds() -> set[str]:
    """Every `type` the union declares, read off the members."""
    return {typing.get_args(m.model_fields["type"].annotation)[0] for m in typing.get_args(Event)}


def test_the_committed_document_is_what_the_app_serves() -> None:
    """A route, model or event change that was not regenerated fails here, with the command."""
    expected = render_document(build_document())
    committed = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.exists() else ""
    assert committed == expected, (
        f"{CONTRACT_PATH} is stale: the API surface changed and the published contract did not.\n"
        f"Regenerate it with `{REGENERATE_COMMAND}`, review the diff, and bump "
        "API_CONTRACT_VERSION in src/chemclaw/api/contract.py per schema/api/README.md "
        "(major: a removal, rename or type change; minor: additive)."
    )


def test_the_version_is_one_semver_constant_published_as_info_version() -> None:
    """`API_CONTRACT_VERSION` is the only place the version is written."""
    assert _SEMVER.match(API_CONTRACT_VERSION), API_CONTRACT_VERSION
    committed = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert committed["info"]["version"] == API_CONTRACT_VERSION


def test_the_event_union_is_published_as_a_discriminated_oneof_of_every_kind() -> None:
    """One `oneOf` on `type`; each kind maps to a component declaring that literal."""
    schemas = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["components"]["schemas"]
    union = schemas[TURN_EVENT_SCHEMA]
    assert union["discriminator"]["propertyName"] == "type"
    mapping = union["discriminator"]["mapping"]
    assert set(mapping) == _kinds()
    assert {"capability_degraded", "tool_failed", "job_failed"} <= set(mapping)
    for kind, ref in mapping.items():
        member = schemas[ref.rsplit("/", 1)[1]]
        assert member["properties"]["type"]["const"] == kind


def test_every_sampled_event_validates_against_the_union_and_round_trips() -> None:
    """Each kind's frame parses through the discriminated union to its own class, byte-identical."""
    for key, event in SAMPLES.items():
        data = json.dumps(json.loads(event.model_dump_json()))
        parsed = TURN_EVENT_ADAPTER.validate_json(data)
        assert type(parsed) is type(event), key
        assert parsed.model_dump_json() == event.model_dump_json(), key


class _AnsweringTurn(ScriptedTurn):
    """One token and an answer: the smallest real turn."""

    def create_session(self, *, session_id: str) -> TurnSession:
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "ok"


def test_every_frame_of_a_real_turn_validates_against_the_union() -> None:
    """The runner's actual output, over the real route, parses as the published union."""
    app = create_app(graph_factory=_AnsweringTurn().graph_factory, connector_factory=lambda *_: [])
    frames: list[tuple[str, Any]] = []
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "hi"}) as r:
            assert r.status_code == 200
            name = ""
            for line in r.iter_lines():
                if line.startswith("event:"):
                    name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    frames.append((name, line[len("data:") :].strip()))
    assert frames, "the turn streamed nothing"
    for name, data in frames:
        event = TURN_EVENT_ADAPTER.validate_json(data)
        assert event.type == name, f"the SSE event name {name!r} differs from the body's type"
    assert frames[-1][0] == "answer"
