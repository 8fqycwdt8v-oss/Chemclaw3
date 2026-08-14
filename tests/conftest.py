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

`pytest_collection_modifyitems` owns both wall-clock-cap adjustments: the `thread` timeout method
for Temporal-backed modules, and `PYTEST_TIMEOUT_SCALE`, which is the one knob that relaxes *every*
cap — including the per-test markers, which no command-line flag can reach.
"""

import asyncio
import os
import socket
from collections.abc import Iterator

import psycopg
import pytest
from _pytest.config import UsageError
from _pytest.terminal import TerminalReporter

from chemclaw.connectors.registry import discovered as _connectors_discovered
from chemclaw.core.config import settings
from chemclaw.kg.submission import NoteSubmission
from chemclaw.templates.registry import discovered as _templates_discovered
from tests.pg import create_test_schema, drop_test_schema, schema_dsn

# `pytester` runs a throwaway pytest session inside a tmp dir, which is the only way to observe
# what a hook in *this* file does to a collected item's markers. Enabled here because pytest only
# honours `pytest_plugins` in the rootdir conftest. Used by `tests/test_suite_timeouts.py`.
pytest_plugins = ["pytester"]


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
    destructive tests (`test_vector_index` truncates `note_index`) would otherwise run against
    whatever database the developer's `.env` points at. Redirecting `postgres_dsn` is enough to
    isolate every store, because
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


def timeout_scale() -> float:
    """How much slack every per-test wall-clock cap gets on this machine (default 1.0).

    Not a `Settings` field, for the reason `tests/pg.py` gives for `TEST_SCHEMA`: `core/config/` is
    the operator-facing deployment surface and its parity test requires every field to appear in
    `.env.example`. How loaded the machine running the tests is has nothing to do with a
    deployment.

    **And therefore not spelled `CHEMCLAW_*` either, which it was until this rename.** The prefix
    is the claim that a key comes from that one config, and prose-contract rule 7 enforces exactly
    that over the operator corpus — so the old name passed only by being documented where the rule
    does not look. Measured: one sentence about it added to `README.md`, the natural place to tell
    a person how to run the suite on a loaded machine, and `make prose-validate` failed with
    "names CHEMCLAW_TEST_TIMEOUT_SCALE, which is not a Settings field". It is a pytest knob; it now
    says so, and the runbook may name it.

    Read per call rather than at import so a test can set it and see the effect.
    """
    raw = os.environ.get("PYTEST_TIMEOUT_SCALE", "1")
    try:
        scale = float(raw)
    except ValueError:
        raise UsageError(f"PYTEST_TIMEOUT_SCALE must be a number, got {raw!r}") from None
    if scale <= 0:
        raise UsageError(f"PYTEST_TIMEOUT_SCALE must be positive, got {scale}")
    return scale


def _base_timeout(config: pytest.Config) -> float:
    """The cap an item would get with no marker: `--timeout` if given, else the `timeout` ini."""
    given = config.getoption("timeout", None)
    if given is not None:
        return float(given)
    ini = config.getini("timeout")
    return float(ini) if ini else 0.0


def _apply_timeout_scale(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Multiply every item's effective wall-clock cap by `PYTEST_TIMEOUT_SCALE`.

    **Why a scale and not larger constants.** A `@pytest.mark.timeout(90)` marker overrides
    `--timeout` and `PYTEST_TIMEOUT`, so the tests with the *tightest* caps are exactly the ones a
    loaded machine cannot relax — the inverse of what is wanted. That is not hypothetical: this
    repository's own hardening campaign recorded two `test_pka.py` tests as pre-existing numerical
    failures on unchanged `main` and briefed six agents to ignore them, when both were
    `Timeout (>180.0s) from pytest-timeout` and their assertions had never run. Given
    `--timeout=0` on the same tree and the same box, the pair passed in 1071 s. Hours of work went
    against a false baseline, and a suite that reports red under load teaches its readers to
    discount red.

    Raising the constants instead was considered and rejected: the observed single-test runtime
    under five concurrent agents was ~6x the cap, and that multiplier is a property of the machine,
    not of the test. A constant chosen for a loaded box is no cap at all on an idle one, which
    throws away what these markers are for — naming a spiking optimizer early rather than letting
    it eat the file's whole budget (`test_bo_predict.py`, `test_bo_constraints.py` both say so).
    Scaling keeps every cap's *ratio* to the work and moves them together.

    An explicit marker is written onto every item rather than adjusting the session default,
    because the session default is not what a marked item is held to. Prepended (`append=False`)
    so it becomes the closest marker, and any `method=`/`func_only=` the existing marker carried is
    copied onto the replacement — `_get_item_settings` reads them all off the *one* closest marker,
    so dropping them would silently return a Temporal module to the `signal` method that cannot
    reach it.
    """
    scale = timeout_scale()
    if scale == 1.0:
        return
    default = _base_timeout(config)
    for item in items:
        marker = item.get_closest_marker("timeout")
        kwargs = dict(marker.kwargs) if marker is not None else {}
        seconds = float(marker.args[0]) if marker is not None and marker.args else default
        if seconds <= 0:
            continue  # 0 means "no cap"; scaling it is still no cap
        item.add_marker(pytest.mark.timeout(seconds * scale, **kwargs), append=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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

    `PYTEST_TIMEOUT_SCALE` is applied last, after that marker exists, so the scaled replacement
    can carry `method="thread"` forward.
    """
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and hasattr(module, "start_env_or_skip"):
            item.add_marker(pytest.mark.timeout(method="thread"))
    _apply_timeout_scale(config, items)


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    """Say plainly which failures were timeouts, because two readers already got it wrong.

    `FAILED tests/test_pka.py::test_… - Failed: Timeout (>180.0s) from pytest-timeout` in the
    short summary was read as a numerical failure by two separate reviewers of this repository, and
    the mistake propagated into a campaign's baseline. The difference matters more than its
    wording suggests: a timed-out test proves *nothing* about the assertions it never reached, so
    it is not evidence either way, whereas a failed assertion is a finding.

    Printed as its own section, after the short summary, naming the knob that fixes it.
    """
    timed_out = sorted(
        report.nodeid
        for report in terminalreporter.stats.get("failed", [])
        if "from pytest-timeout" in str(report.longrepr)
    )
    if not timed_out:
        return
    terminalreporter.write_sep("=", "timeouts — these assertions never ran", yellow=True)
    for nodeid in timed_out:
        terminalreporter.write_line(f"TIMEOUT {nodeid}")
    terminalreporter.write_line(
        "These are wall-clock caps, not assertion failures: nothing above is evidence about the "
        "code under test. On a loaded machine re-run with PYTEST_TIMEOUT_SCALE=4 (it scales the "
        "per-test markers too, which --timeout cannot)."
    )
