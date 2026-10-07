"""The operational metrics surface.

Admission shedding (503), budget refusals (429) and audit-sink failures are counted so "at
capacity" and an incomplete audit trail are visible from outside. In-flight turns against the
admission cap is the saturation signal: a pod blocked on the model is full while using little CPU.
"""

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.core.metrics import (
    _COUNTER_LABELS,
    _GAUGE_FAMILIES,
    _GAUGE_FAMILY_LABELS,
    _HISTOGRAM_LABELS,
    _MAX_SERIES_PER_COUNTER,
    CONTENT_TYPE,
    Metrics,
)


class _FakeAgent:
    """Minimal agent stand-in; the metrics route never touches it."""

    mcp_tools: list[Any] = []

    def create_session(self, *, session_id: str) -> Any:
        from chemclaw.agent.session import TurnSession

        return TurnSession(session_id=session_id)


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
    with TestClient(create_app()) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE.startswith("text/plain")


def test_the_endpoint_exposes_saturation_not_cpu() -> None:
    """In-flight turns against the cap is the signal the HPA should scale on (gap DEP-4)."""
    with TestClient(create_app()) as client:
        body = client.get("/metrics").text
    assert "chemclaw_turns_in_flight" in body
    assert "chemclaw_turn_capacity" in body


def test_metrics_carry_no_identifiers_or_turn_content() -> None:
    """Metrics carry no identifiers or turn content: the route is unauthenticated, like `/healthz`.

    Label names are checked against an allowlist of the names actually declared. A declared label
    such as `profile` is deployment-chosen and bounded, and says nothing about who asked or what; a
    session id, actor oid, tool argument or model-supplied string would fail here.
    """
    with TestClient(create_app()) as client:
        client.post("/sessions")
        body = client.get("/metrics").text
    # All three declaration tables, not just the counters': `operation` on
    # `chemclaw_db_query_duration_seconds` appears in no counter, and gauge families are declared
    # too.
    permitted = (
        {"le"}
        | {label for labels in _COUNTER_LABELS.values() for label in labels}
        | {label for labels in _HISTOGRAM_LABELS.values() for label in labels}
        | set(_GAUGE_FAMILY_LABELS.values())
    )
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
    """A declared label actually reaches the exposition.

    Otherwise the allowlist test would keep passing on a registry that emitted no labels at all.
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


def test_a_labelled_counter_cannot_be_incremented_bare() -> None:
    """A labelled counter cannot be incremented bare: declared labels are required.

    A scraper reads a bare sample as a further series, not as the total, so any `sum()` would
    double-count.
    """
    metrics = Metrics()
    with pytest.raises(KeyError, match="chemclaw_tokens_total"):
        metrics.increment("chemclaw_tokens_total", 1.0)
    assert "\nchemclaw_tokens_total " not in metrics.render()


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


def test_the_dropped_series_counter_names_the_metric_that_is_undercounting() -> None:
    """The dropped-series counter names the metric that hit the cap.

    Its label domain is the declared metric names, so it is not itself capped; like
    `chemclaw_gauge_read_failures_total`, it carries `("metric",)` so an alert says which metric.
    """
    metrics = Metrics()
    for index in range(_MAX_SERIES_PER_COUNTER + 3):
        metrics.increment("chemclaw_tokens_total", 1.0, {"profile": f"p{index}"})

    exposition = metrics.render()
    assert 'chemclaw_metric_series_dropped_total{metric="chemclaw_tokens_total"} 3' in exposition
    # And the counter that records the cap is not itself subject to it: its label domain is this
    # module's own declared names, which is what makes it safe to label at all.
    assert metrics.value("chemclaw_metric_series_dropped_total") == 3.0


def test_a_swallowed_audit_sink_failure_is_counted() -> None:
    """The trail can be incomplete while tool calls keep working (SEC-3) — that must be visible.

    The bridge imports the registry lazily and tolerates its absence, because the workers import
    `chemclaw.agent.audit` without ever building the front door.
    """
    from chemclaw.core.metrics import METRICS
    from chemclaw.core.metrics_bridge import record_metric

    before = METRICS.value("chemclaw_audit_sink_failures_total")
    record_metric(lambda metrics: metrics.increment("chemclaw_audit_sink_failures_total"))
    assert METRICS.value("chemclaw_audit_sink_failures_total") == before + 1


def test_every_gauge_family_declares_the_label_it_is_keyed_by() -> None:
    """Every gauge family declares the label it is keyed by, checked in both directions.

    A family without a label renders one metric name N times with nothing to tell the series apart;
    a label for an undeclared family renders nothing yet reads as coverage.
    """
    assert set(_GAUGE_FAMILIES) == set(_GAUGE_FAMILY_LABELS), (
        "a gauge family and the label it is keyed by are one declaration in two tables: "
        f"undeclared labels {sorted(set(_GAUGE_FAMILIES) - set(_GAUGE_FAMILY_LABELS))}, "
        f"labels for no family {sorted(set(_GAUGE_FAMILY_LABELS) - set(_GAUGE_FAMILIES))}"
    )


def test_a_gauge_family_renders_one_labelled_series_per_reading() -> None:
    """A gauge family renders one labelled series per reading.

    A family is bound to a callable returning `{label value: reading}` read on every scrape; each
    line carries the label (so `topk` works), and a never-read family is absent rather than zero.
    """
    metrics = Metrics()
    assert "chemclaw_table_bytes{" not in metrics.render(), (
        "an unread gauge family renders a series anyway, so a table holding gigabytes would be "
        "indistinguishable from one nobody has measured"
    )
    metrics.bind_gauge_family(
        "chemclaw_table_bytes", lambda: {"session_messages": 581632.0, "audit_events": 4096.0}
    )
    rendered = metrics.render()
    assert 'chemclaw_table_bytes{table="session_messages"} 581632' in rendered
    assert 'chemclaw_table_bytes{table="audit_events"} 4096' in rendered


def test_a_non_finite_gauge_reading_renders_as_prometheus_spells_it() -> None:
    """A non-finite gauge reading renders as Prometheus spells it (`+Inf`, `NaN`).

    An unparseable sample fails the whole scrape, losing every metric the pod has.
    """
    for reading, expected in (
        (float("inf"), "+Inf"),
        (float("-inf"), "-Inf"),
        (float("nan"), "NaN"),
    ):
        metrics = Metrics()
        metrics.bind_gauge("chemclaw_turns_in_flight", lambda reading=reading: reading)  # type: ignore[misc]
        assert f"chemclaw_turns_in_flight {expected}" in metrics.render()


def test_a_bool_reading_renders_as_a_number_rather_than_as_the_word_true() -> None:
    """A bool reading renders as a number rather than `True`.

    `bool` subclasses `int`, and `True` is not a parseable sample, so binding a gauge to a predicate
    would fail the whole exposition. `_sample` coerces it.
    """
    for reading, expected in ((True, "1.0"), (False, "0.0")):
        metrics = Metrics()
        metrics.bind_gauge("chemclaw_egress_guard_armed", lambda reading=reading: reading)  # type: ignore[misc]
        assert f"chemclaw_egress_guard_armed {expected}\n" in metrics.render()


def test_a_counter_past_a_million_is_rendered_exactly() -> None:
    """A counter past a million is rendered exactly.

    `:g` carries six significant digits, which would render 1,234,567 as `1.23457e+06`: accepted,
    graphed and wrong.
    """
    metrics = Metrics()
    metrics.increment("chemclaw_turns_started_total", amount=1_234_567)
    rendered = metrics.render()
    assert "chemclaw_turns_started_total 1234567" in rendered
    assert "e+06" not in rendered


def test_a_histogram_sum_keeps_the_precision_its_observations_had() -> None:
    """`_sum` had the same six-digit ceiling as a counter, and a duration sum is where it lands.

    A pod observing seconds accumulates past 10^6 in about eleven days of busy tool calls, after
    which every `rate()` over `_sum` reads a rounded numerator against an exact denominator.
    """
    metrics = Metrics()
    metrics.observe("chemclaw_turn_duration_seconds", 1_234_567.25)
    line = next(
        row
        for row in metrics.render().splitlines()
        if row.startswith("chemclaw_turn_duration_seconds_sum")
    )
    assert line.endswith(" 1234567.25"), line


def test_the_bucket_boundary_label_is_left_alone_because_it_is_a_series_identity() -> None:
    """The bucket boundary label `le` is left alone because it is a series identity.

    `3600` and `3600.0` are the same number but two different series to existing dashboards, so `le`
    must not be routed through `_sample`.
    """
    metrics = Metrics()
    metrics.observe("chemclaw_turn_duration_seconds", 0.5)
    rendered = metrics.render()
    assert 'le="1"' in rendered
    assert 'le="1.0"' not in rendered
