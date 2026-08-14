"""What the front door could not tell you about itself.

The load test had to measure turn latency from the client, because the server exposed none: there
were no histograms, `api/app.py` never called `configure_telemetry` (so `CHEMCLAW_OTEL_ENABLED`
was inert at the one process a chemist talks to), and every turn on a pod shared a single
correlation id — bound once inside `build_agent`, which caches one agent per profile for the
process's whole life. The last one is not an observability gap but an audit-trail defect: two
chemists' tool calls were indistinguishable in the audit record.
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

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
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

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
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
    """Prometheus buckets are cumulative and the `+Inf` bucket must equal the count."""
    metrics = Metrics()
    for seconds in (0.02, 0.2, 7.0):
        metrics.observe("chemclaw_tool_duration_seconds", seconds)
    body = metrics.render()

    assert "# TYPE chemclaw_tool_duration_seconds histogram" in body
    assert 'chemclaw_tool_duration_seconds_bucket{le="0.05"} 1' in body
    assert (
        'chemclaw_tool_duration_seconds_bucket{le="0.25"} 2' in body
    )  # cumulative, not per-bucket
    assert 'chemclaw_tool_duration_seconds_bucket{le="+Inf"} 3' in body
    assert "chemclaw_tool_duration_seconds_count 3" in body
    assert "chemclaw_tool_duration_seconds_sum 7.22" in body


def test_a_sample_on_a_boundary_lands_in_that_bucket() -> None:
    """`le` means "less than or equal", so an exact boundary belongs to its own bucket."""
    metrics = Metrics()
    metrics.observe("chemclaw_turn_duration_seconds", 1.0)
    assert 'chemclaw_turn_duration_seconds_bucket{le="1"} 1' in metrics.render()


def test_token_spend_is_counted_not_only_budgeted() -> None:
    """The budget guard metered spend only to refuse a turn; the same number is now a rate."""

    class _MeteredAgent(ScriptedTurn):
        """An agent whose update reports usage the way a provider's does."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
            yield Chunk("ok", input_tokens=30, output_tokens=12)

    before = METRICS.value("chemclaw_tokens_total")
    metered = _MeteredAgent()

    async def _collect() -> None:
        async for _ in run_turn(
            TurnSession(session_id="s-f"),
            "hi",
            connectors=[],
            graph_factory=metered.graph_factory,
        ):
            pass

    asyncio.run(_collect())
    assert METRICS.value("chemclaw_tokens_total") == before + 42


def test_a_real_turn_books_its_spend_against_the_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The metric and the ledger must be fed by the same turn, not merely both exist.

    Asserting that `runner.py` contains a `record_turn_cost(...)` call would pass on a call placed
    where it never runs — the same trap that made the tracing check unable to tell a context manager
    *created* from one *entered*. So this drives a real turn through `run_turn` and reads what the
    sink was handed: the tokens must match the metric's, and the row must carry the identity the
    metric structurally cannot.
    """
    from chemclaw.agent.turn_cost import TurnCost

    booked: list[TurnCost] = []

    class _CapturingSink:
        async def record(self, cost: TurnCost) -> None:
            booked.append(cost)

    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _CapturingSink)

    class _MeteredAgent(ScriptedTurn):
        """The same shape as the agent above: one update carrying a split usage report."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
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

    # And a turn that never answered is billed too, marked as such. Booked from the `finally` and
    # not from the success path, because a turn that broke — or that a client hung up on — spent
    # real tokens, and a ledger holding only the tidy ones is wrong in the direction that hides a
    # runaway. This is the assertion that fails if the call moves onto the answered path.
    class _BrokenAgent(ScriptedTurn):
        """A turn whose model call raises before it says anything."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
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
