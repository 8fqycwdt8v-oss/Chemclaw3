"""A turn that lost its connectors must say so (REV-6, D-138).

`open_reachable` returned "the names of the connectors that are not connected, for the caller to
surface", and all four callers — the front-door runner, the CLI, and both template activities —
called it bare and dropped the list. The result is the quietest failure in the system: the model is
handed a shorter tool list, never learns one is missing, and answers confidently from what remains.
"the ELN has nothing on that batch" and "the ELN was unreachable" arrive as the same sentence.

These tests drive a connector that comes up *unconnected* — which is what a dark host actually looks
like here, since `connectors.transport` makes `connect` non-fatal by construction — and assert on
what reaches the chemist and the scrape. They fail on the unfixed code, where the stream carried
tokens and an answer and nothing else.
"""

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

from connectors.registry import open_reachable
from service.metrics import METRICS
from tests.test_service import _client, _FakeAgent


class _DarkMcpTool:
    """A connector whose host is down: it enters its context and stays unconnected.

    Deliberately not a raising stand-in. `open_reachable` catches nothing on purpose — MAF
    re-connects an unconnected tool inside `Agent.run` and would raise there anyway — so the real
    shape of an unreachable connector is exactly this: the context manager succeeds, `is_connected`
    stays `False`, and the tool contributes nothing to the turn.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.is_connected = False
        self.functions: list[Any] = []

    async def __aenter__(self) -> "_DarkMcpTool":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


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
    """The chemist learns the answer is partial while it is still arriving, not afterwards.

    Ordering is the assertion that matters: a marker appended after the answer is read by a person
    who has already acted on it. Before the first token, a surface can render the answer as
    provisional from the start.
    """
    events = _stream_events([_DarkMcpTool("eln")])

    kinds = [e["type"] for e in events]
    assert kinds == ["capability_degraded", "token", "token", "answer"]
    assert events[0]["connectors"] == ["eln"]


def test_the_turn_still_answers_without_its_connectors() -> None:
    """Degrade, do not fail: an unreachable connector costs its tools, never the conversation.

    Worth pinning alongside the announcement, because the obvious over-correction for a silent
    failure is to start raising — which would turn one dark connector into a dead front door.
    """
    events = _stream_events([_DarkMcpTool("eln"), _DarkMcpTool("qm")])

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
            await open_reachable(stack, [_DarkMcpTool("eln"), _DarkMcpTool("qm")])

    before = METRICS.value("chemclaw_connectors_unreachable_total")
    asyncio.run(_open())
    assert METRICS.value("chemclaw_connectors_unreachable_total") == before + 2
