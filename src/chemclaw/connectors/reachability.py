"""What this process last learned about each connector's reachability, and when.

The breaker behind the per-turn connector open: `connectors.health` writes each verdict it
reaches, and `connectors.transport` reads it before dialling, so a down connector does not cost
`connector_open_timeout_seconds` on every turn. Its own module to avoid an import cycle
(health -> registry -> transport). Process-local, since each process can observe the fact itself.

Recovery has two paths: the readiness sweep records healthy verdicts, readmitting a connector on
the next turn, and any verdict expires after `connector_breaker_window_seconds`.
"""

import time

from chemclaw.core.config import settings

# Last verdict per connector and when (`time.monotonic`, so a clock adjustment cannot make it look
# fresh). Bounded by the number of enabled connectors.
_LAST_SEEN: dict[str, tuple[float, bool]] = {}


def record_reachability(connector: str, *, reachable: bool, dialled: bool = False) -> None:
    """Remember what this process just learned about `connector`, for the open path to read.

    Called by the health sweep (which readmits recovered connectors) and by the open path (for
    processes without `/readyz`, and for MCP handshake failures `/healthz` cannot see).

    The timestamp dates the start of an outage: a repeated unreachable verdict only re-dates it when
    `dialled` (a real MCP open), otherwise frequent cheap probes would keep the verdict from ever
    expiring. A healthy verdict always wins and re-dates; a first verdict is always recorded.
    """
    previous = _LAST_SEEN.get(connector)
    if not reachable and not dialled and previous is not None and not previous[1]:
        return
    _LAST_SEEN[connector] = (time.monotonic(), reachable)


def recently_unreachable(connector: str) -> bool:
    """Whether this process found `connector` unreachable recently enough to skip dialling it.

    A verdict older than `connector_breaker_window_seconds` is not trusted, so recovery never
    depends on a probe running. `0` disables the breaker.
    """
    window = settings.connector_breaker_window_seconds
    if not window:
        return False
    seen = _LAST_SEEN.get(connector)
    if seen is None:
        return False
    at, reachable = seen
    return not reachable and time.monotonic() - at < window


def forget_reachability() -> None:
    """Drop every remembered verdict, so the next open dials for real.

    For tests, so one test's dark connector does not leak into the next. Nothing is closed or
    re-probed.
    """
    _LAST_SEEN.clear()
