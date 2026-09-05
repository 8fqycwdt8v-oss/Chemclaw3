"""Shared pytest fixtures and test fakes.

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

`pytest_terminal_summary` owns the qualifications a run's headline number needs: which failures
were wall-clock timeouts, and how many tests an unreachable Postgres, an unstartable Temporal test
server, or an absent `helm` binary took away.
"""

import asyncio
import os
import socket
from collections.abc import Iterator

import psycopg
import pytest
from _pytest.config import UsageError
from _pytest.terminal import TerminalReporter

from chemclaw.agent.authz import side_effecting_tools as _side_effecting_tools
from chemclaw.connectors.reachability import forget_reachability as _forget_reachability
from chemclaw.connectors.registry import discovered as _connectors_discovered
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import _REGISTRY as _TOOL_REGISTRY
from chemclaw.ingest.eln.warehouse.connect import forget_open_warehouses as _forget_warehouses
from chemclaw.ingest.sources.registry import discovered as _sources_discovered
from chemclaw.kg.submission import NoteSubmission, SubmissionOutcome
from chemclaw.retrieval.vectors.registry import forget_vector_store as _forget_vector_store
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

    async def submit(self, submission: NoteSubmission) -> SubmissionOutcome:
        """Capture the submission and return a fake PR reference."""
        self.submissions.append(submission)
        return SubmissionOutcome(reference=f"pr://{submission.branch}")


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
def _fresh_discovery_caches() -> Iterator[None]:
    """Clear the connector, template and data-source `@cache`d discovery registries per test.

    `chemclaw.connectors.registry.discovered`, `chemclaw.templates.registry.discovered` and
    `chemclaw.ingest.sources.registry.discovered` are `@cache`d for production, where the
    bundle/template/source layout is fixed for the process's life.
    Most of the suite calls them expecting the real, on-disk default; a handful of tests repoint
    `connectors_dir` / `templates_dir` at a `tmp_path` fixture bundle instead. `monkeypatch`
    restores the setting afterwards, but it has no idea a `functools.cache` sits downstream, so a
    test that forgot to clear it left the *next* test reading a stale or `tmp_path`-only result —
    order-dependent failures in `test_agent.py` and `test_prose_contract.py` traced to exactly
    this (`docs/planning/BACKLOG.md`). Clearing both directions, autouse, turns "remember to clear
    the cache" from a per-file convention every new test has to rediscover into an invariant nothing
    can forget — and it is cheap: clearing an empty `functools.cache` is O(1).

    **The data-source registry is the third of the same kind and was the one not here**, cleared
    instead by a per-file autouse fixture in `tests/test_datasource_seam.py`. That worked for as
    long as every test repointing `data_sources_dir` lived in that file, and stopped the moment one
    did not: a single test elsewhere pointing the registry at its own `tmp_path` manifests poisoned
    the cache for the rest of the session, and 50 tests in four unrelated files failed reading a
    corpus of one fixture source. Which is precisely the failure the docstring above already
    describes, in the one registry it did not cover.
    """
    _connectors_discovered.cache_clear()
    _templates_discovered.cache_clear()
    _sources_discovered.cache_clear()
    # Derived from the first two, so it goes stale exactly when they do — a repointed
    # `connectors_dir` with this cache still warm would leave the write gates reading the old
    # deployment's classification.
    _side_effecting_tools.cache_clear()
    yield
    _connectors_discovered.cache_clear()
    _templates_discovered.cache_clear()
    _sources_discovered.cache_clear()
    _side_effecting_tools.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_generated_tools() -> Iterator[None]:
    """Restore `core.tool_registry` to its import-time contents after every test.

    **A fourth process-global of exactly the kind the fixture above argues about, and it was not
    covered.** `_REGISTRY` is populated two ways: by `@tool` at import of `agent/tool_modules.py`,
    which is fixed for the process's life and must stay; and by `_register_generated_tools`, which
    adds one launcher per enabled durable job and per enabled step template *at call time*, because
    which of those are enabled is a deployment's choice. The second kind is the leak — a test that
    calls `_capability_tools()` or `template_tools()` leaves 23 launchers behind for the rest of
    the session, and the next test asking "is this name taken?" gets a different answer than it
    would alone.

    That is not hypothetical: `test_a_connector_cannot_claim_a_template_launcher_name_on_the_first
    _build` exists to prove the collision guard works on a process's *first* agent build, which is
    the state where no launcher is registered. It passed run alone and failed in the full suite,
    on its own precondition assertion — the test correctly refusing to be evidence about ordering
    while something upstream had already done the registering.

    Snapshot-and-restore rather than `clear()`, because the import-time half is what the whole of
    `agent/tool_modules.py` exists to guarantee and dropping it would serve zero tools.
    """
    before = dict(_TOOL_REGISTRY)
    yield
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(before)


@pytest.fixture(autouse=True)
def _fresh_connector_reachability() -> Iterator[None]:
    """Forget the per-process connector reachability verdicts around every test.

    `chemclaw.connectors.reachability` is what stops a turn dialling a connector this process just
    found unreachable (`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`), and it is
    module state for the same reason the pools and discovery caches are: it belongs to the process,
    not to a request. In a test session that is exactly the hazard the two fixtures above exist for
    — one test's dark connector would silently stop the *next* test's open from dialling at all, and
    the failure would be order-dependent.
    """
    _forget_reachability()
    yield
    _forget_reachability()


