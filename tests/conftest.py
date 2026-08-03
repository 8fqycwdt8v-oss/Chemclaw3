"""Shared pytest fixtures and test fakes.

`fast_mock` shrinks the mock-HPC sleep durations so server-backed workflow tests
finish in milliseconds; it is autouse but harmless to tests that don't touch
those settings, and it reverts cleanly via monkeypatch after each test.

`FakeSubmitter` is the one PR-gate test double: every test that exercises a
"propose a note" path imports it (`from tests.conftest import FakeSubmitter`)
instead of redefining an identical fake per file (DRY).

`_fresh_discovery_caches` clears the connector and template `@cache`d discovery seams around
every test; see its docstring for why that has to be autouse rather than a per-file convention.

`_free_port` is the one "ask the OS for an unused loopback port" helper, shared by every test
that starts a real server instead of being redefined per file (Rule of Three).
"""

import asyncio
import socket
from collections.abc import Iterator

import psycopg
import pytest

from chemclaw.connectors.registry import discovered as _connectors_discovered
from chemclaw.core.config import settings
from chemclaw.kg.pr_gate import NoteSubmission
from chemclaw.templates.registry import discovered as _templates_discovered
from tests.pg import create_test_schema, drop_test_schema, schema_dsn


def _free_port() -> int:
    """An unused localhost port, so concurrent test runs cannot collide on a fixed one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSubmitter:
    """Records PR-gate submissions instead of touching git, returning a stub PR ref."""

    def __init__(self) -> None:
        """Start with no captured submissions."""
        self.submissions: list[NoteSubmission] = []

    async def submit(self, submission: NoteSubmission) -> str:
        """Capture the submission and return a fake PR reference."""
        self.submissions.append(submission)
        return f"pr://{submission.branch}"


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema() -> Iterator[None]:
    """Point every Postgres-backed test at a dedicated schema, and drop it afterwards.

    Session-scoped and autouse so it is impossible to opt out of by forgetting a fixture: the
    destructive tests (`test_audit_chain` truncates `audit_events`, `test_vector_index`
    truncates `note_index`) would otherwise run against whatever database the developer's
    `.env` points at. Redirecting `postgres_dsn` is enough to isolate every store, because
    they all resolve their own connection from it — see `tests/pg.py`.

    A missing database is not an error here: the per-test `migrated_db_or_skip` already turns
    that into a skip, so this yields untouched and lets it report the reason.
    """
    base_dsn = settings.postgres_dsn
    try:
        asyncio.run(create_test_schema(base_dsn))
    except (psycopg.Error, ConnectionError):  # pragma: no cover - env-dependent
        yield  # no reachable database; the Postgres tests skip themselves
        return

    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "postgres_dsn", schema_dsn(base_dsn))
    # `session_store_dsn` falls back to `postgres_dsn` only while it is empty; an explicitly
    # configured one would otherwise escape the redirect and write to the real schema.
    if settings.session_store_dsn:
        patch.setattr(settings, "session_store_dsn", schema_dsn(settings.session_store_dsn))
    try:
        yield
    finally:
        patch.undo()
        asyncio.run(drop_test_schema(base_dsn))


@pytest.fixture(autouse=True)
def fast_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the mock HPC job complete near-instantly for tests."""
    monkeypatch.setattr(settings, "hpc_mock_submit_seconds", 0.0)
    monkeypatch.setattr(settings, "hpc_mock_run_seconds", 0.02)
    monkeypatch.setattr(settings, "hpc_poll_interval_seconds", 0.01)


@pytest.fixture(autouse=True)
def _fresh_discovery_caches() -> Iterator[None]:
    """Clear the connector and template `@cache`d discovery registries around every test.

    `chemclaw.connectors.registry.discovered` and `chemclaw.templates.registry.discovered` are
    `@cache`d for production, where the bundle/template layout is fixed for the process's life.
    Most of the suite calls them expecting the real, on-disk default; a handful of tests repoint
    `connectors_dir` / `templates_dir` at a `tmp_path` fixture bundle instead. `monkeypatch`
    restores the setting afterwards, but it has no idea a `functools.cache` sits downstream, so a
    test that forgot to clear it left the *next* test reading a stale or `tmp_path`-only result —
    order-dependent failures in `test_agent.py` and `test_prose_contract.py` traced to exactly
    this (`docs/planning/BACKLOG.md`). Clearing both directions, autouse, turns "remember to clear
    the cache" from a per-file convention every new test has to rediscover into an invariant nothing
    can forget — and it is cheap: clearing an empty `functools.cache` is O(1).
    """
    _connectors_discovered.cache_clear()
    _templates_discovered.cache_clear()
    yield
    _connectors_discovered.cache_clear()
    _templates_discovered.cache_clear()


@pytest.fixture(autouse=True)
def loopback_service_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests in the loopback dev posture, so `create_app`'s fail-closed guard admits them.

    The front door refuses to boot unauthenticated on a non-loopback bind (SEC-2); tests drive
    the app entirely in-process (TestClient — no socket is ever bound), so they use the loopback
    posture. The guard's own refuse/opt-in/boot behavior is proven explicitly in test_auth.py,
    which overrides these settings per test.
    """
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give Temporal-backed tests a `thread`-method timeout, because `signal` cannot reach them.

    `pyproject.toml` sets `timeout_method = signal` deliberately: it fails the one hung test and
    lets the session continue, rather than `os._exit`-ing everything after it. That reasoning holds
    for pure-Python hangs and is useless here. `signal` raises from a SIGALRM handler, and the
    interpreter only runs handlers between bytecodes — a test blocked inside `temporalio`'s Rust
    core (PyO3) never gets back to run it, so the cap silently does nothing.

    Measured: a workflow submitted to a queue whose worker had not registered it hung
    `test_bo_knowledge.py` for **28 minutes** past a 600 s cap, until the GitHub job timeout killed
    the run — no test name, no traceback, and `main` red on every commit since the gates were
    enabled. The `thread` method fires from a watchdog thread, so it works regardless of what the
    main thread is stuck in, and it dumps every thread's traceback before exiting.

    Losing "the session continues" costs nothing in this case: a hang that burns the whole job
    stops everything after it anyway. This trades a silent 30-minute cancellation for a named
    failure in three minutes.

    Selected by module rather than by marker so a new Temporal test is covered the day it is
    written: importing `start_env_or_skip` is what makes a module able to hang this way.
    """
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and hasattr(module, "start_env_or_skip"):
            item.add_marker(pytest.mark.timeout(method="thread"))
