"""Shared pytest fixtures and test fakes.

`FakeWriter` is the one note-write test double. `_fresh_derived_tool_sets` and its siblings clear
process-level caches around every test. `_free_port`, `client` and `log_field` are shared helpers.
`pytest_collection_modifyitems` adjusts wall-clock caps (the `thread` method for Temporal modules,
and `PYTEST_TIMEOUT_SCALE`). `pytest_terminal_summary` reports timeouts and what the run skipped.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Generator, Iterator
from typing import Any

import psycopg
import pytest
from _pytest.config import UsageError
from _pytest.terminal import TerminalReporter
from fastapi.testclient import TestClient

from chemclaw.agent.authz import knowledge_read_tools as _knowledge_read_tools
from chemclaw.agent.authz import side_effecting_tools as _side_effecting_tools
from chemclaw.connectors.contract import forget_contract_findings as _forget_contract_findings
from chemclaw.connectors.reachability import forget_reachability as _forget_reachability
from chemclaw.core.config import settings
from chemclaw.ingest.eln.warehouse.connect import forget_open_warehouses as _forget_warehouses
from chemclaw.kg.record import NoteWrite, WriteOutcome
from chemclaw.retrieval.vectors.registry import forget_vector_store as _forget_vector_store
from tests.pg import create_test_schema, drop_test_schema, schema_dsn

# `pytester` runs a throwaway pytest session inside a tmp dir, which is the only way to observe
# what a hook in *this* file does to a collected item's markers. Enabled here because pytest only
# honours `pytest_plugins` in the rootdir conftest. Used by `tests/test_suite_timeouts.py`.
pytest_plugins = ["pytester"]

# The multi-replica lane starts a dozen processes and its own Temporal broker, so the unit gate does
# not collect it; `make live-replicas` names it with this variable (`tests/live_replicas/`).
collect_ignore = [] if os.environ.get("CHEMCLAW_LIVE_REPLICAS") else ["live_replicas"]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin anyio's pytest plugin to asyncio, which is the only loop anything here runs on.

    Without this the plugin parametrizes over every installed backend, adding a failing `[trio]` arm
    if trio is ever installed. Each test still gets a fresh loop that is closed on teardown, which
    `core/db.py`'s per-loop pool cache relies on.
    """
    return "asyncio"


def _free_port() -> int:
    """An unused localhost port, so concurrent test runs cannot collide on a fixed one."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def log_field(record: logging.LogRecord, name: str) -> Any:
    """One `extra=` field off a captured record — `getattr`, because a `LogRecord` has no schema."""
    return getattr(record, name)


@pytest.fixture
def client() -> TestClient:
    """The front door with a fake agent — the same seam every other front-door test uses.

    A file can define its own `client` fixture to override this one. `tests.test_service` is
    imported inside the body so collecting unrelated files does not pay for the front-door import
    tree.
    """
    from tests.test_service import _app, _FakeAgent

    return TestClient(_app(_FakeAgent()))


class FakeWriter:
    """Captures note writes instead of touching git, returning a stub commit reference."""

    def __init__(self) -> None:
        """Start with no captured writes."""
        self.writes: list[NoteWrite] = []

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Capture the write and return a fake commit reference."""
        self.writes.append(write)
        return WriteOutcome(reference=f"commit://{len(self.writes)}")


#: Connections per Postgres pool in each xdist worker (see `isolated_postgres_schema`).
_XDIST_POOL = 8

# Every `Settings` field naming a Postgres database this suite would otherwise write to; each is
# redirected into the isolation schema. `tests/test_suite_isolation.py` fails if a new `*_dsn` field
# is not listed here.
_ISOLATED_DSN_SETTINGS = ("postgres_dsn", "postgres_migration_dsn", "session_store_dsn")


