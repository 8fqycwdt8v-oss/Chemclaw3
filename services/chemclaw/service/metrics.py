"""Process metrics in Prometheus text format (gap DEP-4).

Observability was structured logs plus opt-in OTel *traces*. There was no metrics surface at all,
which left three things invisible in operations and one thing actively mis-tuned:

- **Load shedding is silent.** Admission control (AG-15) sheds excess turns with a 503 and the
  budget guard refuses with a 429. Both are working as designed and neither is countable, so
  "the service is at capacity" looks identical to "the service is fine" from outside.
- **Lost GxP audit records are silent.** `agents.audit` deliberately swallows a sink failure to
  keep tool calls working (SEC-3) and logs an ERROR marker — but nothing counts it, so an audit
  trail can be quietly incomplete for a long time.
- **The HPA scales on the wrong signal.** `values.yaml` autoscales the front door on CPU at 70%,
  which for an SSE-streaming, LLM-latency-dominated service is close to noise: a pod blocked on
  the model uses almost no CPU while being completely full. In-flight turns against the admission
  cap is the signal that actually describes saturation.

**No new dependency.** `prometheus_client` would be one more package to install, scan, and pin for
what is ~80 lines of text formatting. The exposition format is a stable, trivially-generated text
protocol, and this module is the only place that knows it.

Metrics are process-wide (one registry per pod), because that is the scope a scrape targets.

This module used to say histograms belonged in the OTel trace pipeline rather than here. That was
wrong twice over: `service/app.py` never called `configure_telemetry`, so `CHEMCLAW_OTEL_ENABLED`
did nothing at the front door and there was no latency signal at all; and traces are sampled and
per-request, so they cannot answer "what is p95 right now" for an alert or an autoscaler. A load
test had to derive turn latency from the client side because the server exposed none. So there are
now two histograms, and the trace pipeline keeps the per-request detail they deliberately drop.
"""

import threading
from bisect import bisect_left
from collections.abc import Callable

# Metric name -> help text. Declared up front so every metric is documented at its definition and
# the exposition always carries HELP/TYPE lines (a scrape without them is much harder to read).
_COUNTERS: dict[str, str] = {
    "chemclaw_turns_started_total": "Turns admitted and started.",
    "chemclaw_turns_failed_total": "Turns that ended in an error event.",
    "chemclaw_turns_shed_total": "Turns rejected with 503 because no admission permit was free.",
    "chemclaw_turns_refused_budget_total": "Turns refused with 429 by the turn/token budget.",
    "chemclaw_turns_conflict_total": "Turns rejected with 409 (a turn was already running).",
    "chemclaw_turn_timeouts_total": "Turns cancelled by the wall-clock turn timeout.",
    "chemclaw_audit_sink_failures_total": (
        "GxP audit records that could not be persisted (the trail is incomplete)."
    ),
    "chemclaw_jobs_started_total": "Durable jobs launched by an agent tool.",
    "chemclaw_notes_proposed_total": "Notes opened on a branch through the PR-gate.",
    "chemclaw_event_streams_rejected_total": (
        "Push-back event streams rejected with 429 at the per-user or per-process cap."
    ),
    # A guard that switches itself off is worse than one that fails loudly, and this one did
    # exactly that 32 times in a 126-second load test while nothing but a WARNING said so.
    "chemclaw_rollback_watermark_unavailable_total": (
        "Turns that ran without a durable-history rollback watermark (D-107): a client "
        "disconnect during one of these can leave an orphaned tool_use and brick the session."
    ),
    # The budget guard (service.budget) already meters spend, but only to *refuse* a turn, and its
    # counters are per-process and unexported. This is the same number as an observable rate, so
    # "what is this deployment costing per hour" stops being a question only the provider's bill
    # can answer.
    "chemclaw_tokens_total": "Model tokens reported across all turns (prompt + completion).",
}

# Latency histograms. Two, not more: a turn is the unit a chemist waits on, and a tool call is the
# unit that explains a slow turn. Anything finer is what the trace pipeline is for.
_HISTOGRAMS: dict[str, str] = {
    "chemclaw_turn_duration_seconds": "Wall-clock duration of one streamed agent turn.",
    "chemclaw_tool_duration_seconds": "Wall-clock duration of one tool invocation.",
}

# Bucket boundaries, in seconds. Not a `Settings` field on purpose: Prometheus treats the bucket
# set as part of a histogram's identity, so changing it per deployment breaks aggregation across
# pods and invalidates recorded history — it is a property of the metric's definition, like its
# HELP text, not a deployment knob. The range is chosen for this service's measured shape: a stub
# model puts a turn near 1 s, the load test's p50 at 50 users was 37 s, and the wall-clock turn
# timeout is 600 s, so the buckets have to span three orders of magnitude and still resolve the
# sub-second tool calls that dominate the count.
_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)

