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


def test_dsn_options_survive_alongside_a_statement_timeout() -> None:
    """A DSN's own libpq `options` is kept when we add our statement timeout, not overwritten.

    psycopg merges a keyword argument *over* the connection string, so assigning `options=`
    silently dropped whatever the DSN carried — and only on connections that asked for a timeout,
    since `None` is dropped rather than merged. That made an operator's `search_path` (the shape
    the test-schema isolation depends on), `application_name`, or `work_mem` vanish on some call
    sites and survive on others.
    """
    dsn = "postgresql://h/db?options=-c%20search_path%3Dchemclaw_test,public"
    merged = db._merged_options(dsn, 30.0)
    assert merged is not None
    assert "search_path=chemclaw_test,public" in merged  # the operator's setting survives
    assert "statement_timeout=30000" in merged  # and ours is applied
    # Ours last, so libpq's last-occurrence-wins gives our timeout precedence over a DSN's own.
    assert merged.index("statement_timeout") > merged.index("search_path")


def test_no_statement_timeout_leaves_dsn_options_untouched() -> None:
    """With no timeout to add we contribute nothing, so the DSN's `options` passes through."""
    dsn = "postgresql://h/db?options=-c%20search_path%3Dchemclaw_test,public"
    assert db._merged_options(dsn, None) is None
    assert db._merged_options(dsn, 0) is None


def test_statement_timeout_applies_when_the_dsn_has_no_options() -> None:
    """The ordinary case: no DSN options, so ours is the whole string."""
    assert db._merged_options("postgresql://h/db", 1.5) == "-c statement_timeout=1500"


def test_unparseable_dsn_still_gets_our_timeout() -> None:
    """A DSN libpq cannot parse still carries our option; the connect reports the real error."""
    assert db._merged_options("::garbage==", 2.0) == "-c statement_timeout=2000"


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


def test_connection_without_a_pool_opens_a_dedicated_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that never entered `pooling()` keeps the pre-pool behavior: one connect per call.

    Scripts, migrations and unit tests must not need pool setup to talk to Postgres, so the
    fallback path is part of the contract rather than an accident.
    """
    opened: list[str] = []

    class _Conn:
        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _fake_connect(dsn: str, **kwargs: object) -> _Conn:
        opened.append(dsn)
        return _Conn()

    monkeypatch.setattr(db, "connect", _fake_connect)

    async def _run() -> None:
        for _ in range(3):
            async with db.connection("postgresql://h/db"):
                pass

    asyncio.run(_run())
    assert opened == ["postgresql://h/db"] * 3


def test_pooling_resets_its_state_even_when_the_block_raises() -> None:
    """`pooling()` must not leave the process believing it still has a pool after a crash.

    A stuck flag would send every later `connection()` at a pool dictionary that has been
    cleared, so the failure mode of a failed startup would be a permanently broken process
    rather than a restart.
    """

    async def _run() -> None:
        with pytest.raises(RuntimeError):
            async with db.pooling():
                assert db._POOLING is True
                raise RuntimeError("boom")

    asyncio.run(_run())
    assert db._POOLING is False
    assert db._POOLS == {}
