"""The turn's side of a queued tool call: start it, wait a little, and answer as the tool would.

Installed as a `langchain-mcp-adapters` tool interceptor on a connector whose manifest declares
`queued:` (`connectors/transport.py::_interceptors`), so every middleware sees an ordinary
connector call; only the last hop becomes a `QueuedToolWorkflow` on the interactive queue.

The agent always gets a `CallToolResult`: the server's answer if it arrived within
`queued.inline_wait_seconds`, a refusal as a refusal, otherwise a text naming the durable job
(announced with `job_started`, answered later as `job_completed`). An answer that waited for a
slot says so (`_with_wait`), since the `tool_queued` events never reach the model.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal

from langchain_mcp_adapters.interceptors import (
    MCPToolCallRequest,
    MCPToolCallResult,
    ToolCallInterceptor,
)
from mcp.types import CallToolResult, TextContent
from temporalio.api.enums.v1 import PendingActivityState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from chemclaw.agent.authz import require_actor
from chemclaw.connectors.manifest import QueuedDispatch
from chemclaw.connectors.queued_call import QueuedToolCall
from chemclaw.connectors.queued_workflow import RESULT_KEY, QueuedToolWorkflow
from chemclaw.connectors.queues import interactive_queue
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect
from chemclaw.core.turn_signals import record_job_started, record_tool_queued
from chemclaw.durable.connector_job import ConnectorJobResult, envelope_from_result, failure_reason

logger = logging.getLogger(__name__)

Handler = Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]]

# Last backlog read per interactive queue, as `(monotonic time, count)`: one broker read per queue
# per tick serves every waiting turn.
_BACKLOG: dict[str, tuple[float, int | None]] = {}
# Set once the broker answers a stats request without stats (too old a server); never asked again.
_STATS_UNSUPPORTED = False


class QueueUnavailable(Exception):
    """The queued run could not be started — so nothing was queued and the caller may go direct."""


def queued_workflow_id(connector: str, tool: str, arguments: dict[str, Any]) -> str:
    """The id identical queued calls share, so concurrent ones join one run.

    A function of the call alone, never of who asked, which is why only a tool whose answer depends
    only on its arguments may be queued (`manifest.QueuedDispatch`).
    """
    return f"queued-{connector}-{tool}-{stable_hash([connector, tool, arguments])}"


def queued_interceptor(
    connector: str, dispatch: QueuedDispatch, call_timeout: float
) -> ToolCallInterceptor:
    """The interceptor that routes `dispatch.tools` through the queue and leaves the rest alone."""
    queued = frozenset(dispatch.tools)

    async def intercept(request: MCPToolCallRequest, handler: Handler) -> MCPToolCallResult:
        if request.name not in queued:
            return await handler(request)
        try:
            return await dispatch_queued(
                connector,
                request.name,
                dict(request.args),
                inline_wait=dispatch.inline_wait_seconds,
                call_timeout=call_timeout,
            )
        except QueueUnavailable:
            # The queue must not take a capability down: nothing was started, so call the server
            # directly.
            # Counted, because a broker outage silently making every call direct is the load this
            # design avoids.
            logger.warning("queue unreachable; calling %s.%s directly", connector, request.name)
            record_metric(
                lambda m: m.increment(
                    "chemclaw_queued_tool_calls_direct_total", labels={"tool": request.name}
                )
            )
            return await handler(request)

    return intercept


async def dispatch_queued(
    connector: str, tool: str, arguments: dict[str, Any], *, inline_wait: float, call_timeout: float
) -> CallToolResult:
    """Queue one call and answer as the tool would, within `inline_wait` or as a job id.

    Args:
        connector: The connector serving `tool`.
        tool: The tool being called.
        arguments: Its arguments, as the model sent them.
        inline_wait: How long the turn waits for the answer before handing back a job id.
        call_timeout: How long the call may run once it has a slot.

    Returns:
        The server's `CallToolResult`, a refusal as `isError`, or a text naming the job.
    """
    call = QueuedToolCall(
        connector=connector,
        tool=tool,
        arguments=arguments,
        call_timeout_seconds=call_timeout,
        queue_timeout_seconds=settings.queued_tool_timeout_seconds,
    )
    session_id = get_current_session_id() or ""
    try:
        client = await connect()
        handle = await client.start_workflow(
            QueuedToolWorkflow.run,
            call,
            id=queued_workflow_id(connector, tool, arguments),
            task_queue=interactive_queue(connector),
            # An open run under this id is the identical call: join it. A closed one is history, and
            # a new ask
            # runs again (cheaply, behind `cached_compute`).
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            memo={
                "requested_by": require_actor(),
                "correlation_id": get_current_correlation_id() or "",
                "session_id": session_id,
            },
        )
    except (SubsystemUnavailableError, RPCError) as exc:
        raise QueueUnavailable(f"{connector}.{tool} could not be queued") from exc
    record_metric(lambda m: m.increment("chemclaw_queued_tool_calls_total", labels={"tool": tool}))
    waited: float | None = None
    try:
        finished, waited = await _wait_reporting(client, handle, connector, tool, inline_wait)
    except TimeoutError:
        if await _detach(handle, session_id):
            record_job_started(handle.id, tool)
            return _text(
                f"{tool} is waiting for a free slot on {connector!r} and did not finish within "
                f"{inline_wait:.0f} s. It keeps running as durable job {handle.id!r}: its result "
                "will be delivered to this conversation, and "
                f"get_durable_job_status({handle.id!r}) collects it."
            )
        # It finished between the wait running out and the signal: the answer is already there.
        try:
            finished = await handle.result()
        except WorkflowFailureError as exc:
            return _failed(tool, exc)
    except WorkflowFailureError as exc:
        return _failed(tool, exc)
    raw = envelope_from_result(handle.id, finished).data.get(RESULT_KEY)
    return _with_wait(CallToolResult.model_validate(raw), connector, waited)


def _with_wait(result: CallToolResult, connector: str, waited: float | None) -> CallToolResult:
    """`result` with one sentence saying the call waited for a slot, when it did.

    The `tool_queued` events reach only the chemist's stream, so without this the model can only
    guess
    about the queue. Appended as a separate block so the server's payload stays byte-for-byte
    intact,
    and the block starts with its own separator because readers join text blocks with `""`.
    """
    if waited is None:
        return result
    note = (
        f"\n\n(Queue: this call waited about {max(1, round(waited))} s for a free slot on "
        f"{connector!r} before it ran.)"
    )
    return result.model_copy(
        update={"content": [*result.content, TextContent(type="text", text=note)]}
    )


def _failed(tool: str, exc: WorkflowFailureError) -> CallToolResult:
    """A failed run as the tool's refusal, so the agent reads why rather than a traceback."""
    return _text(f"{tool} could not be run: {failure_reason(exc.__cause__ or exc)}", is_error=True)


