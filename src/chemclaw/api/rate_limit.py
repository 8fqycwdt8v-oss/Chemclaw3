"""A per-principal request budget for every authenticated route.

The concurrency cap and token budget bound turns; this bounds everything else, which also does
real work (Temporal, Postgres, connector sweeps).

A token bucket rather than a fixed window, so a caller cannot double the rate across a window
edge; `burst` states how much may be spent at once. Called from `require_principal`, which every
authenticated route depends on, so a new route cannot skip it; `/healthz`, `/readyz` and
`/metrics` are not limited. Per process: the fleet-wide ceiling is the rate times the replica
count, which belongs at the ingress and is not checked here.
"""

import logging
import time

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)


class RateLimited(Exception):
    """A principal has spent its request budget. Carries the wait a client should honour."""

    def __init__(self, retry_after_seconds: float) -> None:
        """Record how long until one token is available, for the `Retry-After` header."""
        super().__init__("rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class _Bucket:
    """One principal's tokens and the moment they were last computed."""

    __slots__ = ("tokens", "at")

    def __init__(self, tokens: float, at: float) -> None:
        """A full bucket as of `at`."""
        self.tokens = tokens
        self.at = at


class RequestLimiter:
    """Token buckets keyed by principal, bounded in the number of principals it will track.

    The key is attacker-influenced, so the map is a `BoundedLru`: past the cap the
    least-recently-seen principal is evicted and starts with a fresh burst.
    """

    def __init__(self, *, per_minute: float, burst: float, max_principals: int) -> None:
        """Configure the refill rate, the ceiling, and how many principals to remember.

        Raises:
            ValueError: When `per_minute` is not positive (a zero rate would divide by zero once the
                bucket drains). "Unlimited" means not building a limiter, as
                `enforce_request_budget` does.
        """
        if per_minute <= 0:
            raise ValueError(f"per_minute must be > 0, got {per_minute}; do not build a limiter")
        self._rate = per_minute / 60.0
        self._burst = burst
        self._buckets: BoundedLru[str, _Bucket] = BoundedLru(max_principals)

    def check(self, principal_id: str, *, now: float | None = None) -> None:
        """Spend one token for `principal_id`, or raise `RateLimited`.

        `now` is injectable so tests drive refill without sleeping. Monotonic time, so a wall-clock
        step cannot grant or refuse refills.
        """
        moment = time.monotonic() if now is None else now
        bucket = self._buckets.get(principal_id)  # marks the principal recently seen
        if bucket is None:
            bucket = _Bucket(self._burst, moment)
            self._buckets.put(principal_id, bucket)  # inserting evicts past the cap
        else:
            bucket.tokens = min(self._burst, bucket.tokens + (moment - bucket.at) * self._rate)
            bucket.at = moment
        if bucket.tokens < 1.0:
            raise RateLimited((1.0 - bucket.tokens) / self._rate)
        bucket.tokens -= 1.0


_limiter: RequestLimiter | None = None


def limiter() -> RequestLimiter:
    """The process-wide limiter, built from config on first use.

    Lazy so a test's settings apply; `reset_limiter` clears it explicitly between tests.
    """
    global _limiter
    if _limiter is None:
        _limiter = RequestLimiter(
            per_minute=settings.service_rate_limit_per_minute,
            burst=settings.service_rate_limit_burst,
            max_principals=settings.service_rate_limit_max_principals,
        )
    return _limiter


def reset_limiter() -> None:
    """Discard the process limiter so the next call rebuilds it from current config."""
    global _limiter
    _limiter = None


def enforce_request_budget(principal_id: str) -> None:
    """Spend one request against `principal_id`'s budget; raise `RateLimited` when it is gone.

    A no-op when `service_rate_limit_per_minute` is 0, the code default for CLI, tests and dev; the
    chart turns it on.
    """
    if not settings.service_rate_limit_per_minute:
        return
    try:
        limiter().check(principal_id)
    except RateLimited:
        record_metric(lambda m: m.increment("chemclaw_requests_rate_limited_total"))
        logger.info("rate limit exceeded for principal %s", principal_id)
        raise
