"""A per-principal request budget for every authenticated route.

The concurrency cap and token budget bound turns; this bounds everything else, which also does
real work (Temporal, Postgres, connector sweeps).

A token bucket rather than a fixed window, so a caller cannot double the rate across a window
edge; `burst` states how much may be spent at once. Called from `require_principal`, which every
authenticated route depends on, so a new route cannot skip it; `/healthz`, `/readyz` and
`/metrics` are not limited.

Under `session_store=postgres` the bucket is one row per principal (`api/rate_limit_store.py`), so
the rate is the deployment's however many replicas run. A principal just refused is remembered
locally until its next token, which is correct because only this principal's own spends drain its
bucket, and saves a round trip per refused request. If the database cannot be reached the
in-process bucket answers instead, so the limit degrades to per-replica rather than to none.
Under the in-memory store the in-process bucket is the only one.
"""

import logging
import time

import psycopg

from chemclaw.api import rate_limit_store
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

# Principals the shared bucket refused, each to the monotonic moment its next token exists. A cache:
# losing an entry costs one round trip.
_refused_until: BoundedLru[str, float] | None = None

# Whether the last shared spend failed, so an outage is logged when it starts and when it ends and
# not once per request.
_shared_unreachable = False


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
    global _limiter, _refused_until, _shared_unreachable
    _limiter = None
    _refused_until = None
    _shared_unreachable = False


async def _check_shared(principal_id: str) -> None:
    """Spend from the principal's shared bucket, or raise `RateLimited`.

    Falls back to the in-process bucket when the database cannot be reached.
    """
    global _refused_until, _shared_unreachable
    if _refused_until is None:
        _refused_until = BoundedLru(settings.service_rate_limit_max_principals)
    now = time.monotonic()
    wait_until = _refused_until.get(principal_id)
    if wait_until is not None:
        if wait_until > now:
            raise RateLimited(wait_until - now)
    per_minute = settings.service_rate_limit_per_minute
    try:
        spent = await rate_limit_store.spend(
            principal_id, per_minute=per_minute, burst=settings.service_rate_limit_burst
        )
    except (ConnectionError, OSError, psycopg.Error):
        record_metric(lambda m: m.increment("chemclaw_rate_limit_shared_unavailable_total"))
        if not _shared_unreachable:
            _shared_unreachable = True
            logger.warning(
                "the shared request budget is unreachable; limiting per replica until it returns",
                exc_info=True,
            )
        limiter().check(principal_id)
        return
    if _shared_unreachable:
        _shared_unreachable = False
        logger.info("the shared request budget is reachable again")
    if not spent.allowed:
        wait = spent.retry_after(per_minute / 60.0)
        _refused_until.put(principal_id, now + wait)
        raise RateLimited(wait)


async def enforce_request_budget(principal_id: str) -> None:
    """Spend one request against `principal_id`'s budget; raise `RateLimited` when it is gone.

    A no-op when `service_rate_limit_per_minute` is 0, the code default for CLI, tests and dev; the
    chart turns it on.
    """
    if not settings.service_rate_limit_per_minute:
        return
    try:
        if settings.session_store == "postgres":
            await _check_shared(principal_id)
        else:
            limiter().check(principal_id)
    except RateLimited:
        record_metric(lambda m: m.increment("chemclaw_requests_rate_limited_total"))
        logger.info("rate limit exceeded for principal %s", principal_id)
        raise