def redirect_dsns_to_test_schema(patch: pytest.MonkeyPatch) -> None:
    """Point every configured Postgres DSN setting at `tests.pg.TEST_SCHEMA`.

    A loop over a named list so no DSN setting escapes isolation (an unredirected migration DSN
    would leave the schema empty and fall through to `public`). An empty setting is left alone: it
    falls back to `postgres_dsn`, which is already redirected.
    """
    for name in _ISOLATED_DSN_SETTINGS:
        configured = str(getattr(settings, name))
        if configured:
            patch.setattr(settings, name, schema_dsn(configured))


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema() -> Iterator[None]:
    """Point every Postgres-backed test at a dedicated schema, and drop it afterwards.

    Session-scoped and autouse so destructive tests can never run against the developer's database.
    The schema is created with the unredirected runtime DSN, so a configuration that cannot be
    isolated fails loudly. A missing database is not an error here; `migrated_db_or_skip` reports it
    per test.
    """
    base_dsn = settings.postgres_dsn
    try:
        asyncio.run(create_test_schema(base_dsn))
    except (psycopg.Error, ConnectionError):  # pragma: no cover - env-dependent
        yield  # no reachable database; the Postgres tests skip themselves
        return

    patch = pytest.MonkeyPatch()
    redirect_dsns_to_test_schema(patch)
    if os.environ.get("PYTEST_XDIST_WORKER"):
        # Every xdist worker draws its own pools against one server; a small pool per worker keeps
        # `-n 4` well inside a stock `max_connections` of 100.
        patch.setattr(settings, "pg_pool_max_size", min(settings.pg_pool_max_size, _XDIST_POOL))
    try:
        yield
    finally:
        patch.undo()
        asyncio.run(drop_test_schema(base_dsn))


@pytest.fixture(autouse=True)
def _fresh_derived_tool_sets() -> Iterator[None]:
    """Clear the two `@cache`d authorization sets derived from the discovery registries, per test.

    `side_effecting_tools` and `knowledge_read_tools` are cached on no arguments while their real
    input is the enabled manifests, so a test that repoints `connectors_dir` would leak into the
    next. Autouse because a per-file convention gets forgotten and fails order-dependently
    elsewhere; it is cheap. The discovery registries themselves are keyed on their directories and
    need no clearing (`forget_discovered()` covers new manifests in an already-read directory).
    """
    _side_effecting_tools.cache_clear()
    _knowledge_read_tools.cache_clear()
    yield
    _side_effecting_tools.cache_clear()
    _knowledge_read_tools.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_connector_reachability() -> Iterator[None]:
    """Forget the per-process connector reachability and contract findings around every test.

    Otherwise one test's unreachable connector would stop the next test from dialling it, and one
    test's logged finding would silence the next test's, order-dependently.
    """
    _forget_reachability()
    _forget_contract_findings()
    yield
    _forget_reachability()
    _forget_contract_findings()


@pytest.fixture(autouse=True)
def _fresh_attached_connections() -> Iterator[None]:
    """Forget the process-lived warehouse connections and vector store around every test.

    Both are memoised in production; in tests a cached connection would serve every later test the
    first test's `warehouse_fake.prime()` rows.
    """
    _forget_warehouses()
    _forget_vector_store()
    yield
    _forget_warehouses()
    _forget_vector_store()


@pytest.fixture(autouse=True)
def loopback_dev_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests in the loopback dev posture, so the fail-closed boot guards admit them.

    Declares the three postures the boot guards require: a loopback front-door bind, a declared
    loopback `llm_base_url` (the mock gateway), and an unauthenticated Temporal worker. Each guard's
    refusal is tested in `test_auth.py`, `test_llm_gateway_guard.py` and `test_worker_posture.py` by
    overriding these per test.
    """
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    monkeypatch.setattr(settings, "llm_allow_loopback_gateway", True)
    monkeypatch.setattr(settings, "worker_allow_unauthenticated", True)


def timeout_scale() -> float:
    """How much slack every per-test wall-clock cap gets on this machine (default 1.0).

    Not a `Settings` field and not `CHEMCLAW_*`-prefixed: machine load is not deployment
    configuration. Read per call so a test can set it.
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

    A `timeout` marker overrides `--timeout`, so only a scale can relax the tightest caps on a
    loaded machine while keeping each cap's ratio to its work. An explicit marker is prepended to
    every item so it is the closest one, copying any `method=`/`func_only=` from the marker it
    replaces — dropping them would return a Temporal module to the `signal` method.
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


