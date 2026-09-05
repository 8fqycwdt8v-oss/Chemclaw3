"""The shared Postgres connect helper fails clearly and safely (admin-troubleshooting, P0).

Proves the two behaviors an admin depends on when the database is down: the DSN password is
never echoed, and an unreachable host raises a `ConnectionError` (retryable infra fault, not
a non-retryable `ChemclawError`) whose message names the host and the underlying cause. No
live database is needed — the psycopg connect is monkeypatched to fail.
"""

import ast
import asyncio
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from chemclaw.core import db
from chemclaw.core.config import settings


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


def test_no_statement_timeout_still_carries_the_plan_mode_and_the_dsn_options() -> None:
    """With no timeout to add we still contribute the plan mode, and the DSN's own survives.

    This used to assert `None` — "nothing of ours to add". The plan mode is now always ours to
    add, on every connection, for the reason `_FORCE_CUSTOM_PLAN` states.
    """
    dsn = "postgresql://h/db?options=-c%20search_path%3Dchemclaw_test,public"
    for merged in (db._merged_options(dsn, None), db._merged_options(dsn, 0)):
        assert "search_path=chemclaw_test,public" in merged  # the operator's setting survives
        assert "plan_cache_mode=force_custom_plan" in merged
        assert "statement_timeout" not in merged  # none was asked for


def test_statement_timeout_applies_when_the_dsn_has_no_options() -> None:
    """The ordinary case: no DSN options, so ours is the whole string."""
    assert db._merged_options("postgresql://h/db", 1.5) == (
        "-c plan_cache_mode=force_custom_plan -c statement_timeout=1500"
    )


def test_unparseable_dsn_still_gets_our_timeout() -> None:
    """A DSN libpq cannot parse still carries our options; the connect reports the real error."""
    assert db._merged_options("::garbage==", 2.0) == (
        "-c plan_cache_mode=force_custom_plan -c statement_timeout=2000"
    )


def test_every_pooled_connection_refuses_generic_plans() -> None:
    """The plan mode is on every connection this module hands out, whatever else it carries.

    Pinned because the defect it prevents is invisible from the application: psycopg auto-prepares
    on the fifth execution, Postgres may then switch that statement to a generic plan, and for a
    parameterised `ORDER BY embedding <=> $1` the generic plan is a sequential scan — measured at
    9 ms -> 1,280 ms on 100k chunks, permanent for that connection. Nothing in the suite runs one
    statement eleven times on one pooled connection, so only this assertion stands between the
    setting and its silent removal.
    """
    for dsn, timeout in (
        ("postgresql://h/db", None),
        ("postgresql://h/db", 30.0),
        ("postgresql://h/db?options=-c%20work_mem%3D64MB", 5.0),
        ("::garbage==", 1.0),
    ):
        assert "plan_cache_mode=force_custom_plan" in db._merged_options(dsn, timeout)


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


