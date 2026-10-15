"""What a caller of `open_session` catches, against a real streamable-HTTP MCP server.

The SDK's client runs on task groups, which wrap whatever the caller's `async with` block raises in
an `ExceptionGroup`. Every caller classifies with plain `except` clauses (`calc_session`, the
labeller, the connector error sanitizer, Temporal's retry types), so these tests go through the
genuine `open_session` and a genuine server and assert each failure arrives as its own class.
"""

import asyncio
import dataclasses
import logging
import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, ClassVar

import anyio
import pytest
from chemclaw_contracts.calc import EmbedStructureRequest
from chemclaw_contracts.rxnlabel import LabellerVersionRequest
from mcp import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.connectors.calc.compose import _attempt
from chemclaw.connectors.calc.remote import (
    CalcBusyError,
    CalcServerError,
    CalcTimeBudgetError,
    CalcToolError,
    remote_call,
)
from chemclaw.connectors.server import _sanitize_tool_errors, connector_app
from chemclaw.core import mcp_session
from chemclaw.core.config import settings
from chemclaw.core.mcp_session import (
    McpAtCapacity,
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    McpTimeBudget,
    invoke,
    open_session,
)
from chemclaw.durable.registry import ActivityClass
from chemclaw.durable.retry import policy_for
from chemclaw.ingest.labels.labeller import LabelServerError, LabelToolError, RxnLabelServer
from tests.conftest import _free_port
from tests.served import ServerThread
from tests.temporal_env import start_local_env_or_skip

_TOKEN_ENV = "CHEMCLAW_TEST_MCP_SESSION_TOKEN"
_TOKEN = "session-test-token"

#: What the probe server does on the next call; set per test by `served`.
_MODE: dict[str, str] = {"mode": "ok"}


def _behave() -> dict[str, Any]:
    """Do what `_MODE` says, raising the way a fleet server raises that outcome."""
    mode = _MODE["mode"]
    if mode == "at_capacity":
        raise CalcBusyError("the pod is full")
    if mode == "time_budget":
        raise ValueError("[calc-time-budget] stopped after 780 s")
    if mode == "refusal":
        raise ValueError("bad smiles 'C(' (unbalanced parenthesis)")
    if mode == "fault":
        raise RuntimeError("postgres://secret@internal/db is down")
    return {"ok": True, "version": "probe-1"}


def _probe_server() -> FastMCP:
    """A server whose tools are the wire names `remote_call` and the labeller send."""
    server = FastMCP("mcp-session-probe")

    @server.tool(name=EmbedStructureRequest.tool_name)
    async def embed_structure(smiles: str) -> dict[str, Any]:
        """Behave per `_MODE`."""
        del smiles
        return _behave()

    @server.tool(name=LabellerVersionRequest.tool_name)
    async def labeller_version() -> dict[str, Any]:
        """Behave per `_MODE`."""
        return _behave()

    return server


@pytest.fixture(scope="module")
def probe_url() -> Iterator[str]:
    """A real uvicorn server on a loopback port, speaking MCP over streamable HTTP with a bearer."""
    patch = pytest.MonkeyPatch()
    patch.setenv(_TOKEN_ENV, _TOKEN)
    port = _free_port()
    app = connector_app(_probe_server(), name="mcp-session-probe", token_env=_TOKEN_ENV)
    try:
        with ServerThread(app, port):
            yield f"http://127.0.0.1:{port}/mcp"
    finally:
        patch.undo()