# The `tests/temporal_env.py` helpers whose presence in a module means "this can wait on a
# broker". Named as a tuple so adding a third starter is one entry rather than a second
# `hasattr` somebody has to remember.
_ENV_STARTERS = ("start_env_or_skip", "start_local_env_or_skip")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give Temporal-backed tests a `thread`-method timeout, because `signal` cannot reach them.

    The `signal` method raises from a SIGALRM handler, which never runs while a test is blocked
    inside `temporalio`'s Rust core, so a hang would outlive its cap. The `thread` method fires from
    a watchdog thread and dumps tracebacks. Modules are selected by importing either starter from
    `tests/temporal_env.py`, so a new Temporal test is covered automatically. `PYTEST_TIMEOUT_SCALE`
    is applied afterwards and carries `method="thread"` forward.
    """
    for item in items:
        module = getattr(item, "module", None)
        if module is not None and any(hasattr(module, name) for name in _ENV_STARTERS):
            item.add_marker(pytest.mark.timeout(method="thread"))
    _apply_timeout_scale(config, items)


# The marker `tests/pg.py::migrated_db_or_skip` puts in its skip reason. Matched as a substring
# rather than by counting test files, because what a reader needs is how many *tests* did not run,
# and only the run knows that.
_POSTGRES_SKIP = "Postgres unavailable"


def _report_postgres_skips(terminalreporter: TerminalReporter) -> None:
    """Say how many tests the unreachable database took away.

    A run with no Postgres skips the whole durable layer and still prints a green line. The count is
    measured by the run itself rather than written in prose, where it goes stale.
    """
    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if _POSTGRES_SKIP in str(report.longrepr)
    ]
    if not skipped:
        return
    terminalreporter.write_sep("=", "Postgres-backed tests did not run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} tests were skipped because Postgres was unreachable, so this run is not "
        "evidence about the durable layer, the session store, the note-proposal tables, the "
        "publish outbox or retention. Start it: `sudo -n dockerd &`, `make up`, `make db-migrate`."
    )


def _report_public_schema_shadowing(terminalreporter: TerminalReporter) -> None:
    """Say when a green run was green because *this* database has already run the agent.

    The isolation DSN falls through to `public`, and the checkpointer/store tables no migration
    creates exist there once a dev database has run the agent, but never in CI. A test reading them
    unqualified passes locally and fails in CI; this makes that visible. Reported, not enforced:
    having run the agent locally is not a mistake (`tests/pg.py::create_checkpoint_tables` is the
    fix for a test that needs the tables).
    """
    from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
    from chemclaw.agent.scratchpad import STORE_TABLES

    upstream = sorted({*CHECKPOINT_TABLES, *STORE_TABLES})
    try:
        with psycopg.connect(settings.postgres_dsn, connect_timeout=3) as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(%s)",
                (upstream,),
            ).fetchall()
    except psycopg.Error:  # pragma: no cover - the no-Postgres run is already reported above
        return
    shadowing = sorted(str(row[0]) for row in rows)
    if not shadowing:
        return
    terminalreporter.write_sep("=", "`public` holds tables no migration creates", yellow=True)
    terminalreporter.write_line(
        f"{', '.join(shadowing)} exist in `public` on this database, and the isolation "
        "search_path falls through to it. A test that reads one of these unqualified without "
        "calling tests.pg.create_checkpoint_tables() passed here on rows CI will not have — its "
        "database has run migrations but never the agent. This run is not evidence about that "
        "case."
    )


# The reason `pytest.mark.skipif(shutil.which("helm") is None, ...)` puts on every rendered-chart
# test in `tests/test_deploy_chart.py`. Matched the same way, for the same reason: the number a
# reader needs is how many tests did not run, and only the run knows that.
_HELM_SKIP = "helm is not installed"


def _report_helm_skips(terminalreporter: TerminalReporter) -> None:
    """Say how many rendered-chart tests an absent `helm` binary took away.

    `helm` is not a Python dependency, so tests gated on `shutil.which("helm")` skip silently. A run
    that skips them is evidence only about the chart's static YAML. CI installs Helm, so this is a
    local-sandbox warning. Install: https://helm.sh/docs/intro/install/.
    """
    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if _HELM_SKIP in str(report.longrepr)
    ]
    if not skipped:
        return
    terminalreporter.write_sep("=", "Rendered-chart tests did not run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} tests were skipped because `helm` is not installed, so this run is not "
        "evidence about the rendered Helm chart — only about the chart's static YAML. Install "
        "helm (https://helm.sh/docs/intro/install/) to run them."
    )


def _report_slow_fork_skips(terminalreporter: TerminalReporter) -> None:
    """Say when a box's own process-creation cost took the parse-deadline tests away.

    `tests/test_parse_isolation.py` floors its budgets at one fork round trip; on a slow-forking
    machine the floor leaves no room, so those tests skip. Matched on the marker that module
    defines.
    """
    from tests.test_parse_isolation import _SLOW_FIXTURE_SKIP

    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if _SLOW_FIXTURE_SKIP in str(report.longrepr)
    ]
    if not skipped:
        return
    terminalreporter.write_sep("=", "Parse-deadline tests did not run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} tests were skipped because creating a process costs more here than the "
        "deadline they derive, so this run is not evidence that a parse past its deadline frees "
        "its upload slot — the wedge those tests regress against. CI's runner forks ~5x faster "
        "and runs them."
    )


# The marker `tests/temporal_env.py::start_env_or_skip` puts in its skip reason. Matched the same
# way, for the same reason: the number a reader needs is how many tests did not run.
_TEMPORAL_SKIP = "Temporal test server unavailable"


def _report_temporal_skips(terminalreporter: TerminalReporter) -> None:
    """The same warning for the Temporal test server.

    `start_env_or_skip` downloads the server binary on first use, so a network-restricted sandbox
    skips every test that drives a real workflow and still prints green — and workflow sequencing,
    idempotency and continue-as-new can only be observed against a server.
    """
    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if _TEMPORAL_SKIP in str(report.longrepr)
    ]
    if not skipped:
        return
    terminalreporter.write_sep("=", "Temporal-backed tests did not run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} tests were skipped because the Temporal test server could not start, so "
        "this run is not evidence about any durable workflow — the BO campaign's per-round record "
        "and resumption, the connector-job wrapper, or the report fan-out. The binary is fetched "
        "on first use and needs network egress."
    )


def _report_sibling_skips(terminalreporter: TerminalReporter) -> None:
    """Say plainly that the cross-repository checks did not run, and what that leaves unchecked.

    Without a `Chemclaw3-mcp` checkout, `tests/test_context_floor.py` (which bounds the half of the
    request prefix behind both compaction defaults) and `tests/test_sibling_manifest_agreement.py`
    skip. Matched on `tests/siblings.SIBLING_SKIP`.
    """
    from tests.siblings import SIBLING_SKIP

    skipped = [
        report
        for report in terminalreporter.stats.get("skipped", [])
        if SIBLING_SKIP in str(report.longrepr)
    ]
    if not skipped:
        return
    terminalreporter.write_sep("=", "Cross-repository checks did not run", yellow=True)
    terminalreporter.write_line(
        f"{len(skipped)} tests were skipped because Chemclaw3-mcp could not be read, so this run "
        "is not evidence about the half of the request prefix that fleet serves — the allowance "
        "PREFIX_BOUND is built from and both compaction defaults are derived from — nor about "
        "whether the two repositories still declare the same connector surface, or still agree "
        "about the tool names and argument keys on the `calc` and `rxnlabel` backend seams. "
        "Clone it beside this one, or set CHEMCLAW_MCP_REPO; where there is a checkout already, "
        "each skip above names the bundle it could not measure and why — a missing dependency in "
        "that tree's own `.venv` now costs that bundle's measurement and no other."
    )


#: Set where the sibling checkout *and* its environment are provisioned on purpose — CI's `check`
#: job — so a skip that `_report_sibling_skips` would only count is a failure instead.
SIBLINGS_REQUIRED = "CHEMCLAW_SIBLINGS_REQUIRED"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Fail a cross-repository check that skipped where its sibling was provisioned to be read.

    CI builds the sibling's environment, so a sibling skip there means a precondition broke and is
    reported as a failure. Matched on `tests/siblings.SIBLING_SKIP`.
    """
    report = yield
    if not report.skipped or os.environ.get(SIBLINGS_REQUIRED) != "1":
        return report
    from tests.siblings import SIBLING_SKIP

    if SIBLING_SKIP in str(report.longrepr):
        report.outcome = "failed"
        report.longrepr = (
            f"{SIBLINGS_REQUIRED}=1, and this cross-repository check skipped instead of running: "
            f"{report.longrepr}"
        )
    return report


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    """Say plainly which failures were timeouts, and how much of the suite never ran.

    A timed-out test proves nothing about assertions it never reached, unlike a failed assertion,
    and a skipped Postgres, Temporal, helm, sibling or slow-fork test proves nothing at all. Printed
    after the short summary, naming the knob that fixes it.
    """
    _report_postgres_skips(terminalreporter)
    _report_temporal_skips(terminalreporter)
    _report_helm_skips(terminalreporter)
    _report_public_schema_shadowing(terminalreporter)
    _report_sibling_skips(terminalreporter)
    _report_slow_fork_skips(terminalreporter)
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