@pytest.fixture(autouse=True)
def _fresh_attached_connections() -> Iterator[None]:
    """Forget the process-lived warehouse connections and vector store around every test.

    Both are memoised in production for the reason their protocols already assume: neither has a
    `close`, and the data-source seam builds a retrieve half per `gather_evidence` call, so a store
    or a session built per call is one nothing can ever dispose. A remembered handle is exactly
    wrong in a test session, though — `warehouse_fake.prime()` installs a new fake per test, and a
    cached connection would serve every later test the *first* test's rows.

    Autouse for `_fresh_discovery_caches`'s reason: "clear the cache" as a per-file convention is
    something each new test file has to rediscover, and the failure it produces is order-dependent.
    """
    _forget_warehouses()
    _forget_vector_store()
    yield
    _forget_warehouses()
    _forget_vector_store()


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


# The `tests/temporal_env.py` helpers whose presence in a module means "this can wait on a
# broker". Named as a tuple so adding a third starter is one entry rather than a second
# `hasattr` somebody has to remember.
_ENV_STARTERS = ("start_env_or_skip", "start_local_env_or_skip")


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
    written: importing one of `tests/temporal_env.py`'s starters is what makes a module able to
    hang this way. **Both** of them — the real-time `start_local_env_or_skip` was added for the
    tests that drive terminate, eviction and an unserved queue, and those are precisely the ones
    that can wait forever on a broker, so a check naming only the time-skipping starter would have
    left the newest hang-capable module uncovered.

    `PYTEST_TIMEOUT_SCALE` is applied last, after that marker exists, so the scaled replacement
    can carry `method="thread"` forward.
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
    """Say how many tests the unreachable database took away, because prose kept getting it wrong.

    A run with no Postgres skips the whole durable layer — the session store, the note-proposal
    tables, retention, the outbox — and still prints a green line, which reads as "the suite
    passed" and means "the suite mostly did not run". `CLAUDE.md` warns about exactly this and
    stated the size of it as a number, which went stale by ~38% in the direction that understates
    the risk (it said ~157; the measured figure was 216). A count in prose describes the suite on
    the day someone counted; this one is measured by the run that is reporting it, which is the
    rule `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` reached for the eight other
    counts that were wrong.
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

    The isolation DSN is `search_path=<test schema>,public` — `public` second, because the
    `vector` extension is installed once per database and the type has to stay resolvable
    (`tests/pg.py::schema_dsn`). That fallthrough is what keeps the suite runnable; it is also a
    way for a test to pass on evidence the runner will not have.

    The tables it can happen with are exactly the ones **no migration creates**:
    `AsyncPostgresSaver.setup()` and `AsyncPostgresStore.setup()` make them the first time the
    agent runs, so a dev database that has held a conversation has them in `public` and CI's
    throwaway container never does. A test that reads `checkpoints` unqualified without creating
    it therefore passes locally and fails on the runner, on identical code — measured, three of
    `tests/test_api_sessions.py`'s delete tests did exactly that, and the local run that cleared
    them reported 5422 passed. `tests/pg.py::create_checkpoint_tables` is the fix for a test that
    needs them; this section is what makes their *absence* in CI visible from a dev run.

    Reported rather than enforced, and reported as a qualification rather than a failure: having
    run the agent against your own database is not a mistake, and dropping the tables would break
    the next `make chat`. What is a mistake is reading a green line as evidence about a database
    that has never run it — the same misreading `_report_postgres_skips` exists for.
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

    `helm` is not a Python dependency, so a plain `uv sync` never installs it, and the 33 tests
    gated on `shutil.which("helm")` skip silently and still print a green line — the same failure
    `_report_postgres_skips` exists for, on a second dependency that had no such warning. It is not
    a hypothetical: rendering the chart is what found five HIGH-severity chart defects that had
    survived earlier reviews precisely because nobody had rendered it, and a local run that skips
    these 33 is not evidence any of that class of defect is still absent. Install it:
    https://helm.sh/docs/intro/install/, or run `make helm-validate` once it is on `PATH`.
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


# The marker `tests/temporal_env.py::start_env_or_skip` puts in its skip reason. Matched the same
# way, for the same reason: the number a reader needs is how many tests did not run.
_TEMPORAL_SKIP = "Temporal test server unavailable"


def _report_temporal_skips(terminalreporter: TerminalReporter) -> None:
    """The same warning for the other backend a green line can be silent about.

    `start_env_or_skip` downloads the time-skipping server's binary on first use, so a
    network-restricted sandbox skips every test that drives a *real workflow* — the durable BO
    campaign and its resumption, the connector-job wrapper, the report fan-out — and prints green.
    That is the `_report_postgres_skips` failure exactly, on a second backend that had no such
    warning: the Postgres half of this file exists because a count in prose went stale by 38%,
    while the Temporal half of the same risk was reported by nothing at all.

    It matters most for the tests that are hardest to replace: a workflow's sequencing, its
    idempotency keys and its continue-as-new can only be observed against a server, so a suite that
    skips them silently is not evidence about durability in the one place durability lives.
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


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    """Say plainly which failures were timeouts, and how much of the suite never ran.

    Every section is about the same misreading: a run's headline number is believed without the
    things that qualify it. A timed-out test proves nothing about the assertions it never
    reached, and a skipped Postgres, Temporal or helm test proves nothing at all — see
    `_report_postgres_skips`, `_report_temporal_skips` and `_report_helm_skips`.

    `FAILED tests/test_pka.py::test_… - Failed: Timeout (>180.0s) from pytest-timeout` in the
    short summary was read as a numerical failure by two separate reviewers of this repository, and
    the mistake propagated into a campaign's baseline. The difference matters more than its
    wording suggests: a timed-out test proves *nothing* about the assertions it never reached, so
    it is not evidence either way, whereas a failed assertion is a finding.

    Printed as its own section, after the short summary, naming the knob that fixes it.
    """
    _report_postgres_skips(terminalreporter)
    _report_temporal_skips(terminalreporter)
    _report_helm_skips(terminalreporter)
    _report_public_schema_shadowing(terminalreporter)
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
