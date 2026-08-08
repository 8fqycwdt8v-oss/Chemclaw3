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

**`degraded()` lives here rather than in `core/metrics.py`, and that is not filing.** It is the
same shape as `agent/audit.py`'s pattern — count it, then log it under a stable marker — and it is
called from inside `except` blocks, which is the one place a raising metric update is worst: a bad
label name there would replace the degradation the caller was reporting with a `KeyError` from the
reporting itself. So it has to go *through* `record_metric`, and `core/metrics.py` cannot import
this module without a cycle. The registry declares; this module records without endangering the
caller; `degraded` is the second thing that needs exactly that guarantee.
"""

import logging
from collections.abc import Callable

from chemclaw.core.metrics import METRICS, Metrics

_DEGRADED_COUNTER = "chemclaw_degraded_total"


def record_metric(update: Callable[[Metrics], None]) -> None:
    """Apply `update` to the process metrics registry, tolerating an update that raises."""
    try:
        update(METRICS)
    except Exception:  # pragma: no cover - defensive; metrics must never break the caller's path
        pass


def degraded(
    logger: logging.Logger,
    subsystem: str,
    message: str,
    *args: object,
    level: int = logging.ERROR,
    exc_info: bool = True,
) -> None:
    """Record that `subsystem` failed and the caller continued with less: count it, then log it.

    Every call site is a deliberate swallow — a preference that did not persist, a cost row that
    was lost, a connector token list that could not be resolved — and each is right to swallow,
    because the alternative is failing a chemist's turn over telemetry. What was missing is the
    number. Measured on the tree this was written against: **42 `except` handlers that log a
    warning and do not re-raise, across 35 modules, of which 3 counted anything** (`durable/
    publish.py`, `kg/graph.py`, `kg/proposal.py`). The other 32 modules were invisible to anything
    but a log search nobody runs.

    `logger` is the **caller's**, deliberately: a helper that logged under its own name would put
    `chemclaw.core.metrics_bridge` on every degradation line and throw away the one field that says
    where it happened.

    `level` defaults to ERROR, following `agent/audit.py`: a degradation is not a caution about
    something that might matter later, it is a thing that definitely did not happen. The two sites
    that pass `WARNING` are the ones where the lost function is cosmetic or is already gated in CI,
    and each says so where it passes it.

    Args:
        logger: the calling module's logger, so the record names the module that degraded.
        subsystem: a short, source-fixed name for what lost function; becomes the metric label and
            the log marker. Must be a literal at the call site — `tests/test_degraded.py` reads
            them out of the source and pins the set.
        message: a `%`-style format string describing the degradation, as any log call.
        *args: the format arguments for `message`.
        level: the log level; ERROR unless the site argues otherwise.
        exc_info: attach the active exception, as these sites are inside `except` blocks.
    """
    record_metric(lambda m: m.increment(_DEGRADED_COUNTER, labels={"subsystem": subsystem}))
    logger.log(level, "degraded[%s]: " + message, subsystem, *args, exc_info=exc_info)