@pytest.fixture
def served(probe_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """`probe_url`, with the calculation and labelling clients pointed at it and the mode reset."""
    for prefix in ("calc", "rxnlabel"):
        monkeypatch.setattr(settings, f"{prefix}_server_url", probe_url)
        monkeypatch.setattr(settings, f"{prefix}_server_token_env", _TOKEN_ENV)
    _MODE["mode"] = "ok"
    yield probe_url
    _MODE["mode"] = "ok"


def _open(url: str, **kwargs: Any) -> Any:
    """`open_session` with this suite's bearer and a bound that is never what a test waits on."""
    return open_session(url, token_env=_TOKEN_ENV, timeout_seconds=10, **kwargs)


# --- the raised exception arrives as itself ------------------------------------------------------


def test_the_exception_the_block_raised_arrives_as_that_object(served: str) -> None:
    """The caller gets back the very object it raised, with its chain and traceback intact."""
    root = KeyError("the cause")
    raised = ValueError("raised in the block")

    async def go() -> BaseException:
        try:
            async with _open(served) as session:
                assert session is not None
                try:
                    raise root
                except KeyError:
                    raise raised from root
        except BaseException as exc:
            return exc
        raise AssertionError("the block's exception was swallowed")

    caught = asyncio.run(go())

    assert caught is raised
    assert caught.__cause__ is root
    assert not isinstance(caught, BaseExceptionGroup)
    frames = []
    tb = caught.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    assert "go" in frames, frames


def test_a_group_the_caller_raised_on_purpose_is_not_unpacked(served: str) -> None:
    """Only the SDK's wrapping is removed: a caller's own `ExceptionGroup` stays a group."""
    deliberate = ExceptionGroup("two items failed", [ValueError("a"), ValueError("b")])

    async def go() -> BaseException:
        try:
            async with _open(served):
                raise deliberate
        except BaseException as exc:
            return exc
        raise AssertionError("swallowed")

    assert asyncio.run(go()) is deliberate


def test_a_cancelled_block_is_still_a_cancellation(served: str) -> None:
    """Cancelling the task that holds the session ends it cancelled, not as a raised error."""

    async def go() -> bool:
        held = asyncio.Event()

        async def hold() -> None:
            async with _open(served):
                held.set()
                await asyncio.sleep(30)

        task = asyncio.ensure_future(hold())
        await asyncio.wait_for(held.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(go()) is True


def test_an_interpreter_exit_in_the_block_is_not_converted(served: str) -> None:
    """`KeyboardInterrupt` leaves as itself, and is never taken for a connection failure."""

    async def go() -> None:
        async with _open(served):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(go())


def test_a_connection_that_cannot_be_made_is_still_a_connect_failure() -> None:
    """Nothing listening: `McpConnectFailed`, whose cause is the SDK's group, as before."""
    url = f"http://127.0.0.1:{_free_port()}/mcp"

    async def go() -> None:
        async with _open(url):
            raise AssertionError("connected to nothing")  # pragma: no cover

    with pytest.raises(McpConnectFailed) as caught:
        asyncio.run(go())
    assert isinstance(caught.value.__cause__, BaseExceptionGroup)


def test_a_refused_credential_is_still_named_as_one(served: str) -> None:
    """A wrong bearer is `McpCredentialRefused`, not an outage and not the block's concern."""

    async def go() -> None:
        async with open_session(
            served, token_env="CHEMCLAW_TEST_UNSET_VARIABLE", timeout_seconds=5
        ):
            raise AssertionError("connected without a credential")  # pragma: no cover

    with pytest.raises(McpCredentialRefused) as caught:
        asyncio.run(go())
    assert caught.value.status == 401


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("at_capacity", McpAtCapacity),
        ("time_budget", McpTimeBudget),
        ("refusal", McpRequestRefused),
        ("fault", McpServerFault),
    ],
)
def test_invoke_failures_reach_the_caller_as_their_own_class(
    served: str, mode: str, expected: type[Exception]
) -> None:
    """Each server outcome, raised by `invoke` in the block, is the exact class `invoke` chose."""
    _MODE["mode"] = mode

    async def go() -> None:
        async with _open(served) as session:
            await invoke(session, EmbedStructureRequest.tool_name, {"smiles": "C"})

    with pytest.raises(expected) as caught:
        asyncio.run(go())
    assert type(caught.value) is expected


# --- the same, through each production caller ----------------------------------------------------

_CALC_OUTCOMES: list[tuple[str, type[Exception], type[Exception]]] = [
    # mode, the class `remote_call` raises, the class `invoke` raised underneath it
    ("at_capacity", CalcBusyError, McpAtCapacity),
    ("time_budget", CalcTimeBudgetError, McpTimeBudget),
    ("refusal", CalcToolError, McpRequestRefused),
    ("fault", CalcServerError, McpServerFault),
]


@pytest.mark.parametrize(("mode", "expected", "underneath"), _CALC_OUTCOMES)
def test_remote_call_names_each_outcome_by_its_own_class(
    served: str, mode: str, expected: type[Exception], underneath: type[Exception]
) -> None:
    """The calculation client's `except` clauses are reached for errors raised in its block."""
    _MODE["mode"] = mode

    with pytest.raises(expected) as caught:
        asyncio.run(remote_call(EmbedStructureRequest(smiles="C")))

    assert type(caught.value) is expected
    assert type(caught.value.__cause__) is underneath
    if mode == "refusal":
        assert "bad smiles" in str(caught.value)


def test_remote_call_still_answers_when_nothing_goes_wrong(served: str) -> None:
    """The control: the same path returns the payload."""
    assert asyncio.run(remote_call(EmbedStructureRequest(smiles="C"))) == {
        "ok": True,
        "version": "probe-1",
    }


def test_remote_call_names_an_unreachable_server_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead port is `CalcServerError` (retryable), caused by `McpConnectFailed`."""
    monkeypatch.setattr(settings, "calc_server_url", f"http://127.0.0.1:{_free_port()}/mcp")

    with pytest.raises(CalcServerError) as caught:
        asyncio.run(remote_call(EmbedStructureRequest(smiles="C")))
    assert isinstance(caught.value.__cause__, McpConnectFailed)


def test_remote_call_names_a_refused_credential_bad_data(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected bearer is `CalcToolError`: it never recovers on retry."""
    monkeypatch.setattr(settings, "calc_server_token_env", "CHEMCLAW_TEST_UNSET_VARIABLE")

    with pytest.raises(CalcToolError) as caught:
        asyncio.run(remote_call(EmbedStructureRequest(smiles="C")))
    assert isinstance(caught.value.__cause__, McpCredentialRefused)


@pytest.mark.parametrize(
    ("mode", "expected", "underneath"),
    [
        # A full pod ran nothing and the drain asks again, so it is the retryable class.
        ("at_capacity", LabelServerError, McpAtCapacity),
        ("time_budget", LabelToolError, McpTimeBudget),
        ("refusal", LabelToolError, McpRequestRefused),
        ("fault", LabelServerError, McpServerFault),
    ],
)
def test_the_labeller_reaches_its_refusal_and_fault_branches(
    served: str, mode: str, expected: type[Exception], underneath: type[Exception]
) -> None:
    """The labeller's `except Mcp...` clauses, dead for in-block errors behind the group."""
    _MODE["mode"] = mode

    with pytest.raises(expected) as caught:
        asyncio.run(RxnLabelServer().version())

    assert type(caught.value) is expected
    assert type(caught.value.__cause__) is underneath


def test_the_labeller_names_an_unreachable_server_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead port is `LabelServerError`, the retryable class."""
    monkeypatch.setattr(settings, "rxnlabel_server_url", f"http://127.0.0.1:{_free_port()}/mcp")

    with pytest.raises(LabelServerError) as caught:
        asyncio.run(RxnLabelServer().version())
    assert isinstance(caught.value.__cause__, McpConnectFailed)


def _sanitized_calc_connector() -> FastMCP:
    """A connector tool that calls the calculation server, behind the production sanitizer."""
    connector = FastMCP("calc-connector")

    @connector.tool()
    async def embed(smiles: str) -> dict[str, Any]:
        """Embed one structure on the calculation server."""
        return await remote_call(EmbedStructureRequest(smiles=smiles))

    _sanitize_tool_errors(connector, name="calc")
    return connector


@pytest.mark.parametrize(
    ("mode", "must_say", "must_not_say"),
    [
        # Worded, and the marker first, so the queued-call path can tell busy from broken.
        (
            "at_capacity",
            "Error executing tool embed: [calc-at-capacity] the calculation service",
            "",
        ),
        # A deliberate refusal passes through with the server's text.
        ("time_budget", "[calc-time-budget]", "internal error"),
        ("refusal", "bad smiles", "internal error"),
        # An outage is not a `ValueError`, so the sanitizer's opaque notice is the sanctioned text.
        ("fault", "an internal error occurred", "postgres://"),
    ],
)
def test_the_connector_sanitizer_sees_the_worded_error_not_a_group(
    served: str, mode: str, must_say: str, must_not_say: str
) -> None:
    """`_sanitize_tool_errors` reads `__cause__`, which a wrapped group made the opaque notice."""
    _MODE["mode"] = mode
    connector = _sanitized_calc_connector()

    async def go() -> str:
        with pytest.raises(ToolError) as caught:
            await connector._tool_manager.call_tool("embed", {"smiles": "C"}, convert_result=True)
        return str(caught.value)

    text = asyncio.run(go())

    assert must_say in text, text
    if must_not_say:
        assert must_not_say not in text, text


def test_a_screen_records_a_refused_item_instead_of_failing_the_activity(served: str) -> None:
    """`compose._attempt` catches `ValueError`; a refusal now reaches it as one."""
    _MODE["mode"] = "refusal"

    outcome = asyncio.run(_attempt(remote_call(EmbedStructureRequest(smiles="C("))))

    assert type(outcome) is CalcToolError


# --- what Temporal sees --------------------------------------------------------------------------


@dataclasses.dataclass
class _Calls:
    """How many times the activity body ran."""

    count: int = 0


_CALLS = _Calls()


@activity.defn(name="mcp-session-test-embed")
async def _embed_activity(smiles: str) -> dict[str, Any]:
    """A durable activity that does what `cached_remote`/`remote_call` callers do."""
    _CALLS.count += 1
    return await remote_call(EmbedStructureRequest(smiles=smiles))


@workflow.defn(name="McpSessionTestWorkflow")
class _EmbedWorkflow:
    """One activity under the calculation backend's real retry policy, with the waits shortened."""

    retry: ClassVar[RetryPolicy]

    @workflow.run
    async def run(self, smiles: str) -> dict[str, Any]:
        """Dispatch the activity with the class-level policy."""
        return await workflow.execute_activity(  # type: ignore[no-any-return]
            "mcp-session-test-embed",
            smiles,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=self.retry,
        )


