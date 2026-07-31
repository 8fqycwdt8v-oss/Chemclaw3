"""Process metrics in Prometheus text format (gap DEP-4).

Observability was structured logs plus opt-in OTel *traces*. There was no metrics surface at all,
which left three things invisible in operations and one thing actively mis-tuned:

- **Load shedding is silent.** Admission control (AG-15) sheds excess turns with a 503 and the
  budget guard refuses with a 429. Both are working as designed and neither is countable, so
  "the service is at capacity" looks identical to "the service is fine" from outside.
- **Lost GxP audit records are silent.** `chemclaw.agent.audit` deliberately swallows a sink
failure to
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
wrong twice over: `api/app.py` never called `configure_telemetry`, so `CHEMCLAW_OTEL_ENABLED`
did nothing at the front door and there was no latency signal at all; and traces are sampled and
per-request, so they cannot answer "what is p95 right now" for an alert or an autoscaler. A load
test had to derive turn latency from the client side because the server exposed none. So there are
now two histograms, and the trace pipeline keeps the per-request detail they deliberately drop.
"""

import logging
import threading
from bisect import bisect_left
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)


def _escape(value: str) -> str:
    r"""Escape a label value for the exposition format.

    Prometheus requires `\\`, `"` and newline escaped inside a label value. A profile name will
    never contain one, which is exactly why it is done here rather than trusted: the escape is a
    property of the format, and the next label to be declared may not be so well behaved.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# Metric name -> help text. Declared up front so every metric is documented at its definition and
# the exposition always carries HELP/TYPE lines (a scrape without them is much harder to read).
_COUNTERS: dict[str, str] = {
    "chemclaw_turns_started_total": "Turns admitted and started.",
    "chemclaw_turns_failed_total": "Turns that ended in an error event.",
    # The two halves of a contended front door, and they mean different things: queueing is the
    # system absorbing a burst, shedding is it declining one. A rising queue rate with a flat shed
    # rate is capacity being used; both rising together is capacity being exceeded. Since D-166
    # both are reported on the turn's own stream, so neither has an HTTP status left to be counted
    # by at the load balancer.
    "chemclaw_turns_queued_total": (
        "Turns that had to wait for an admission permit (a `queued` event was streamed)."
    ),
    "chemclaw_turns_shed_total": (
        "Turns ended by the admission timeout because no permit ever freed (D-166: an error "
        "event on an open stream, previously an HTTP 503)."
    ),
    "chemclaw_turns_refused_budget_total": "Turns refused with 429 by the turn/token budget.",
    "chemclaw_turns_conflict_total": "Turns rejected with 409 (a turn was already running).",
    "chemclaw_turn_timeouts_total": "Turns cancelled by the wall-clock turn timeout.",
    "chemclaw_audit_sink_failures_total": (
        "GxP audit records that could not be persisted (the trail is incomplete)."
    ),
    "chemclaw_jobs_started_total": "Durable jobs launched by an agent tool.",
    "chemclaw_notes_proposed_total": "Notes opened on a branch through the PR-gate.",
    # The counterpart to the line above, and the reason it could not stand alone: a best-effort
    # publish that fails is logged inside a workflow and swallowed, because the science is already
    # durable and a dead git remote must not fail a completed job. That is the right call about the
    # *job* and the wrong shape for the *knowledge* — with only a success counter, a total git
    # outage reads as "zero proposals", which is exactly what an idle system reads as. Two counters
    # make the difference visible and give the alert a ratio to fire on.
    "chemclaw_notes_publish_failures_total": (
        "Knowledge notes that could not be opened on a branch; the knowledge was lost."
    ),
    # A turn whose connectors did not come up still answers — from whatever tools remained. That is
    # the right behaviour and the reason it needs a number: a degraded answer is indistinguishable
    # from a good one in the transcript, and `open_reachable` returned the list to four callers that
    # all discarded it (REV-6). Counted per unreachable connector rather than per degraded turn, so
    # "one connector is dark" and "the fleet is dark" are different rates.
    "chemclaw_connectors_unreachable_total": (
        "Connectors that failed to come up when a turn or template step opened them; their tools "
        "were absent from that turn."
    ),
    "chemclaw_event_streams_rejected_total": (
        "Push-back event streams rejected with 429 at the per-user or per-process cap."
    ),
    # A pooled checkout that times out is indistinguishable, from the route's point of view, from
    # an unreachable database — both arrive as `ConnectionError` and both are retryable. The load
    # run turned 16 of them into HTTP 500s because no route caught them, and the pool they came
    # from was not even exhausted: it never grew past 13 of 64 connections and opened zero new
    # ones. Counted separately from the admission shed so "the loop could not schedule a handoff"
    # is not read as "the LLM endpoint is full".
    "chemclaw_db_unavailable_total": (
        "Requests shed with 503 because a pooled Postgres connection could not be obtained."
    ),
    # Same principle as the watermark counter above: the cross-process turn guard (D-121) is a
    # lease, so it holds only while its holder keeps refreshing. A refresh that fails is the guard
    # narrowing, and it must not be something only a log line knows.
    "chemclaw_turn_claim_refresh_failures_total": (
        "Failed refreshes of a running turn's session claim (D-121): the claim may lapse and "
        "another worker start a turn on the same session."
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
    # The same spend, split along the dimensions it is *priced* along (REV-10). One undifferentiated
    # total cannot answer "what is this costing", because input, output and cache-read carry
    # different prices — cache-read is roughly an order of magnitude cheaper than a fresh input
    # token, so a deployment that caches well and one that does not look identical in the total
    # while their bills do not. MAF already reports all four; nothing read past the sum.
    "chemclaw_input_tokens_total": "Prompt tokens sent to the model, excluding cache reads.",
    "chemclaw_output_tokens_total": "Completion tokens generated by the model.",
    "chemclaw_cache_read_tokens_total": (
        "Prompt tokens served from the provider's cache — priced well below a fresh input token, "
        "so this is the number that shows caching working."
    ),
    "chemclaw_cache_write_tokens_total": (
        "Prompt tokens written to the provider's cache — priced above a fresh input token, so a "
        "cache that is written and never read is a net loss this makes visible. Structurally 0 on "
        "the openai_compatible provider, which reports cache reads but has no cache-write concept: "
        "an honest zero here is not a fault (REV-9)."
    ),
    # Durable history compaction (D-151). A count that stays flat while sessions are long means
    # the pass is not running — the knob is off, or the row floor is never reached — which is
    # the difference between "history is bounded" and "nothing is bounding it".
    "chemclaw_history_rows_compacted_total": (
        "Stored conversation rows removed by durable compaction after a turn."
    ),
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

# Counters that carry labels, and the label names each accepts. A counter absent from this map is
# unlabelled and behaves exactly as before — pre-seeded to zero and rendered as one bare line.
#
# **Declared, not free-form** (REV-10). An undeclared label name raises exactly as an undeclared
# metric already does, because the failure mode of a label typo is not a crash but a second, silent
# time series that no dashboard queries and nobody notices.
#
# **Only `profile`, and deliberately not `model`.** Per-model attribution is already emitted, with
# richer labels than this registry could cheaply provide: MAF's `gen_ai.client.token.usage` carries
# the request model, the response model, the provider and the token type, and the shipped chart
# turns OTel on. What it has never heard of is a Chemclaw *profile* — so that is the gap worth
# filling here, and duplicating the model axis would mean two systems to reconcile.
_COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    "chemclaw_tokens_total": ("profile",),
    "chemclaw_input_tokens_total": ("profile",),
    "chemclaw_output_tokens_total": ("profile",),
    "chemclaw_cache_read_tokens_total": ("profile",),
    "chemclaw_cache_write_tokens_total": ("profile",),
}

# The most label-sets one counter may hold. A label *value* is not bounded by this module — it comes
# from configuration, and a future label could come from a provider response — so an unbounded map
# keyed on it is the same slow leak this codebase has already fixed three times (the budget
# tracker's per-user counters, the front door's live sessions, the note index). Past the cap the new
# series is refused and said so once, rather than being accepted quietly until the pod runs out of
# memory. Generous: `profile` is a handful of names, so reaching this means something is wrong.
_MAX_SERIES_PER_COUNTER = 64

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
        # Labelled series, per counter, keyed by the sorted label pairs. Not pre-seeded: a series
        # exists once it has been observed, which is the Prometheus convention and the same rule
        # the gauge path states — an invented zero is indistinguishable from a real one.
        self._series: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._capped: set[str] = set()

    def increment(
        self, name: str, amount: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a declared counter. An undeclared name or label is a programming error, so raises.

        The declaration is binding **in both directions**: a counter in `_COUNTER_LABELS` must be
        incremented *with* its labels, and one absent from it must be incremented *without* any.
        One rule rather than two, and it removes the case that has no good answer — a bare sample
        beside labelled ones, which a scraper reads as a further series rather than as their total,
        so the counter would silently double-count under any `sum()`.
        """
        if name not in _COUNTERS:
            raise KeyError(f"undeclared counter {name!r}")
        given = dict(labels or {})
        declared = _COUNTER_LABELS.get(name, ())
        if set(given) != set(declared):
            raise KeyError(
                f"counter {name!r} takes label(s) {sorted(declared)}, got {sorted(given)}"
            )
        if not declared:
            with self._lock:
                self._counts[name] += amount
            return
        key = tuple(sorted((label, str(value)) for label, value in given.items()))
        with self._lock:
            series = self._series.setdefault(name, {})
            if key not in series and len(series) >= _MAX_SERIES_PER_COUNTER:
                if name not in self._capped:
                    self._capped.add(name)
                    log.warning(
                        "counter %s reached %d label sets; further series are dropped. A label "
                        "value here is meant to be low-cardinality (a profile name), so this "
                        "means something is generating values it should not.",
                        name,
                        _MAX_SERIES_PER_COUNTER,
                    )
                return
            series[key] = series.get(key, 0.0) + amount

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
        """A counter's total across every label set (tests assert on this, not on the text).

        Summed rather than per-series on purpose: a caller asking for a counter's value wants the
        number the unlabelled counter used to report, and Prometheus aggregates the same way
        server-side. Reading one series is a query concern, not this registry's.
        """
        with self._lock:
            return self._counts[name] + sum(self._series.get(name, {}).values())

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
            series = {name: dict(values) for name, values in self._series.items()}
        lines: list[str] = []
        for name, help_text in _COUNTERS.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
            if name not in _COUNTER_LABELS:
                lines.append(f"{name} {counts[name]:g}")
                continue
            # A labelled counter emits one line per observed series and never a bare one — the
            # bare sample cannot exist, because `increment` requires the declared labels. A
            # counter nothing has observed yet is therefore genuinely absent rather than zero,
            # which is the Prometheus convention and this module's own rule for gauges.
            for key, total in sorted(series.get(name, {}).items()):
                rendered = ",".join(f'{label}="{_escape(value)}"' for label, value in key)
                lines.append(f"{name}{{{rendered}}} {total:g}")
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
