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

Metrics are process-wide (one registry per pod), because that is the scope a scrape targets. They
are deliberately *counters and gauges only* — no histograms — since latency distribution is what
the OTel trace pipeline already carries; duplicating it here would be a second source of truth.
"""

import threading
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
}

_GAUGES: dict[str, str] = {
    "chemclaw_turns_in_flight": "Turns currently streaming.",
    "chemclaw_turn_capacity": "Configured maximum concurrent turns (the admission cap).",
    "chemclaw_live_sessions": "Sessions held in the front door's in-process LRU.",
    # Out-of-process capability can fail independently of the chat service, so its reachability
    # is a first-class signal rather than something to find in a log (`connectors.health`).
    "chemclaw_connectors_unhealthy": "Enabled connectors that could not be reached (0 = all up).",
}


class Metrics:
    """A tiny, thread-safe counter/gauge registry that renders Prometheus exposition text.

    Gauges are read through callables rather than stored, so a gauge can never drift from the
    structure it describes (the semaphore, the session map) — there is nothing to keep in sync.
    """

    def __init__(self) -> None:
        """Start with every declared counter at zero and no gauge sources bound."""
        self._lock = threading.Lock()
        self._counts: dict[str, float] = dict.fromkeys(_COUNTERS, 0.0)
        self._gauges: dict[str, Callable[[], float]] = {}

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

    def value(self, name: str) -> float:
        """Current value of a counter (tests assert on this rather than parsing the text)."""
        with self._lock:
            return self._counts[name]

    def render(self) -> str:
        """Render the Prometheus text exposition format (one HELP/TYPE/value block per metric)."""
        with self._lock:
            counts = dict(self._counts)
            gauges = dict(self._gauges)
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
        return "\n".join(lines) + "\n"


# The process-wide registry. A module singleton for the same reason logging configuration is one:
# a scrape targets a process, and code deep in the call tree (the audit sink, a tool) must be able
# to count something without having a registry threaded down to it.
METRICS = Metrics()

# Exposition content type, per the Prometheus text format spec. Kept beside the renderer so the
# route and the format cannot disagree.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
