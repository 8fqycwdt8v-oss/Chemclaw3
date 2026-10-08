"""A principal's request token bucket in Postgres: the rate holds for the deployment, not the pod.

`api/rate_limit.py` is the policy and the in-process fallback; this is the shared half behind the
same budget. One row per principal, refilled lazily by whoever spends next, and spent in one
`INSERT … ON CONFLICT DO UPDATE … RETURNING`: the conflicting writer blocks on the row lock and
re-evaluates against the committed row, so no read-modify-write window exists and two replicas
cannot both spend the last token. Refill and spend use the database's clock, so the replicas'
own clocks cannot grant extra refills.
"""

from typing import NamedTuple

from chemclaw.core import db
from chemclaw.core.config import settings

#: Refill, then spend one token if one is there. The refilled balance is spelled out three times
#: because an upsert's `SET` list cannot reference a sibling expression; the `GREATEST(0, …)` keeps
#: a statement whose transaction clock is a hair behind the committed row from taking tokens away.
#: A principal's first request inserts a full bucket, less the token it spends.
_SPEND = """
    INSERT INTO request_buckets AS b (principal_id, tokens, refilled_at, allowed)
    VALUES (%(principal)s, CASE WHEN %(burst)s >= 1 THEN %(burst)s - 1 ELSE %(burst)s END,
            now(), %(burst)s >= 1)
    ON CONFLICT (principal_id) DO UPDATE SET
        tokens = LEAST(%(burst)s,
                       b.tokens + GREATEST(0, EXTRACT(EPOCH FROM now() - b.refilled_at)) * %(rate)s)
                 - CASE WHEN LEAST(%(burst)s, b.tokens
                       + GREATEST(0, EXTRACT(EPOCH FROM now() - b.refilled_at)) * %(rate)s) >= 1
                        THEN 1 ELSE 0 END,
        allowed = LEAST(%(burst)s, b.tokens
                       + GREATEST(0, EXTRACT(EPOCH FROM now() - b.refilled_at)) * %(rate)s) >= 1,
        refilled_at = now()
    RETURNING allowed, tokens
"""


class Spend(NamedTuple):
    """The outcome of one spend: whether a token was taken, and the balance left afterwards."""

    allowed: bool
    tokens: float

    def retry_after(self, rate_per_second: float) -> float:
        """Seconds until one token is available (0 when this spend was granted)."""
        return 0.0 if self.allowed else max(0.0, (1.0 - self.tokens) / rate_per_second)


def _dsn() -> str:
    """The session tier's database: a request budget has the lifetime of the people using it."""
    return settings.session_store_dsn or settings.postgres_dsn


async def spend(principal_id: str, *, per_minute: float, burst: float) -> Spend:
    """Take one token from `principal_id`'s shared bucket, or learn how long until one is there.

    Raises:
        ConnectionError: The database could not be reached; the caller decides the fallback.
    """
    rate = per_minute / 60.0
    async with db.connection(_dsn(), operation="rate_limit") as conn:
        cursor = await conn.execute(
            _SPEND, {"principal": principal_id, "burst": burst, "rate": rate}
        )
        row = await cursor.fetchone()
    if row is None:  # an upsert with no WHERE always returns its row
        raise ConnectionError("the request bucket statement returned no row")
    return Spend(allowed=bool(row[0]), tokens=float(row[1]))
