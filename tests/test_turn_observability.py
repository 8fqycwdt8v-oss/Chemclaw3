"""What the front door could not tell you about itself.

The load test had to measure turn latency from the client, because the server exposed none: there
were no histograms, `service/app.py` never called `configure_telemetry` (so `CHEMCLAW_OTEL_ENABLED`
was inert at the one process a chemist talks to), and every turn on a pod shared a single
correlation id — bound once inside `build_agent`, which caches one agent per profile for the
process's whole life. The last one is not an observability gap but an audit-trail defect: two
chemists' tool calls were indistinguishable in the GxP record.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import AgentSession

from agents.identity_context import get_current_correlation_id
from service.metrics import METRICS, Metrics
from service.runner import run_turn


class _SilentAgent:
    """A fake agent that reads the ambient correlation id from inside its turn."""

    mcp_tools: list[Any] = []

    def __init__(self) -> None:
        """Start with nothing observed."""
        self.seen: list[str | None] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self, message: str, *, stream: bool, session: AgentSession, **_run_options: Any
    ) -> Any:
        async def _gen() -> Any:
            self.seen.append(get_current_correlation_id())
            yield SimpleNamespace(text="ok", contents=[], user_input_requests=[])

        return _gen()


def _drive(agent: _SilentAgent, session_id: str) -> None:
    """Run one turn to completion, discarding its events."""

    async def _collect() -> None:
        async for _ in run_turn(agent, AgentSession(session_id=session_id), "hi"):
            pass

    asyncio.run(_collect())


def test_each_turn_gets_its_own_correlation_id() -> None:
    """Two turns on one cached agent must not share a correlation id.

    The agent is deliberately reused across both turns, because that is exactly the production
    shape: `service/app.py` caches one agent per profile for the pod's lifetime.
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

    class _BrokenAgent:
        """An agent whose turn raises partway through."""

        mcp_tools: list[Any] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self, message: str, *, stream: bool, session: AgentSession, **_run_options: Any
        ) -> Any:
            async def _gen() -> Any:
                raise RuntimeError("boom")
                yield  # pragma: no cover - unreachable, makes this an async generator

            return _gen()

    before_count, _ = METRICS.observations("chemclaw_turn_duration_seconds")

    async def _collect() -> None:
        async for _ in run_turn(_BrokenAgent(), AgentSession(session_id="s-e"), "hi"):
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

    class _MeteredAgent:
        """An agent whose update reports usage the way MAF's does."""

        mcp_tools: list[Any] = []

        def run(  # noqa: D102 - a fake agent's run, documented by its class
            self, message: str, *, stream: bool, session: AgentSession, **_run_options: Any
        ) -> Any:
            async def _gen() -> Any:
                usage = {"input_token_count": 30, "output_token_count": 12}
                yield SimpleNamespace(
                    text="ok",
                    contents=[SimpleNamespace(usage_details=usage, name=None)],
                    user_input_requests=[],
                )

            return _gen()

    before = METRICS.value("chemclaw_tokens_total")

    async def _collect() -> None:
        async for _ in run_turn(_MeteredAgent(), AgentSession(session_id="s-f"), "hi"):
            pass

    asyncio.run(_collect())
    assert METRICS.value("chemclaw_tokens_total") == before + 42


def test_the_front_door_configures_logging_and_telemetry_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every worker did this at its entrypoint; the process a chemist talks to did not.

    Without it the front door ran on Python's default root logger — WARNING, no format, ignoring
    `CHEMCLAW_LOG_LEVEL` — and `CHEMCLAW_OTEL_ENABLED` had no effect there at all.
    """
    from fastapi.testclient import TestClient

    from service import app as service_app
    from tests.test_service import _FakeAgent, _no_connectors

    calls: list[str] = []
    monkeypatch.setattr(service_app, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(service_app, "configure_telemetry", lambda: calls.append("telemetry"))

    app = service_app.create_app(
        agent_factory=lambda _profile: _FakeAgent(), connector_factory=_no_connectors
    )
    with TestClient(app):
        pass
    assert calls == ["logging", "telemetry"]
