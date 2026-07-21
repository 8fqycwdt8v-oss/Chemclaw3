"""Shared Postgres connect helper: fail fast with a clear, credential-safe message.

Why this exists: both the calculation store (`calc.postgres_store`) and the fingerprint
store (`mcp_servers.fpstore`) open short-lived psycopg connections, and a down or
misconfigured database otherwise surfaces as a raw `psycopg.OperationalError` traceback that
never says *which* database or *why*. This wraps the connect once (DRY) so every caller
reports "Postgres unreachable at <host>: <cause>" with the DSN password redacted.

The failure is raised as `ConnectionError`, deliberately **not** a `ChemclawError`: an
unreachable database is a transient infrastructure fault, so Temporal should retry the
activity, whereas `ChemclawError` (a `ValueError`) is marked non-retryable bad data.
"""

from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import TupleRow

from chemclaw.config import settings


def _redact(dsn: str) -> str:
    """Return `dsn` with any password removed, so it is safe to echo in an error message."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<postgres>"
    if parts.password is None:
        return dsn
    host = parts.hostname or ""
    netloc = f"{parts.username}@{host}" if parts.username else host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


async def connect(
    dsn: str, *, statement_timeout_seconds: float | None = None
) -> psycopg.AsyncConnection[TupleRow]:
    """Open a Postgres connection, failing fast and clearly when unreachable.

    Uses the configured libpq `connect_timeout` so an unreachable host errors quickly instead
    of hanging the calling activity until its start-to-close timeout. A connection failure is
    re-raised as `ConnectionError` carrying the password-redacted DSN and the underlying
    cause, so an admin immediately sees which database failed and why.

    `statement_timeout_seconds` sets a per-statement wall-clock bound (libpq
    `statement_timeout`) so a hung query is cancelled rather than burning the enclosing
    activity's whole budget. Omit (or pass 0/None) for no per-statement bound — the
    migration runner does this, since an index build may legitimately run long.
    """
    options = None
    if statement_timeout_seconds:
        # libpq statement_timeout is in milliseconds; passed as a server option so it
        # applies to every statement on the connection without an extra round trip.
        options = f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"
    try:
        return await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=settings.pg_connect_timeout_seconds, options=options
        )
    except psycopg.OperationalError as exc:
        raise ConnectionError(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc
