"""What `core/db.py` measures, and what it must not claim to have measured.

Both defects here were reproduced with a number before they were fixed, and both are the same
shape: a *label* that was a true-sounding sentence about something else. The duration histogram
timed the caller's whole block and its HELP said "one pooled database operation"; the failure
counter tested the *builtin* `ConnectionError` and its own docstring said a caller's exception is
not a fact about Postgres.

These drive the real `db.connection` seam against the real registry, for the reason
`tests/test_datapath_observability.py` gives: the failure being closed is "the wrong thing was
recorded", which a call-assertion cannot see.
"""

import asyncio
import logging
import time

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.kg.git_submitter import GitNoteSubmitter
from tests.pg import migrated_db_or_skip
from tests.test_datapath_observability import _counter, _events, _series


def _series_or_none(name: str, **labels: str) -> float | None:
    """`_series`, but absent is a value rather than an error — a series that must not exist yet."""
    try:
        return _series(name, **labels)
    except AssertionError:
        return None


# --- the duration is a *hold*, not a query ----------------------------------------------------


def test_the_timed_span_is_the_callers_whole_block() -> None:
    """Pin what `chemclaw_db_query_duration_seconds` means, because its HELP got it wrong.

    Not a regression guard — this passes before and after, deliberately. It is the executable
    statement of the contract the corrected HELP has to describe: the span runs from before the
    checkout to after the `with` body, so whatever the caller does while holding the connection is
    inside the number. Measured on the unfixed tree, a block that slept three seconds booked
    3.015 s and emitted `db.slow`; nothing about that reading was wrong except the word "query".
    """
    asyncio.run(migrated_db_or_skip())
    operation = "review_probe_hold"

    async def run() -> None:
        async with db.connection(settings.postgres_dsn, operation=operation) as conn:
            await conn.execute("SELECT 1")
            await asyncio.sleep(0.2)

    started = time.perf_counter()
    asyncio.run(run())
    elapsed = time.perf_counter() - started

    assert _series("chemclaw_db_query_duration_seconds_count", operation=operation) == 1.0
    # The one bucket that separates "timed the statement" from "timed the block": a `SELECT 1` is
    # sub-millisecond, so a sample at or under 0.1 s would mean the sleep was outside the span.
    assert _series("chemclaw_db_query_duration_seconds_bucket", operation=operation, le="0.1") == 0
    assert elapsed >= 0.2


def test_the_submit_lock_names_the_thing_it_holds_a_connection_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_cluster_lock` holds a connection across an entire git submission, push included.

    Unnamed — which is how it shipped — every note submission booked a network-to-a-forge sample
    into `chemclaw_db_query_duration_seconds{operation="unspecified"}` and emitted a `db.slow`
    WARNING saying a *database operation* held a connection that long. The hold is real and worth
    measuring; what it lacked was a label saying what it is, so a dashboard rendered a remote git
    push as database latency.
    """
    asyncio.run(migrated_db_or_skip())
    # The cross-pod lock only exists where the deployment shares a database; a memory-store
    # deployment is single-process and skips it entirely.
    monkeypatch.setattr(settings, "session_store", "postgres")
    operation = "kg_cluster_submit_lock"
    before = _series_or_none("chemclaw_db_query_duration_seconds_count", operation=operation) or 0.0

    async def run() -> None:
        submitter = GitNoteSubmitter(repo_dir=".", base_branch="main", remote="origin")
        async with submitter._cluster_lock():
            pass

    asyncio.run(run())

    after = _series_or_none("chemclaw_db_query_duration_seconds_count", operation=operation)
    assert after is not None, "the cluster submit lock booked its hold with no operation name"
    assert after == before + 1.0


# --- a caller's dead socket is not a Postgres outage -------------------------------------------


def test_a_callers_own_connection_error_is_not_a_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`ConnectionResetError` from the caller's code booked `kind="unavailable"` and named the DB.

    The builtin `ConnectionError` is the base of `ConnectionResetError`, `BrokenPipeError`,
    `ConnectionAbortedError` and `ConnectionRefusedError` — every one of which an HTTP client, an
    MCP session or a sink driver raises from inside the block. Measured on the unfixed tree,
    `raise ConnectionResetError("my HTTP client died")` produced
    `chemclaw_db_query_failures_total{kind="unavailable"} 1` and a `db.failed` WARNING naming this
    deployment's database, which is a page for somebody else's socket.
    """
    asyncio.run(migrated_db_or_skip())
    before = _counter("chemclaw_db_query_failures_total")

    async def run() -> None:
        async with db.connection(settings.postgres_dsn, operation="review_probe_caller"):
            raise ConnectionResetError("my HTTP client died")

    with caplog.at_level(logging.WARNING, logger="chemclaw.core.db"):
        with pytest.raises(ConnectionResetError):
            asyncio.run(run())

    assert _counter("chemclaw_db_query_failures_total") == before
    assert "db.failed" not in _events(caplog)
    # Still timed: the connection *was* held, and how long is not in dispute.
    assert _series("chemclaw_db_query_duration_seconds_count", operation="review_probe_caller") >= 1


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (db._DatabaseUnavailable("no connection"), "unavailable"),
        (ConnectionResetError("peer reset"), None),
        (BrokenPipeError("pipe"), None),
        (ConnectionRefusedError("refused"), None),
        (ConnectionAbortedError("aborted"), None),
        (ValueError("bad data"), None),
    ],
)
def test_only_this_modules_own_unavailability_counts_as_unavailable(
    exc: BaseException, kind: str | None
) -> None:
    """The class of the exception is now the same statement as the label.

    Four of these six were `unavailable` before the fix, for faults that had nothing to do with
    Postgres. `_DatabaseUnavailable` is raised only by the two places in `core/db.py` that mean it,
    so it cannot be produced by a caller's socket.
    """
    assert db._failure_kind(exc) == kind


def test_an_unreachable_database_is_still_counted() -> None:
    """Narrowing the test must not stop counting the thing it was there for.

    `connect()` wraps a real `OperationalError` into this module's own class, so a genuinely
    unreachable server in a non-pooling process — a migration, a script, a test — still books
    `unavailable`. Port 1 on loopback is closed by construction and reachable with no egress.
    """
    with pytest.raises(ConnectionError) as caught:
        asyncio.run(db.connect("postgresql://nobody@127.0.0.1:1/none"))

    assert isinstance(caught.value, db._DatabaseUnavailable)
    assert db._failure_kind(caught.value) == "unavailable"
