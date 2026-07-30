"""The operational metrics surface (gaps DEP-4, SCH-4, SCH-5).

Three things were working correctly and completely invisibly: admission control shedding turns
with a 503, the budget guard refusing with a 429, and `chemclaw.agent.audit` swallowing a sink
failure to
keep tool calls alive. "At capacity" looked identical to "fine" from outside, and a GxP trail could
be quietly incomplete indefinitely.

The gauge set matters as much as the counters: the Helm chart autoscales the front door on CPU,
which for a stream-bound, model-latency-dominated service is close to noise — a pod blocked on the
model uses almost no CPU while being completely full. In-flight turns against the admission cap is
the signal that actually describes saturation.
"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.api.metrics import (
    _COUNTER_LABELS,
    _MAX_SERIES_PER_COUNTER,
    CONTENT_TYPE,
    Metrics,
)


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
    """The route is unauthenticated (like `/healthz`), so it must expose counts and capacity only.

    This used to assert that a histogram's `le` bucket boundary was the *only* label anywhere, and
    that was the right guard while the registry had no label support. It is now an allowlist of the
    label names actually declared, because the reason behind it was never "no labels" — it was "no
    label may carry turn-derived data on an unauthenticated route".

    A declared label name passes that test on its own terms: `profile` is a value from
    `profiles/*.yaml`, chosen by whoever deploys the system, bounded by the number of files on disk
    and identical for every chemist using it. It says nothing about *who* asked or *what* they
    asked. A session id, an actor oid, a tool argument or a model-supplied string would all fail
    here, and the allowlist is what keeps the next label from being one of those by accident — the
    registry refuses an undeclared label name, and this refuses an undeclared one reaching the wire.
    """
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        client.post("/sessions")
        body = client.get("/metrics").text
    permitted = {"le"} | {label for labels in _COUNTER_LABELS.values() for label in labels}
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        label = re.search(r"\{(.*)\}", line)
        if label is None:
            continue
        names = {pair.split("=", 1)[0] for pair in label.group(1).split(",")}
        assert names <= permitted, f"undeclared label on an unauthenticated route: {line}"
        # `le` is still constrained to a bucket boundary — a number from `_BUCKETS`, never text.
        for pair in label.group(1).split(","):
            if pair.startswith("le="):
                assert re.fullmatch(r'le="(\+Inf|[0-9.]+)"', pair), f"malformed bucket: {line}"


def test_a_declared_label_reaches_the_exposition() -> None:
    """The allowlist above is only meaningful if a label can actually get there.

    Without this, `test_metrics_carry_no_identifiers_or_turn_content` would keep passing on a
    registry that had silently stopped emitting labels at all — which is how a guard becomes
    decoration.
    """
    metrics = Metrics()
    metrics.increment("chemclaw_tokens_total", 7.0, {"profile": "property-lookup"})
    assert 'chemclaw_tokens_total{profile="property-lookup"} 7' in metrics.render()


def test_an_undeclared_label_is_refused() -> None:
    """A label typo is not a crash but a second silent series nobody queries — so it raises."""
    metrics = Metrics()
    with pytest.raises(KeyError):
        metrics.increment("chemclaw_tokens_total", 1.0, {"proflie": "typo"})
    with pytest.raises(KeyError):
        metrics.increment("chemclaw_turns_started_total", 1.0, {"profile": "undeclared-here"})


def test_a_counter_value_sums_across_its_label_sets() -> None:
    """`value()` answers "how many in total", which is what every caller of it means."""
    metrics = Metrics()
    metrics.increment("chemclaw_tokens_total", 3.0, {"profile": "a"})
    metrics.increment("chemclaw_tokens_total", 4.0, {"profile": "b"})
    assert metrics.value("chemclaw_tokens_total") == 7.0


def test_the_series_count_is_capped() -> None:
    """A label value is not bounded by this module, so the map it keys must be.

    The same slow leak this codebase has fixed three times (budget tracker, live sessions, note
    index). Past the cap the new series is refused; the ones already there keep counting.
    """
    metrics = Metrics()
    for index in range(_MAX_SERIES_PER_COUNTER + 10):
        metrics.increment("chemclaw_tokens_total", 1.0, {"profile": f"p{index}"})
    assert metrics.value("chemclaw_tokens_total") == float(_MAX_SERIES_PER_COUNTER)
    # And the ones that were admitted keep working rather than being frozen out too.
    metrics.increment("chemclaw_tokens_total", 5.0, {"profile": "p0"})
    assert metrics.value("chemclaw_tokens_total") == float(_MAX_SERIES_PER_COUNTER) + 5.0


def test_a_swallowed_audit_sink_failure_is_counted() -> None:
    """The GxP trail can be incomplete while tool calls keep working (SEC-3) — that must be visible.

    The bridge imports the registry lazily and tolerates its absence, because the workers import
    `chemclaw.agent.audit` without ever building the front door.
    """
    from chemclaw.api.metrics import METRICS
    from chemclaw.core.metrics_bridge import record_metric

    before = METRICS.value("chemclaw_audit_sink_failures_total")
    record_metric(lambda metrics: metrics.increment("chemclaw_audit_sink_failures_total"))
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

    monkeypatch.setattr("chemclaw.api.app.request_note_reindex", _fake_reindex)
    with TestClient(create_app(agent_factory=lambda _profile: _FakeAgent())) as client:
        response = client.post("/events/knowledge-merged")
    assert response.status_code == 202
    assert response.json()["workflow_id"].startswith("note-reindex-")
    assert started == ["called"]
