"""The operational metrics surface (gaps DEP-4, SCH-4, SCH-5).

Three things were working correctly and completely invisibly: admission control shedding turns
with a 503, the budget guard refusing with a 429, and `agents.audit` swallowing a sink failure to
keep tool calls alive. "At capacity" looked identical to "fine" from outside, and a GxP trail could
be quietly incomplete indefinitely.

The gauge set matters as much as the counters: the Helm chart autoscales the front door on CPU,
which for a stream-bound, model-latency-dominated service is close to noise — a pod blocked on the
model uses almost no CPU while being completely full. In-flight turns against the admission cap is
the signal that actually describes saturation.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from service.app import create_app
from service.metrics import CONTENT_TYPE, Metrics


class _FakeAgent:
    """Minimal agent stand-in; the metrics route never touches it."""

    mcp_tools: list[Any] = []

    def create_session(self, *, session_id: str) -> Any:
        from agent_framework import AgentSession

        return AgentSession(session_id=session_id)


def test_a_counter_renders_with_help_and_type() -> None:
    """A scrape without HELP/TYPE lines is far harder to read, so they are part of the contract."""
    metrics = Metrics()
    metrics.increment("chemclaw_turns_started_total", 3)
    text = metrics.render()
    assert "# HELP chemclaw_turns_started_total" in text
    assert "# TYPE chemclaw_turns_started_total counter" in text
    assert "chemclaw_turns_started_total 3" in text


def test_every_declared_counter_is_exposed_even_at_zero() -> None:
    """A missing series and a zero series look identical to an alert rule; zero must be present."""
    text = Metrics().render()
    assert "chemclaw_turns_shed_total 0" in text
    assert "chemclaw_audit_sink_failures_total 0" in text


def test_an_undeclared_metric_is_a_programming_error() -> None:
    """Typos must fail loudly rather than silently creating a series nothing alerts on."""
    metrics = Metrics()
    with pytest.raises(KeyError):
        metrics.increment("chemclaw_typo_total")
    with pytest.raises(KeyError):
        metrics.bind_gauge("chemclaw_typo", lambda: 1.0)


def test_an_unbound_gauge_is_omitted_rather_than_reported_as_zero() -> None:
    """A fabricated zero is indistinguishable from a genuinely idle service."""
    assert "chemclaw_turns_in_flight" not in Metrics().render()


def test_a_gauge_reads_its_live_source_each_time() -> None:
    """Gauges read the structure they describe, so they cannot drift from it."""
    metrics = Metrics()
    live = [0]
    metrics.bind_gauge("chemclaw_turns_in_flight", lambda: float(live[0]))
    assert "chemclaw_turns_in_flight 0" in metrics.render()
    live[0] = 5
    assert "chemclaw_turns_in_flight 5" in metrics.render()


def test_the_endpoint_serves_the_prometheus_content_type() -> None:
    """A scraper keys off the content type; the route and the renderer must agree on it."""
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE.startswith("text/plain")


def test_the_endpoint_exposes_saturation_not_cpu() -> None:
    """In-flight turns against the cap is the signal the HPA should scale on (gap DEP-4)."""
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        body = client.get("/metrics").text
    assert "chemclaw_turns_in_flight" in body
    assert "chemclaw_turn_capacity" in body


def test_metrics_carry_no_identifiers_or_turn_content() -> None:
    """The route is unauthenticated (like /healthz), so it must expose counts and capacity only."""
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        client.post("/sessions")
        body = client.get("/metrics").text
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        # Every series is `name value` — no labels at all, so no id can leak through one.
        assert "{" not in line, f"unexpected label set: {line}"


def test_a_swallowed_audit_sink_failure_is_counted() -> None:
    """The GxP trail can be incomplete while tool calls keep working (SEC-3) — that must be visible.

    `agents.audit` imports the registry lazily and tolerates its absence, because the workers
    import that module without ever building the front door.
    """
    from agents.audit import _count_sink_failure
    from service.metrics import METRICS

    before = METRICS.value("chemclaw_audit_sink_failures_total")
    _count_sink_failure()
    assert METRICS.value("chemclaw_audit_sink_failures_total") == before + 1


def test_a_merge_notification_triggers_a_rebuild_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freshness stops being bounded by the slowest interval (gap SCH-6).

    The whole system was poll-on-a-timer with no inbound event path, so a merged note's worst-case
    staleness was the reindex interval. This collapses it to seconds.
    """
    started: list[str] = []

    async def _fake_reindex() -> str:
        started.append("called")
        return "note-reindex-202607250900"

    monkeypatch.setattr("service.app.request_note_reindex", _fake_reindex)
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        response = client.post("/events/knowledge-merged")
    assert response.status_code == 202
    assert response.json()["workflow_id"].startswith("note-reindex-")
    assert started == ["called"]