_GAUGES: dict[str, str] = {
    "chemclaw_turns_in_flight": "Turns currently streaming.",
    "chemclaw_turn_capacity": "Configured maximum concurrent turns (the admission cap).",
    "chemclaw_live_sessions": "Sessions held in the front door's in-process LRU.",
    # Out-of-process capability can fail independently of the chat service, so its reachability
    # is a first-class signal rather than something to find in a log (`connectors.health`).
    "chemclaw_connectors_unhealthy": "Enabled connectors that could not be reached (0 = all up).",
    # Pool saturation (D-119). `requests_waiting` above zero is the signal that `pg_pool_max_size`
    # is too small for the offered load — the thing that used to show up as a connect timeout with
    # an idle database, which is unreadable from any other metric.
    "chemclaw_pg_pool_size": "Connections held across this process's Postgres pools.",
    "chemclaw_pg_pool_available": "Pooled connections currently idle and available.",
    "chemclaw_pg_pool_requests_waiting": "Callers blocked waiting for a pooled connection.",
}


class Metrics:
    """A tiny, thread-safe counter/gauge/histogram registry rendering Prometheus exposition text.

    Gauges are read through callables rather than stored, so a gauge can never drift from the
    structure it describes (the semaphore, the session map, the connection pools) — there is
    nothing to keep in sync. Counters and histograms are accumulated, since there is no live
    structure holding "how many turns have ever run".
    """

    def __init__(self) -> None:
        """Start with every declared counter at zero and no gauge sources bound."""
        self._lock = threading.Lock()
        self._counts: dict[str, float] = dict.fromkeys(_COUNTERS, 0.0)
        self._gauges: dict[str, Callable[[], float]] = {}
        # Per histogram: one tally per bucket, plus a final overflow slot for samples past the
        # last boundary, plus the running sum. The *cumulative* counts the exposition format wants
        # are derived at render time, so recording a sample is one index and one increment.
        self._histograms: dict[str, list[float]] = {
            name: [0.0] * (len(_BUCKETS) + 1) for name in _HISTOGRAMS
        }
        self._histogram_sums: dict[str, float] = dict.fromkeys(_HISTOGRAMS, 0.0)

    def increment(self, name: str, amount: float = 1.0) -> None:
        """Add to a declared counter. An undeclared name is a programming error, so it raises."""
        if name not in _COUNTERS:
            raise KeyError(f"undeclared counter {name!r}")
        with self._lock:
            self._counts[name] += amount

    def bind_gauge(self, name: str, source: Callable[[], float]) -> None:
        """Bind a gauge to a live source; reading it always reflects current state."""
        if name not in _GAUGES:
            raise KeyError(f"undeclared gauge {name!r}")
        with self._lock:
            self._gauges[name] = source

    def observe(self, name: str, seconds: float) -> None:
        """Record one latency sample. An undeclared name is a programming error, so it raises."""
        if name not in _HISTOGRAMS:
            raise KeyError(f"undeclared histogram {name!r}")
        # `bisect_left` puts a sample exactly on a boundary in that boundary's bucket, which is
        # what Prometheus's `le` ("less than or equal") semantics mean. Past the last boundary it
        # lands in the overflow slot rendered as `le="+Inf"`.
        index = bisect_left(_BUCKETS, seconds)
        with self._lock:
            self._histograms[name][index] += 1.0
            self._histogram_sums[name] += seconds

    def value(self, name: str) -> float:
        """Current value of a counter (tests assert on this rather than parsing the text)."""
        with self._lock:
            return self._counts[name]

    def observations(self, name: str) -> tuple[int, float]:
        """A histogram's `(count, sum)` — what tests assert on instead of parsing the text."""
        with self._lock:
            return int(sum(self._histograms[name])), self._histogram_sums[name]

    def render(self) -> str:
        """Render the Prometheus text exposition format (one HELP/TYPE/value block per metric)."""
        with self._lock:
            counts = dict(self._counts)
            gauges = dict(self._gauges)
            histograms = {name: list(values) for name, values in self._histograms.items()}
            histogram_sums = dict(self._histogram_sums)
        lines: list[str] = []
        for name, help_text in _COUNTERS.items():
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} counter",
                f"{name} {counts[name]:g}",
            ]
        for name, help_text in _GAUGES.items():
            source = gauges.get(name)
            if source is None:
                # A gauge whose source is not bound is omitted rather than reported as 0 — a
                # fabricated zero would be indistinguishable from a genuinely idle service.
                continue
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name} {float(source()):g}",
            ]
        for name, help_text in _HISTOGRAMS.items():
            buckets = histograms[name]
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
            # Prometheus buckets are cumulative ("how many samples were <= le"), so the per-bucket
            # tallies are summed as they are emitted; the final `+Inf` bucket equals the count.
            cumulative = 0.0
            for boundary, tally in zip(_BUCKETS, buckets[:-1], strict=True):
                cumulative += tally
                lines.append(f'{name}_bucket{{le="{boundary:g}"}} {cumulative:g}')
            cumulative += buckets[-1]  # the overflow slot: samples past the last boundary
            lines += [
                f'{name}_bucket{{le="+Inf"}} {cumulative:g}',
                f"{name}_sum {histogram_sums[name]:g}",
                f"{name}_count {cumulative:g}",
            ]
        return "\n".join(lines) + "\n"


# The process-wide registry. A module singleton for the same reason logging configuration is one:
# a scrape targets a process, and code deep in the call tree (the audit sink, a tool) must be able
# to count something without having a registry threaded down to it.
METRICS = Metrics()

# Exposition content type, per the Prometheus text format spec. Kept beside the renderer so the
# route and the format cannot disagree.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
