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

import psycopg
from psycopg import conninfo
from psycopg.rows import TupleRow

from chemclaw.config import settings


def _redact(dsn: str) -> str:
    """Return `dsn` with any password removed, so it is safe to echo in an error message.

    Round-trips through libpq's own parser (`conninfo_to_dict`/`make_conninfo`) so every
    form psycopg accepts is covered — URL userinfo, URL query parameter, and the keyword
    `host=... password=...` form — not just the userinfo case a URL split can see. A DSN
    libpq cannot parse is replaced wholesale rather than echoed on a guess.
    """
    try:
        parts = conninfo.conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        return "<postgres>"
    parts.pop("password", None)
    return conninfo.make_conninfo("", **parts)


def _merged_options(dsn: str, statement_timeout_seconds: float | None) -> str | None:
    """Return the libpq `options` to connect with: the DSN's own, plus our statement timeout.

    psycopg merges a keyword argument *over* the connection string, so passing `options=` would
    silently discard any `options` the DSN already carries — and only on the connections that ask
    for a statement timeout, since `None` is dropped rather than merged. An operator who sets
    `options` in their DSN (a `search_path` for a shared database, `application_name`, `work_mem`)
    would lose it non-deterministically depending on the call site. Concatenating instead keeps
    both; libpq reads the last occurrence of a repeated `-c` setting, so our timeout still wins if
    the DSN happens to set one too.
    """
    if not statement_timeout_seconds:
        return None  # nothing of ours to add; the DSN's own `options` passes through untouched
    # libpq statement_timeout is in milliseconds; passed as a server option so it applies to
    # every statement on the connection without an extra round trip.
    ours = f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"
    try:
        existing = conninfo.conninfo_to_dict(dsn).get("options")
    except psycopg.ProgrammingError:
        return ours  # unparseable DSN: let the connect itself report it, don't mask the error
    return f"{existing} {ours}" if isinstance(existing, str) and existing else ours


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
    options = _merged_options(dsn, statement_timeout_seconds)
    try:
        return await psycopg.AsyncConnection.connect(
            dsn, connect_timeout=settings.pg_connect_timeout_seconds, options=options
        )
    except psycopg.OperationalError as exc:
        raise ConnectionError(f"Postgres unreachable at {_redact(dsn)}: {exc}") from exc
