"""The shared Postgres connect helper fails clearly and safely (admin-troubleshooting, P0).

Proves the two behaviors an admin depends on when the database is down: the DSN password is
never echoed, and an unreachable host raises a `ConnectionError` (retryable infra fault, not
a non-retryable `ChemclawError`) whose message names the host and the underlying cause. No
live database is needed — the psycopg connect is monkeypatched to fail.
"""

import asyncio

import psycopg
import pytest

from chemclaw import db


def test_redact_strips_the_password_only() -> None:
    """Redaction removes the password but keeps user/host/port/db for identification."""
    redacted = db._redact("postgresql://u:secret@host:5432/dbname")
    assert "secret" not in redacted
    for kept in ("u", "host", "5432", "dbname"):
        assert kept in redacted
    # Nothing to strip when the DSN carries no password.
    no_password = db._redact("postgresql://host:5432/dbname")
    for kept in ("host", "5432", "dbname"):
        assert kept in no_password


def test_redact_strips_keyword_conninfo_password() -> None:
    """The keyword libpq form ('host=... password=...') is redacted, not echoed verbatim."""
    redacted = db._redact("host=db.prod user=app password=s3cret dbname=chem")
    assert "s3cret" not in redacted
    for kept in ("db.prod", "app", "chem"):
        assert kept in redacted


def test_redact_strips_query_parameter_password() -> None:
    """A URI carrying the password as a query parameter is redacted too."""
    redacted = db._redact("postgresql://db.prod/chem?password=s3cret")
    assert "s3cret" not in redacted
    for kept in ("db.prod", "chem"):
        assert kept in redacted


def test_redact_unparseable_dsn_yields_placeholder() -> None:
    """A DSN libpq cannot parse is replaced wholesale — never echoed on a guess."""
    assert db._redact("::garbage==") == "<postgres>"


def test_connect_wraps_unreachable_db_without_leaking_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OperationalError becomes a ConnectionError with the cause and a redacted DSN."""

    async def _boom(*args: object, **kwargs: object) -> object:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _boom)

    with pytest.raises(ConnectionError) as exc_info:
        asyncio.run(db.connect("postgresql://u:secret@db.host:5432/chem"))

    message = str(exc_info.value)
    assert "secret" not in message  # password never surfaces in the error
    assert "db.host" in message  # but the admin sees which database failed
    assert "connection refused" in message  # ...and the underlying cause
    assert not isinstance(exc_info.value, ValueError)  # not a ChemclawError → Temporal retries
