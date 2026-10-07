"""The front door's own observability: turn histograms, telemetry setup and per-turn correlation.

Each turn gets its own correlation id; a process-wide one would make two chemists' tool calls
indistinguishable in the audit record.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from chemclaw.agent.session import TurnSession
from chemclaw.api.runner import run_turn
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics import METRICS, Metrics
from tests.fakes_turn import Chunk, Piece, ScriptedTurn


class _SilentAgent(ScriptedTurn):
    """A fake agent that reads the ambient correlation id from inside its turn."""

    def __init__(self) -> None:
        """Start with nothing observed."""
        self.seen: list[str | None] = []

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self.seen.append(get_current_correlation_id())
        yield "ok"


def _drive(agent: ScriptedTurn, session_id: str) -> None:
    """Run one turn to completion on whichever engine is configured, discarding its events."""

    async def _collect() -> None:
        async for _ in run_turn(
            TurnSession(session_id=session_id),
            "hi",
            connectors=[],
            graph_factory=agent.graph_factory,
        ):
            pass

    asyncio.run(_collect())


def test_each_turn_gets_its_own_correlation_id() -> None:
    """Two turns on one cached agent must not share a correlation id.

    The agent is deliberately reused across both turns, because that is exactly the production
    shape: `api/app.py` caches one agent per profile for the pod's lifetime.
    """
    agent = _SilentAgent()
    _drive(agent, "s-a")
    _drive(agent, "s-b")
    assert len(agent.seen) == 2
    assert all(cid for cid in agent.seen)
    assert agent.seen[0] != agent.seen[1]


def test_the_correlation_id_does_not_outlive_its_turn() -> None:
    """Teardown restores the previous value, so nothing leaks into the next thing on this task."""
    assert get_current_correlation_id() is None
    _drive(_SilentAgent(), "s-c")
    assert get_current_correlation_id() is None


def test_a_turn_records_its_duration() -> None:
    """The histogram is what an alert or an autoscaler reads; traces are sampled and per-request."""
    before_count, before_sum = METRICS.observations("chemclaw_turn_duration_seconds")
    _drive(_SilentAgent(), "s-d")
    after_count, after_sum = METRICS.observations("chemclaw_turn_duration_seconds")
    assert after_count == before_count + 1
    assert after_sum > before_sum


def test_a_failed_turn_is_still_timed() -> None:
    """Excluding failures would make the histogram look best exactly when the service is worst."""

    class _BrokenAgent(ScriptedTurn):
        """An agent whose turn raises partway through."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            raise RuntimeError("boom")
            yield  # pragma: no cover - unreachable, makes this an async generator

    before_count, _ = METRICS.observations("chemclaw_turn_duration_seconds")
    broken = _BrokenAgent()

    async def _collect() -> None:
        async for _ in run_turn(
            TurnSession(session_id="s-e"),
            "hi",
            connectors=[],
            graph_factory=broken.graph_factory,
        ):
            pass

    asyncio.run(_collect())
    after_count, _ = METRICS.observations("chemclaw_turn_duration_seconds")
    assert after_count == before_count + 1


def test_the_histogram_renders_cumulative_buckets_with_a_sum_and_count() -> None:
    """Prometheus buckets are cumulative and the `+Inf` bucket must equal the count.

    Driven through the unlabelled `chemclaw_turn_duration_seconds`; the labelled form emits
    different lines and has its own test below.
    """
    metrics = Metrics()
    for seconds in (1.5, 4.0, 25.0):
        metrics.observe("chemclaw_turn_duration_seconds", seconds)
    body = metrics.render()

    assert "# TYPE chemclaw_turn_duration_seconds histogram" in body
    assert 'chemclaw_turn_duration_seconds_bucket{le="2.5"} 1' in body
    assert 'chemclaw_turn_duration_seconds_bucket{le="5"} 2' in body  # cumulative, not per-bucket
    assert 'chemclaw_turn_duration_seconds_bucket{le="+Inf"} 3' in body
    assert "chemclaw_turn_duration_seconds_count 3" in body
    assert "chemclaw_turn_duration_seconds_sum 30.5" in body