def test_connection_defaults_the_statement_timeout_onto_the_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that names no timeout still connects with `pg_statement_timeout_seconds`.

    The offline half of the live proof in `test_db_pool.py`: it pins the libpq `options` string the
    connect receives, so it runs in the sandbox where no Postgres answers. Resolution happens per
    call rather than as a default argument, which is why monkeypatching the setting reaches it —
    a value frozen at import time would be wrong in every test that redirects the configuration.
    """
    monkeypatch.setattr(settings, "pg_statement_timeout_seconds", 12.0)
    seen: list[object] = []

    class _Conn:
        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _fake_connect(dsn: str, **kwargs: object) -> _Conn:
        seen.append(kwargs.get("options"))
        return _Conn()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _fake_connect)

    async def _run() -> None:
        async with db.connection("postgresql://h/db"):
            pass
        # An explicit bound still wins — the readiness probe depends on being tighter than this.
        async with db.connection("postgresql://h/db", statement_timeout_seconds=2.0):
            pass
        # ...and 0 is how a call site says "no bound" without leaving the pooled helper.
        async with db.connection("postgresql://h/db", statement_timeout_seconds=0):
            pass

    asyncio.run(_run())
    plan = "-c plan_cache_mode=force_custom_plan"
    assert seen == [f"{plan} -c statement_timeout=12000", f"{plan} -c statement_timeout=2000", plan]


_UNBOUNDED_BY_DESIGN = {"chemclaw/core/migrate.py", "chemclaw/core/grants.py"}
_DEFINITION_SITE = "chemclaw/core/db.py"


def _modules_calling_db_connect() -> set[str]:
    """Every module under `src/chemclaw` that calls `chemclaw.core.db.connect`, by repo path.

    Resolved through the imports rather than by matching the name, because `connect` is also
    `chemclaw.core.temporal_client.connect` — which a dozen modules import and which has nothing to
    do with Postgres. Both binding forms are followed: `from chemclaw.core.db import connect [as x]`
    and `from chemclaw.core import db` + `db.connect(...)`. `core/db.py` itself is skipped: it
    *defines* `connect` and calls it from `connection()`, which is the delegation rather than a
    bypass of it, and it binds the name by `def` rather than by an import this walk could follow.
    """
    root = Path(__file__).resolve().parents[1] / "src"
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).as_posix() == _DEFINITION_SITE:
            continue
        tree = ast.parse(path.read_text())
        direct: set[str] = set()
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "chemclaw.core.db":
                direct |= {a.asname or a.name for a in node.names if a.name == "connect"}
            elif isinstance(node, ast.ImportFrom) and node.module == "chemclaw.core":
                module_aliases |= {a.asname or a.name for a in node.names if a.name == "db"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            hit = (isinstance(func, ast.Name) and func.id in direct) or (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            )
            if hit:
                found.add(path.relative_to(root).as_posix())
    return found


def test_only_the_migration_paths_open_an_unbounded_postgres_connection() -> None:
    """`connect()` is the escape hatch from the default bound, so its callers are enumerable.

    Defaulting the timeout in `connection()` closes the hole a forgotten keyword opened, and leaves
    exactly one way to reopen it: reach past `connection()` to `connect()`, which still defaults to
    no bound because a migration's index build legitimately runs long. Two modules want that (the
    migration runner and the grant applier, both of which also need a connection nobody else can be
    handed, for the advisory lock). A third would be a store quietly running unbounded again, which
    is the defect this whole change exists to make impossible rather than merely unlikely — so it
    is pinned here instead of trusted to review.

    Two call sites moved off `connect()` to get here: `cli/live_jobs` and `cli/live_storm` each read
    one scalar from the live database through it, which wanted no dedicated connection and no
    unbounded query — only the shortest way to a connection at the time it was written.
    """
    assert _modules_calling_db_connect() == _UNBOUNDED_BY_DESIGN


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


# Long enough for the interpreter to offer a thread switch inside a reader's iteration of `_POOLS`,
# short enough to stay a unit test: at 1e-6 s switch interval the unguarded registry failed 364
# times in 2 s, and every one of those failures was in the first tenth of a second.
_RACE_SECONDS = 1.0
# Enough entries that a Python-level walk of `_POOLS` spans more than one switch. A front door
# holds three; a pooled process that has run evals holds one more per short-lived loop until a
# reader evicts them, so this is the shape of that dict at its widest, not an invented one.
_RACE_SEEDED_POOLS = 40


def test_reading_this_process_pools_while_another_thread_builds_them_does_not_raise() -> None:
    """`_POOLS` is walked from several threads at once, so every walk must be under the lock.

    The dict is cross-thread by construction: a pool is keyed on the loop that owns it, and a
    second loop in a worker thread is the ordinary case rather than an exotic one —
    `evals/retrieval._run_sync` starts one per live metric call, and `durable/eval_drift` runs
    `run_eval` in a thread *inside* the background worker's `pooling()`. Both readers of the
    registry evict the pools whose loop has ended before answering, and that eviction is a
    Python-level iteration, which the interpreter may switch threads in the middle of. Measured
    before the lock, three churn threads against three readers raised
    `RuntimeError: dictionary changed size during iteration` 364 times in two seconds.

    Where it lands is why this is not a metrics-only defect. On `/metrics` the bound gauge raises
    and the pool series silently vanish. But the same eviction opens `_pool_for`, which is on the
    request path, and a `RuntimeError` there is not a psycopg error: `_failure_kind` returns `None`,
    so neither the `ConnectionError` handler nor `_database_unavailable` recognises it and the route
    500s.

    Driven with real threads, real loops, the real module dict and the real `_pool_for`, because the
    defect *is* the interpreter switching threads mid-comprehension — a test that walks its own copy
    of the registry would pass against the code that crashes. `setswitchinterval` only raises the
    rate at which the switch is offered; it creates nothing.
    """
    dsn = "postgresql://chemclaw@localhost/nothing-is-connected-to"
    seed_loop = asyncio.new_event_loop()
    failures: list[str] = []
    stop = threading.Event()
    switch_interval = sys.getswitchinterval()

    def _churn() -> None:
        """`evals/retrieval._run_sync`'s shape: a loop per call that builds a pool and ends."""

        async def _touch() -> None:
            db._pool_for(dsn, None)

        while not stop.is_set():
            try:
                asyncio.run(_touch())
            except Exception as exc:  # the failure is what is being measured
                failures.append(f"_pool_for {exc!r}")

    def _read() -> None:
        """The `chemclaw_pg_pool_*` gauges: a scrape thread asking what this process holds."""
        while not stop.is_set():
            try:
                db.pool_stats()
            except Exception as exc:  # the failure is what is being measured
                failures.append(f"pool_stats {exc!r}")

    try:
        for index in range(_RACE_SEEDED_POOLS):
            db._POOLS[(seed_loop, f"{dsn}?n={index}", None)] = AsyncConnectionPool(
                conninfo=dsn, min_size=0, max_size=1, open=False
            )
        sys.setswitchinterval(1e-6)
        threads = [threading.Thread(target=_churn) for _ in range(3)]
        threads += [threading.Thread(target=_read) for _ in range(3)]
        for thread in threads:
            thread.start()
        time.sleep(_RACE_SECONDS)
        stop.set()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(switch_interval)
        db._POOLS.clear()
        seed_loop.close()

    assert not failures, f"{len(failures)} of them, e.g. {failures[:3]}"


def test_every_walk_of_the_pool_registry_is_under_the_registry_lock() -> None:
    """The lock is only worth having if no reader of `_POOLS` is left outside it.

    The race above is a probabilistic witness — it fails loudly on the code that shipped, but a
    seventh call site added later would be caught by it only if a scrape happened to be inside that
    exact comprehension. So the invariant is also asserted statically: every function in `db.py`
    whose body names `_POOLS` or `_FOREIGN_POOLS` must also enter `_POOL_REGISTRY_LOCK`. This is the
    cheap half, and it is the half that fails on the *next* one rather than the one already found.
    """
    module = ast.parse(Path("src/chemclaw/core/db.py").read_text(encoding="utf-8"))
    unguarded: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
        if names & {"_POOLS", "_FOREIGN_POOLS"} and "_POOL_REGISTRY_LOCK" not in names:
            unguarded.append(node.name)
    assert not unguarded, f"these walk the pool registry without holding its lock: {unguarded}"
