"""Apply an update to the process metrics registry without letting it break the caller.

`core/metrics.py` raises on an undeclared metric or label set, which is right (a typo would
otherwise create a silent series) and must never fail the operation being counted. So the one
swallow lives here, in `record_metric`, and every call site goes through it rather than holding a
bare `except Exception: pass` of its own.

`degraded()` lives here too because it is called from inside `except` blocks, where a raising metric
update would replace the reported failure with a `KeyError`; it must go through `record_metric`, and
`core/metrics.py` cannot import this module without a cycle.
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

    For deliberate swallows (a preference that did not persist, a lost cost row, an unresolved token
    list): each is right not to fail the turn, and each must leave a number on
    `chemclaw_degraded_total`. `logger` is the caller's so the line names the module that degraded.
    `level` defaults to ERROR, since the function definitely did not happen; a site lowering it
    should argue why where it passes it (cosmetic loss, or already gated in CI).

    Args:
        logger: the calling module's logger, so the record names the module that degraded.
        subsystem: a short, source-fixed name for what lost function; becomes the metric label
            and the log marker. Must be a literal at the call site — `tests/test_degraded.py`
            reads them out of the source and pins the set.
        message: a `%`-style format string describing the degradation, as any log call.
        *args: the format arguments for `message`.
        level: the log level; ERROR unless the site argues otherwise.
        exc_info: attach the active exception, as these sites are inside `except` blocks.
    """
    record_metric(lambda m: m.increment(_DEGRADED_COUNTER, labels={"subsystem": subsystem}))
    # G003 suppressed: interpolating eagerly inside an `except` would raise a format mismatch over
    # the failure being reported. Concatenating the prefix keeps one lazy format string.
    logger.log(level, "degraded[%s]: " + message, subsystem, *args, exc_info=exc_info)  # noqa: G003