def test_a_labelled_histogram_renders_one_series_per_label_set() -> None:
    """The label pairs sit inside the same brace group as `le`, and `_sum`/`_count` carry them too.

    `chemclaw_tool_duration_seconds` is labelled by `tool` so slow tools can be told apart; `le` in
    a second brace group would make every bucket a different series from its own `_sum`.
    """
    metrics = Metrics()
    metrics.observe("chemclaw_tool_duration_seconds", 0.02, labels={"tool": "load_skill"})
    metrics.observe("chemclaw_tool_duration_seconds", 7.0, labels={"tool": "run_xtb"})
    body = metrics.render()

    assert 'chemclaw_tool_duration_seconds_bucket{tool="load_skill",le="0.025"} 1' in body
    assert 'chemclaw_tool_duration_seconds_bucket{tool="load_skill",le="+Inf"} 1' in body
    assert 'chemclaw_tool_duration_seconds_count{tool="run_xtb"} 1' in body
    assert 'chemclaw_tool_duration_seconds_sum{tool="run_xtb"} 7' in body
    # The two tools are separate series, so neither carries the other's samples.
    assert 'chemclaw_tool_duration_seconds_count{tool="load_skill"} 1' in body


def test_a_sample_on_a_boundary_lands_in_that_bucket() -> None:
    """`le` means "less than or equal", so an exact boundary belongs to its own bucket."""
    metrics = Metrics()
    metrics.observe("chemclaw_turn_duration_seconds", 1.0)
    assert 'chemclaw_turn_duration_seconds_bucket{le="1"} 1' in metrics.render()


async def test_token_spend_is_counted_not_only_budgeted() -> None:
    """The budget guard metered spend only to refuse a turn; the same number is now a rate."""

    class _MeteredAgent(ScriptedTurn):
        """An agent whose update reports usage the way a provider's does."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield Chunk("ok", input_tokens=30, output_tokens=12)

    before = METRICS.value("chemclaw_tokens_total")
    metered = _MeteredAgent()

    async for _ in run_turn(
        TurnSession(session_id="s-f"),
        "hi",
        connectors=[],
        graph_factory=metered.graph_factory,
    ):
        pass

    assert METRICS.value("chemclaw_tokens_total") == before + 42


def test_a_real_turn_books_its_spend_against_the_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The metric and the ledger are fed by the same real turn.

    A source check would pass on a call that never runs, so this drives `run_turn` and reads the
    sink: its tokens must match the metric's and the row must carry the actor.
    """
    from chemclaw.agent.turn_cost import TurnCost

    booked: list[TurnCost] = []

    class _CapturingSink:
        async def record(self, cost: TurnCost) -> None:
            booked.append(cost)

    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _CapturingSink)

    class _MeteredAgent(ScriptedTurn):
        """The same shape as the agent above: one update carrying a split usage report."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield Chunk("ok", input_tokens=7, output_tokens=3)

    metered = _MeteredAgent()

    async def _collect() -> None:
        async for _ in run_turn(
            TurnSession(session_id="s-cost"),
            "hi",
            connectors=[],
            graph_factory=metered.graph_factory,
        ):
            pass
        # The write is scheduled rather than awaited (it is booked from a `finally` that also runs
        # on the disconnect path), so yield to the loop before reading it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_collect())

    assert len(booked) == 1, "a completed turn did not reach the cost ledger"
    cost = booked[0]
    assert cost.session_id == "s-cost"
    assert cost.input_tokens == 7 and cost.output_tokens == 3
    assert cost.correlation_id, "the ledger's join to the audit trail is empty"
    assert cost.completed is True
    assert cost.duration_seconds > 0

    # A turn that never answered is billed too, marked as such. Booking happens in the `finally`
    # because a broken or abandoned turn still spent tokens; this fails if the call moves onto the
    # answered path.
    class _BrokenAgent(ScriptedTurn):
        """A turn whose model call raises before it says anything."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            raise RuntimeError("boom")
            yield  # pragma: no cover - unreachable, makes this an async generator

    broken = _BrokenAgent()

    async def _collect_broken() -> None:
        async for _ in run_turn(
            TurnSession(session_id="s-broken"),
            "hi",
            connectors=[],
            graph_factory=broken.graph_factory,
        ):
            pass
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_collect_broken())
    assert len(booked) == 2, "a turn that failed was never billed"
    assert booked[1].completed is False


def test_the_front_door_configures_logging_and_telemetry_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every worker did this at its entrypoint; the process a chemist talks to did not.

    Without it the front door ran on Python's default root logger — WARNING, no format, ignoring
    `CHEMCLAW_LOG_LEVEL` — and `CHEMCLAW_OTEL_ENABLED` had no effect there at all.
    """
    from fastapi.testclient import TestClient

    from chemclaw.api import app as service_app
    from tests.test_service import _no_connectors

    calls: list[str] = []
    monkeypatch.setattr(service_app, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(service_app, "configure_telemetry", lambda: calls.append("telemetry"))

    app = service_app.create_app(connector_factory=_no_connectors)
    with TestClient(app):
        pass
    assert calls == ["logging", "telemetry"]
