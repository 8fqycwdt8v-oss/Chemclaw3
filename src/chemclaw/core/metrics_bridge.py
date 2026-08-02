"""Apply an update to the process metrics registry without letting it break the caller (REV-19).

`core/metrics.py` is deliberately strict: `increment` and `observe` raise `KeyError` on an
undeclared counter name, an undeclared histogram, or a label set that does not match what the
counter declared. That strictness is right — the failure mode of a metric typo is not a crash but a
second, silent time series nobody queries — and it is exactly what must not reach a caller's
request path. A mistyped counter name in the PR-gate, the connector registry or an audit sink would
otherwise propagate out of `record_metric` and fail the operation being counted.

So this is one swallow, written once, wrapping the *update*: the metric is lost, the caller is not.
A second copy of a bare `except Exception: pass` is exactly where a real error goes to hide, which
is why the ~10 call sites across six packages go through here rather than each holding their own.

**What this is no longer.** Until the R2 layering move the registry lived in `chemclaw.api`, so the
import was lazy and this module read as a way around the "`core` imports no sibling" rule. Two
docstrings and an ADR (D-2026-08-01) had already established that the lazy import never protected
anything — `core/metrics.py` is stdlib-only, so it imports successfully in the background worker
and in every connector worker, and always did. The registry is kernel material now and the import
is an ordinary one; the swallow is what it always actually was, and stays.

This began as a private helper in `agent/audit.py` with two callers (the audit-sink failure
counter, the tool-latency histogram) and moved here at the fourth, rather than being imported
across modules by its underscore name.
"""

from collections.abc import Callable

from chemclaw.core.metrics import METRICS, Metrics


def record_metric(update: Callable[[Metrics], None]) -> None:
    """Apply `update` to the process metrics registry, tolerating an update that raises."""
    try:
        update(METRICS)
    except Exception:  # pragma: no cover - defensive; metrics must never break the caller's path
        pass