async def _wait_reporting(
    client: Client,
    handle: WorkflowHandle[QueuedToolWorkflow, ConnectorJobResult],
    connector: str,
    tool: str,
    budget: float,
) -> tuple[ConnectorJobResult, float | None]:
    """The run's result within `budget` seconds, saying meanwhile whether it waits or runs.

    Every `queued_tool_progress_seconds` the run is asked where it is, and a `tool_queued` event
    goes
    out when the answer changes, so the card does not read "running" while the call is queued.
    Best-effort (`_progress`). Also returns how long the call was seen waiting, or `None` if no tick
    saw it queued.

    Raises:
        TimeoutError: `budget` ran out first; the run itself is untouched and keeps going.
    """
    loop = asyncio.get_running_loop()
    began = loop.time()
    deadline = began + budget
    result = asyncio.ensure_future(handle.result())
    reported: tuple[str, int | None] | None = None
    queued_until: float | None = None
    seen_queued = False

    def answered() -> tuple[ConnectorJobResult, float | None]:
        if not seen_queued:
            return result.result(), None
        return result.result(), (queued_until or loop.time()) - began

    try:
        while True:
            # The result first: a run that finished while the last progress read was out is an
            # answer, not a timeout.
            if result.done():
                return answered()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            tick = min(settings.queued_tool_progress_seconds, remaining)
            done, _ = await asyncio.wait({result}, timeout=tick)
            if done:
                return answered()
            # Bounded by the tick, so a slow broker delays the next look at the result by one tick
            # at most and never stretches the inline wait past its budget.
            progress = await _progress(
                client,
                handle,
                connector,
                max(0.1, min(tick, deadline - loop.time())),
                started=reported is not None and reported[0] == "running",
            )
            # A run that finished while the read was out is an answer; announcing it as waiting
            # one event before its result would be the false card this loop exists to prevent.
            if result.done():
                return answered()
            if progress is not None and progress[0] == "queued":
                seen_queued = True
            elif progress is not None and seen_queued and queued_until is None:
                queued_until = loop.time()
            if progress is not None and progress != reported:
                reported = progress
                record_tool_queued(tool, handle.id, progress[0], progress[1])
    finally:
        # Cancels the *waiter* only; the run carries on, which is what makes detaching safe.
        result.cancel()


