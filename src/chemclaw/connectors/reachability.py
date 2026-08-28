"""What this process last learned about each connector's reachability, and when.

**The breaker behind the per-turn connector open**
(`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`). `connectors.health` already
probed every enabled bundle at startup and again on every `/readyz`, and the per-turn open path
consulted none of it — so a connector that was down cost `connector_open_timeout_seconds` on
*every* turn for the whole outage, with no backoff, while a fresh answer sat in the readiness
snapshot. This module is the shared memory that closes that: `connectors.health` writes each
verdict it reaches, `connectors.transport` reads it before dialling.

**Its own module because the two writers cannot see each other.** `connectors.health` imports
`connectors.registry`, which imports `connectors.transport`; a reader placed in `health` would make
the transport's import of it a cycle. Nothing here imports anything from this package, so both
sides can.

**Process-local, deliberately.** The timeout being saved is paid per process, and every process can
observe this fact for itself in one round trip — so a shared store would add an invalidation and a
failure mode of its own to distribute something nobody else needs.

**Recovery is not optional, and it has two paths.** A breaker with no way back is an outage
amplifier: it would keep a recovered connector out of the fleet for as long as nothing dialled it.
So the readiness sweep records *healthy* too, which readmits a connector on the next turn, and
independently of any probe a verdict expires after `connector_breaker_window_seconds`, after which
the next turn dials for real.
"""

import time

from chemclaw.core.config import settings

#: The last reachability verdict this process reached about each connector, and when
#: (`time.monotonic`). Monotonic, not wall-clock, for the reason the readiness cache uses it: a
#: clock adjustment must not make a verdict look arbitrarily fresh. Bounded by the number of
#: enabled connectors, so it cannot grow.
_LAST_SEEN: dict[str, tuple[float, bool]] = {}


def record_reachability(connector: str, *, reachable: bool, dialled: bool = False) -> None:
    """Remember what this process just learned about `connector`, for the open path to read.

    Called from both directions on purpose. The health sweep is the one that runs on a timer — the
    kubelet's `/readyz` every ten seconds — and is therefore what *readmits* a connector that came
    back. The open path records too, because a deployment that never serves `/readyz` (the CLI, a
    template activity on a worker) would otherwise have no verdict at all, and because an MCP
    handshake failing is a fact `/healthz` returning 200 cannot see.

    **`dialled` says whether this observation cost a real MCP open, and it is what may restart the
    breaker's window.** The timestamp is the start of the outage, not the time of the last look at
    it: a *repeated* unreachable verdict from a cheap prober leaves the existing timestamp alone.
    Without that, recovery path 2 in this module's docstring is unreachable in the deployment it
    was written for — the shipped chart probes `/readyz` every ten seconds against a five-second
    readiness cache and a thirty-second window, so three sweeps re-date the verdict inside every
    window and it can never expire. Measured on that cadence, scaled: zero dials across sixty turns.

    A *healthy* verdict always wins and always re-dates, from either observer, so recovery path 1 —
    the sweep readmitting a connector that came back — is untouched. So is a first verdict: the
    outage has to be dated by whoever notices it first.
    """
    previous = _LAST_SEEN.get(connector)
    if not reachable and not dialled and previous is not None and not previous[1]:
        return
    _LAST_SEEN[connector] = (time.monotonic(), reachable)


def recently_unreachable(connector: str) -> bool:
    """Whether this process found `connector` unreachable recently enough to skip dialling it.

    The whole breaker, and it is deliberately this small: the state it reads already existed and
    already had a producer that runs on a timer, so what was missing was a reader
    (`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`).

    A verdict older than `connector_breaker_window_seconds` is not trusted, which is what makes
    recovery unconditional rather than dependent on any probe running: past the window the next
    turn dials for real and records what it finds. `0` disables the breaker outright.
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

    For tests, which would otherwise carry one test's dark connector into the next test's open —
    the order-dependent failure `tests/conftest.py`'s other autouse resets exist to prevent. Named
    `forget_…` rather than `reset_…` on the same grounds as
    `ingest.eln.warehouse.connect.forget_open_warehouses`: nothing is closed and nothing is
    re-probed, a memory is simply dropped.
    """
    _LAST_SEEN.clear()
