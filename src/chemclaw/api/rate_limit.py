"""A per-principal request budget — the guard the front door had for turns and nothing else.

Two admission controls existed and both were scoped to the expensive path: the concurrency cap
(`service_max_concurrent_turns`) bounds turns in flight, and the budget guard (D-144) meters
tokens. Everything else was unmetered.
So one authenticated caller could hold both of those at zero and still drive `GET /proposals`,
`GET /jobs`, `POST /sessions`, `GET /schedules` and the attachment route as fast as the network
allowed — every one of which does real work (`/schedules` fans out to Temporal, `/readyz` sweeps the
connector fleet, `/jobs` queries Postgres). A loop with no LLM call in it was free.

**A token bucket, not a fixed window.** A fixed window lets a caller spend the whole allowance in
its last millisecond and the next window's in its first, so the observed peak is twice the
configured rate at exactly the moment a system is least able to absorb it. A bucket refilling
continuously has no edge to align to, and its `burst` says out loud what a fixed window only
implies: how much a caller may spend at once.

**Wired inside `require_principal` on purpose, and it is the only thing there that is not
authentication.** The reason is the one D-2026-07-31 used for the PR-gate: every authenticated route
already funnels through that one dependency, so one call there is a gate that a new route cannot
forget, and the alternative — a decorator on twenty routes — is a gate that the twenty-first route
silently skips. `/healthz`, `/readyz` and `/metrics` do not depend on it and are therefore not
limited, which is correct: a kubelet probes every ten seconds and a scrape must never be throttled
into looking like a down target.

**Per process, like the admission cap, and the same caveat applies.** `maxReplicas: 6` multiplies
the real ceiling by six. That is a property of both guards and of the deployment, not something this
module can fix by pretending otherwise; a fleet-wide limit belongs at the ingress, and the backlog
row about the autoscaler defeating the admission guard covers the same ground for the same reason.
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

    The bound is not incidental. A map keyed by caller identity is the classic unbounded-growth
    bug — this codebase has fixed it three times, most recently for metric label series (D-152) —
    and here the key is attacker-influenced, since minting tokens for many `oid`s is exactly what
    someone working around a per-principal limit would do. So it is an LRU
    (`chemclaw.core.bounded.BoundedLru`, the shared fix for this bug class — S2): past the cap the
    least-recently-seen principal is evicted and starts fresh, which costs that caller one free
    burst and costs the process nothing.
    """

    def __init__(self, *, per_minute: float, burst: float, max_principals: int) -> None:
        """Configure the refill rate, the ceiling, and how many principals to remember."""
        self._rate = per_minute / 60.0
        self._burst = burst
        self._buckets: BoundedLru[str, _Bucket] = BoundedLru(max_principals)

    def check(self, principal_id: str, *, now: float | None = None) -> None:
        """Spend one token for `principal_id`, or raise `RateLimited`.

        `now` is injectable so the tests can drive refill without sleeping — a rate limiter tested
        by sleeping is a rate limiter tested at one rate.

        Monotonic time, not wall clock: an NTP step backwards would otherwise hand out a refill
        that never elapsed, and a step forwards would refuse requests that should have passed.
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

    Built lazily rather than at import so a test that changes the settings gets the settings it
    set, and reset by `reset_limiter` between tests. Not a `@cache`, because the reset has to be
    explicit and visible here rather than reaching into a decorator's internals.
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

    A no-op when `service_rate_limit_per_minute` is 0. That is the code default, because a CLI, a
    test and a single-user dev run have no reason to be throttled and a limiter that fires in those
    contexts is a limiter people switch off everywhere; the chart turns it on, which is where the
    shared endpoint actually is — the same shape `budget_enabled` already uses (REV-16).
    """
    if not settings.service_rate_limit_per_minute:
        return
    try:
        limiter().check(principal_id)
    except RateLimited:
        record_metric(lambda m: m.increment("chemclaw_requests_rate_limited_total"))
        logger.info("rate limit exceeded for principal %s", principal_id)
        raise