async def _progress(
    client: Client,
    handle: WorkflowHandle[QueuedToolWorkflow, ConnectorJobResult],
    connector: str,
    rpc_timeout: float,
    *,
    started: bool = False,
) -> tuple[Literal["queued", "running"], int | None] | None:
    """Whether the run's call is still waiting for a slot, and how many calls wait with it.

    `running` once a worker started the activity; `queued` while it is scheduled but not started,
    including between retries. The count is the broker's approximate backlog on the interactive
    queue,
    this call included. With no pending activity, `started` decides: after the call was seen running
    it says nothing, before that it is queued (nothing has picked the run up) with an unknown count.
    `None` when the broker could not be asked; this must never fail the call.
    """
    try:
        description = await handle.describe(rpc_timeout=timedelta(seconds=rpc_timeout))
        pending = description.raw_description.pending_activities
        if not pending:
            return None if started else ("queued", None)
        if any(a.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED for a in pending):
            return "running", None
        return "queued", await _backlog(client, connector, rpc_timeout)
    except Exception:
        # Best-effort by contract (the docstring): an RPC fault or a describe racing completion
        # means "no annotation this tick", nothing more.
        logger.debug("could not read queue progress for %s", handle.id, exc_info=True)
        return None


async def _backlog(client: Client, connector: str, rpc_timeout: float) -> int | None:
    """The approximate backlog on `connector`'s interactive queue, read at most once a tick.

    `None` from a server that does not report task-queue stats; after the first such answer the
    process stops asking (`_STATS_UNSUPPORTED`). A failed read raises to `_progress`.
    """
    global _STATS_UNSUPPORTED
    if _STATS_UNSUPPORTED:
        return None
    queue_name = interactive_queue(connector)
    now = time.monotonic()
    cached = _BACKLOG.get(queue_name)
    if cached is not None and now - cached[0] < settings.queued_tool_progress_seconds:
        return cached[1]
    answer = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=queue_name),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
            report_stats=True,
        ),
        timeout=timedelta(seconds=rpc_timeout),
    )
    if not answer.HasField("stats"):
        _STATS_UNSUPPORTED = True
        return None
    count = int(answer.stats.approximate_backlog_count)
    _BACKLOG[queue_name] = (now, count)
    return count


async def _detach(
    handle: WorkflowHandle[QueuedToolWorkflow, ConnectorJobResult], session_id: str
) -> bool:
    """Ask the run to deliver its answer to this session; False if the run has already closed.

    Temporal processes a signal that races a completion, so "not found" means the answer exists.
    """
    try:
        await handle.signal(QueuedToolWorkflow.detach, session_id)
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


def _text(message: str, *, is_error: bool = False) -> CallToolResult:
    """A one-block `CallToolResult`, so the adapter converts it like any server answer."""
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=is_error)