def _fast_calculation_retry() -> RetryPolicy:
    """The calculation class's policy (attempts and bad-data list), with its backoff in ms."""
    real = policy_for(ActivityClass.CALC)
    return RetryPolicy(
        maximum_attempts=real.maximum_attempts,
        non_retryable_error_types=real.non_retryable_error_types,
        initial_interval=timedelta(milliseconds=20),
        maximum_interval=timedelta(milliseconds=20),
        backoff_coefficient=1.0,
    )


async def _run_activity(client: Client) -> ActivityError:
    """Run the workflow once and return the activity failure it ended with."""
    _EmbedWorkflow.retry = _fast_calculation_retry()
    async with Worker(
        client,
        task_queue="mcp-session-errors",
        workflows=[_EmbedWorkflow],
        activities=[_embed_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        _CALLS.count = 0
        with pytest.raises(WorkflowFailureError) as caught:
            await client.execute_workflow(
                _EmbedWorkflow.run,
                "C",
                id=f"mcp-session-errors-{os.urandom(4).hex()}",
                task_queue="mcp-session-errors",
            )
    failure = caught.value.cause
    assert isinstance(failure, ActivityError), failure
    return failure


@pytest.mark.parametrize(
    ("mode", "failure_type", "attempts"),
    [
        # Bad data: the bare class name is on the policy's non-retryable list, so it runs once.
        ("refusal", "CalcToolError", 1),
        # Re-running re-burns the same wall clock; registered by its own name.
        ("time_budget", "CalcTimeBudgetError", 1),
        # The control: an outage is retried to the attempt budget.
        ("fault", "CalcServerError", settings.activity_max_attempts),
    ],
)
def test_temporal_sees_the_bare_class_and_retries_only_what_it_should(
    served: str, mode: str, failure_type: str, attempts: int
) -> None:
    """Failure type, retryability and attempt count of a real activity under the real policy."""
    _MODE["mode"] = mode

    async def go() -> ActivityError:
        env = await start_local_env_or_skip()
        try:
            return await _run_activity(env.client)
        finally:
            await env.shutdown()

    failure = asyncio.run(go())

    assert isinstance(failure.cause, ApplicationError)
    assert failure.cause.type == failure_type
    # Temporal decides by this name against the policy's list, so both halves are asserted: the
    # name is (or is not) listed, and the attempt count shows the server acting on it.
    listed = failure_type in (policy_for(ActivityClass.CALC).non_retryable_error_types or [])
    assert listed is (attempts == 1)
    assert _CALLS.count == attempts


# --- cancellation, timeouts and failures of the SDK's own tasks -----------------------------------


def _teardown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the real `ClientSession` fail on the way out, after it has shut itself down."""
    original = ClientSession.__aexit__

    async def failing_exit(self: ClientSession, *exc: Any) -> bool | None:
        try:
            await original(self, *exc)
        finally:
            raise OSError("the transport failed while closing")

    monkeypatch.setattr(ClientSession, "__aexit__", failing_exit)


def test_a_failure_while_closing_after_a_good_block_arrives_bare(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one teardown failure is unwrapped from the SDK's group, since nothing else competes."""
    _teardown_raises(monkeypatch)

    async def go() -> None:
        async with _open(served):
            pass

    with pytest.raises(OSError, match="while closing") as caught:
        asyncio.run(go())
    assert type(caught.value) is OSError


def test_an_outer_asyncio_timeout_still_reports_a_timeout(served: str) -> None:
    """`asyncio.timeout` around the session ends in `TimeoutError`, not a group or a cancel."""

    async def go() -> None:
        async with asyncio.timeout(0.5), _open(served):
            await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        asyncio.run(go())


def test_an_outer_anyio_deadline_still_reports_a_timeout(served: str) -> None:
    """`anyio.fail_after` around the session ends in `TimeoutError`."""

    async def go() -> None:
        with anyio.fail_after(0.5):
            async with _open(served):
                await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        asyncio.run(go())


def test_an_outer_timeout_wins_over_a_failure_while_closing(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled from outside while the transport also fails: the timeout is what is reported."""
    _teardown_raises(monkeypatch)

    async def go() -> None:
        async with asyncio.timeout(0.5), _open(served):
            await asyncio.sleep(30)

    with pytest.raises(TimeoutError):
        asyncio.run(go())


def test_a_transport_task_crashing_surfaces_the_crash_not_a_cancellation(
    served: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash in one of the SDK's tasks cancels the block; the caller is told the crash."""
    in_block = asyncio.Event()
    original = ClientSession._receive_loop

    async def crashing_loop(self: ClientSession) -> None:
        reading = asyncio.ensure_future(original(self))
        try:
            await in_block.wait()
            raise OSError("the receive loop died")
        finally:
            reading.cancel()

    monkeypatch.setattr(ClientSession, "_receive_loop", crashing_loop)

    async def go() -> None:
        async with _open(served):
            in_block.set()
            await asyncio.sleep(30)

    with pytest.raises(OSError, match="receive loop died") as caught:
        asyncio.run(go())
    assert type(caught.value) is OSError


# --- the unwrapping rule on its own --------------------------------------------------------------


def test_a_teardown_failure_in_one_group_is_surfaced_as_its_single_leaf() -> None:
    """With no error from the block, one `Exception` leaf, however deep, is what is raised."""
    leaf = OSError("transport closed")
    wrapped = ExceptionGroup("outer", [ExceptionGroup("inner", [leaf])])

    assert mcp_session._surfaced(wrapped, None) is leaf


def test_several_teardown_failures_stay_a_group() -> None:
    """Nothing says which of two unrelated failures the caller meant."""
    group = ExceptionGroup("outer", [OSError("a"), OSError("b")])

    assert mcp_session._surfaced(group, None) is group


def test_a_group_of_cancellations_is_left_alone() -> None:
    """A `BaseExceptionGroup` holding only cancellation is not turned into an ordinary error."""
    group = BaseExceptionGroup("cancelled", [asyncio.CancelledError()])

    assert mcp_session._surfaced(group, None) is group


def test_the_blocks_own_error_wins_over_what_teardown_added(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The block's own error is returned when teardown raised beside it; the rest is logged."""
    own = ValueError("raised by the caller")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [own]), OSError("teardown")])

    with caplog.at_level(logging.WARNING, logger="chemclaw.core.mcp_session"):
        assert mcp_session._surfaced(group, own) is own
    assert any("teardown raised 1 further" in record.getMessage() for record in caplog.records)


def test_a_cancellation_raised_by_the_block_is_returned_as_that_cancellation() -> None:
    """The block's `CancelledError`, found inside a mixed group, comes back bare."""
    own = asyncio.CancelledError()
    group = BaseExceptionGroup("mixed", [own, OSError("teardown")])

    assert mcp_session._surfaced(group, own) is own


def test_an_interpreter_exit_beside_the_blocks_error_keeps_the_group() -> None:
    """A `KeyboardInterrupt` from another task is not buried under the block's own error."""
    own = ValueError("raised by the caller")
    group = BaseExceptionGroup("g", [own, KeyboardInterrupt()])

    assert mcp_session._surfaced(group, own) is group


def test_a_pending_outside_cancellation_is_kept_over_a_lone_teardown_error() -> None:
    """Only a cancellation requested from outside outranks the teardown error; a crash does not."""
    leaf = OSError("teardown")
    group = ExceptionGroup("outer", [leaf])
    cancelled = asyncio.CancelledError()

    async def judged(cancel_first: bool) -> BaseException:
        started = asyncio.Event()

        async def inner() -> BaseException:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return mcp_session._surfaced(group, cancelled)
            raise AssertionError("not cancelled")  # pragma: no cover

        task = asyncio.ensure_future(inner())
        await started.wait()
        if cancel_first:
            task.cancel()
        else:
            # What anyio's own scope leaves behind: the block saw a cancel, nobody outside asked.
            task.cancel()
            task.uncancel()
        return await task

    assert asyncio.run(judged(True)) is cancelled
    assert asyncio.run(judged(False)) is leaf
