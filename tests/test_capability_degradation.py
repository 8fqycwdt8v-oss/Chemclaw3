"""A turn that lost its connectors must say so (REV-6, D-138).

Otherwise the model gets a shorter tool list and answers confidently from what remains: "the ELN
has nothing" and "the ELN was unreachable" read the same. These tests drive a connector that comes
up unconnected (connect is non-fatal by construction) and assert on what reaches the chemist and
the scrape.
"""

import asyncio
import json
from collections.abc import Iterator
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any, cast

import pytest

import chemclaw.api.runner as runner
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.core.metrics import METRICS
from tests.conftest import _free_port
from tests.test_service import _client, _FakeAgent


@pytest.fixture(autouse=True)
def _reachable_durable_subsystem(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hold the durable subsystem up, so these tests say something about connectors.

    No Temporal runs here, so without this every turn would announce a durable-subsystem outage.
    That half is tested in `tests/test_runner.py`.
    """

    async def _reachable() -> bool:
        return True

    monkeypatch.setattr(runner, "_durable_subsystem_reachable", _reachable)
    yield


def _dark_connector(name: str) -> Any:
    """A connector whose host is down: a spec pointing at a port nothing is listening on.

    A closed port rather than a stub, so the real open path (`create_session` fails,
    `HeldConnectorSession` absorbs it, the name comes back in `unreachable`) is what is tested.
    """
    # The tool never answers — this address is deliberately dark — but a manifest may not
    # declare an empty `tools` list.
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{_free_port()}/mcp", tools=["unreached"], read_only=["unreached"]
    )
    return _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=name)), endpoint)


def _stream_events(connectors: list[Any]) -> list[dict[str, Any]]:
    """Run one turn through the real front door and collect its SSE events."""
    agent = _FakeAgent()
    with _client(agent, connector_factory=lambda _profile: connectors) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events: list[dict[str, Any]] = []
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
        ) as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_a_dark_connector_is_announced_before_the_answer_streams() -> None:
    """The chemist learns the answer is partial before the first token, not afterwards.

    A marker appended after the answer is read by someone who has already acted on it.
    """
    events = _stream_events([_dark_connector("eln")])

    kinds = [e["type"] for e in events]
    assert kinds == ["capability_degraded", "token", "token", "answer"]
    assert events[0]["connectors"] == ["eln"]


def test_the_turn_still_answers_without_its_connectors() -> None:
    """Degrade, do not fail: an unreachable connector costs its tools, never the conversation.

    Worth pinning alongside the announcement, because the obvious over-correction for a silent
    failure is to start raising — which would turn one dark connector into a dead front door.
    """
    events = _stream_events([_dark_connector("eln"), _dark_connector("qm")])

    answers = [e for e in events if e["type"] == "answer"]
    assert len(answers) == 1
    assert answers[0]["text"] == "hi there"
    # Both are named. Reporting only the first would understate a fleet-wide outage as one flaky
    # host, which is the difference between "retry" and "page somebody".
    degraded = [e for e in events if e["type"] == "capability_degraded"]
    assert degraded[0]["connectors"] == ["eln", "qm"]


def test_a_healthy_turn_announces_nothing() -> None:
    """No event when every connector came up — a degradation marker on a good turn is noise.

    The failure mode this guards is a surface that learns to ignore the banner because it is always
    there, at which point the announcement is worth less than nothing.
    """
    assert [e["type"] for e in _stream_events([])] == ["token", "token", "answer"]


def test_each_unreachable_connector_moves_the_counter() -> None:
    """Per connector, not per degraded turn: one dark host and a dark fleet are different rates.

    Read off the registry rather than asserting a log line, so the test pins the number an operator
    would actually alert on.
    """

    async def _open() -> None:
        async with AsyncExitStack() as stack:
            await open_connector_specs(stack, [_dark_connector("eln"), _dark_connector("qm")])

    before = METRICS.value("chemclaw_connectors_unreachable_total")
    asyncio.run(_open())
    assert METRICS.value("chemclaw_connectors_unreachable_total") == before + 2
