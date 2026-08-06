"""The shared Postgres connect helper fails clearly and safely (admin-troubleshooting, P0).

Proves the two behaviors an admin depends on when the database is down: the DSN password is
never echoed, and an unreachable host raises a `ConnectionError` (retryable infra fault, not
a non-retryable `ChemclawError`) whose message names the host and the underlying cause. No
live database is needed — the psycopg connect is monkeypatched to fail.
"""

import asyncio

import psycopg
import pytest

from chemclaw.core import db


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


# --- Every query path is bounded ------------------------------------------------------------
#
# The mutation survivor this closes: `PostgresAuditSink.record` passes
# `statement_timeout_seconds=settings.pg_statement_timeout_seconds` to `db.connection`, and
# replacing it with `None` survived — nothing asserted the bound existed. Postgres tests skip
# offline, so the one place the property is *always* checkable is the source.
#
# Written as a sweep rather than one test per store on purpose. The defect is not "the audit sink
# forgot"; it is that a store can forget, silently, and only a live hung query would say so. Nine
# more call sites had exactly the same shape and exactly the same absence of a test.
#
# **The sweep is what caught the DRY fix, which is the shape it was written for.** Collapsing those
# twenty-nine copies into `db.bounded()` made every one of them stop matching `db.connection(...,
# statement_timeout_seconds=...)`, and this failed rather than quietly sweeping an empty list —
# which is exactly what `test_the_sweep_finds_the_call_sites_it_claims_to_guard` exists to make
# happen. `bounded` is now the bounded spelling and the raw entry points still need the argument.

_UNBOUNDED_BY_DESIGN = {
    # A migration must not have a statement timeout: an index build may legitimately run long, and
    # `core/migrate.py` says so in the comment above this very call. Asserted separately by
    # `tests/test_migrations.py::test_the_run_still_takes_no_statement_timeout`, which is the
    # opposite assertion — that is why the allowlist names it rather than the sweep skipping the
    # file.
    "src/chemclaw/core/migrate.py",
    # Grants are DDL applied by the same operator step as migrations, on the same connection shape.
    "src/chemclaw/core/grants.py",
    # Operator-run live measurement scripts, not a request path. A bound here would cut the
    # measurement rather than protect a chemist.
    "src/chemclaw/cli/live_jobs.py",
    "src/chemclaw/cli/live_storm.py",
}

# The raw entry points: each must be handed a statement timeout explicitly.
_DB_ENTRY_POINTS = {"connection", "pool", "connect"}
# The bounded spelling, which carries `pg_statement_timeout_seconds` by construction. Swept too, so
# a file that switched to it still counts toward the floor below rather than disappearing from it.
_BOUNDED_ENTRY_POINT = "bounded"


def _db_calls() -> list[tuple[str, int, bool]]:
    """Every call into `chemclaw.core.db`'s connection helpers, and whether it bounds statements.

    Resolves the import alias per file, so `db.bounded(...)`, `bounded(...)` imported by name and
    `connect as db_connect` are all found — three spellings that are all in the tree.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    found: list[tuple[str, int, bool]] = []
    for path in sorted((root / "src" / "chemclaw").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "chemclaw.core.db":
                for alias in node.names:
                    imported[alias.asname or alias.name] = alias.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            target = None
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id == "db":
                    target = func.attr
            elif isinstance(func, ast.Name):
                target = imported.get(func.id)
            if target == _BOUNDED_ENTRY_POINT:
                found.append((str(path.relative_to(root)), node.lineno, True))
                continue
            if target not in _DB_ENTRY_POINTS:
                continue
            is_bounded = any(kw.arg == "statement_timeout_seconds" for kw in node.keywords)
            found.append((str(path.relative_to(root)), node.lineno, is_bounded))
    return found


def test_the_sweep_finds_the_call_sites_it_claims_to_guard() -> None:
    """The guard below is vacuous if the AST walk finds nothing — so measure the walk first.

    Every guard-by-source test has this failure mode: a rename upstream turns it into a test that
    passes because it examined an empty list. The floor is deliberately well below the current
    count (37 bounded, 4 allowlisted) so ordinary growth does not touch it, and well above zero.
    """
    calls = _db_calls()
    assert len(calls) > 20, f"the AST walk found only {len(calls)} db calls — it stopped matching"
    assert any(path.endswith("audit_store.py") for path, _, _ in calls), (
        "the call this guard was written for is not being found"
    )


def test_every_query_path_bounds_its_statements() -> None:
    """A Postgres call from a request path carries the configured statement timeout.

    Without it a hung query burns the enclosing Temporal activity or HTTP request rather than
    failing — which is the failure mode the setting exists to prevent, and the one no test could
    see because it only appears against a live database under load.
    """
    unbounded = [
        f"{path}:{line}"
        for path, line, bounded in _db_calls()
        if not bounded and path not in _UNBOUNDED_BY_DESIGN
    ]
    assert not unbounded, (
        "these Postgres calls set no statement timeout, so a hung query has no bound: "
        + ", ".join(unbounded)
        + " — pass statement_timeout_seconds=settings.pg_statement_timeout_seconds, or add the "
        "file to _UNBOUNDED_BY_DESIGN with the reason it must run unbounded."
    )


def test_the_allowlist_names_only_files_that_still_need_it() -> None:
    """An allowlist that outlives its reason is how a guard rots into a rubber stamp.

    Each entry must still contain an unbounded call; a file that has since been fixed, or deleted,
    fails here instead of quietly exempting whatever is written there next.
    """
    unbounded_files = {path for path, _, bounded in _db_calls() if not bounded}
    stale = _UNBOUNDED_BY_DESIGN - unbounded_files
    assert not stale, f"allowlisted but no longer unbounded (drop the entry): {sorted(stale)}"
