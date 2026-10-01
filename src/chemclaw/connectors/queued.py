"""The turn's side of a queued tool call: start it, wait a little, and answer as the tool would.

Installed as a `langchain-mcp-adapters` tool interceptor on a connector whose manifest declares
`queued:` (`connectors/transport.py::_interceptors`), so the tool the agent binds is the adapter's
own object and every middleware — authorization, audit, the plan gate, result framing — sees an
ordinary connector call. Only the last hop differs: instead of `session.call_tool`, the call becomes
a `QueuedToolWorkflow` on the connector's interactive queue, and this waits for its answer.

**What the agent gets back is a `CallToolResult`, whichever way it went**, so the adapter converts
it with its own function: the server's answer when the call finished inside
`queued.inline_wait_seconds`, a refusal as a refusal, and otherwise a short text naming the durable
job the call became. That last case is announced with `job_started`, and the answer arrives through
the session mailbox as `job_completed` — the path every durable job already takes.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
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


class QueueUnavailable(Exception):
    """The queued run could not be started — so nothing was queued and the caller may go direct."""


def queued_workflow_id(connector: str, tool: str, arguments: dict[str, Any]) -> str:
    """The id identical queued calls share, so concurrent ones join one run.

    A function of the call alone — never of who asked — which is why only a tool whose answer is a
    function of its arguments may be queued (`manifest.QueuedDispatch`).
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
            # **The queue must not become the reason a capability is down.** Nothing was started,
            # so the call goes the way it went before queues existed: straight to the server, which
            # admits it or says it is full. Counted, because a broker outage that quietly turns
            # every queued call direct is the load shape this design exists to prevent.
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
            # An open run under this id is the identical call already waiting or running: join
            # it. A closed one is history, and a new ask computes again (behind `cached_compute`,
            # cheaply).
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
    try:
        finished = await _wait_reporting(client, handle, connector, tool, inline_wait)
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
        finished = await handle.result()
    except WorkflowFailureError as exc:
        return _text(
            f"{tool} could not be run: {failure_reason(exc.__cause__ or exc)}", is_error=True
        )
    raw = envelope_from_result(handle.id, finished).data.get(RESULT_KEY)
    return CallToolResult.model_validate(raw)


async def _wait_reporting(
    client: Client,
    handle: WorkflowHandle[QueuedToolWorkflow, ConnectorJobResult],
    connector: str,
    tool: str,
    budget: float,
) -> ConnectorJobResult:
    """The run's result within `budget` seconds, saying meanwhile whether it waits or runs.

    Without this the tool-call card reads "running" for the whole wait, which is false while the
    call sits in the queue — and on a busy deployment that wait is the part a chemist is watching.
    So every `queued_tool_progress_seconds` the run is asked where it is, and a `tool_queued` event
    goes out only when the answer changes. The asking is best-effort (`_progress`): a broker that
    will not answer costs the card its annotation, never the call its result.

    Raises:
        TimeoutError: `budget` ran out first; the run itself is untouched and keeps going.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    result = asyncio.ensure_future(handle.result())
    reported: tuple[str, int | None] | None = None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait(
                {result}, timeout=min(settings.queued_tool_progress_seconds, remaining)
            )
            if done:
                return result.result()
            progress = await _progress(client, handle, connector)
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
) -> tuple[Literal["queued", "running"], int | None] | None:
    """Whether the run's call is still waiting for a slot, and how many calls wait with it.

    `running` once a worker has started the activity; `queued` before that — including between
    retries after a full server, when the activity is scheduled again. The count is the broker's
    approximate backlog on the connector's interactive queue, read only while queued. `None` when
    the broker could not be asked: the annotation is a courtesy, and this must never fail the call.
    """
    try:
        description = await handle.describe()
        pending = description.raw_description.pending_activities
        if any(a.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED for a in pending):
            return "running", None
        queue = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=interactive_queue(connector)),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
                report_stats=True,
            )
        )
    except Exception:
        # Best-effort by contract (the docstring): an RPC fault, a server too old to report stats,
        # a describe racing completion — all of them mean "no annotation this tick", nothing more.
        logger.debug("could not read queue progress for %s", handle.id, exc_info=True)
        return None
    waiting = queue.stats.approximate_backlog_count if queue.HasField("stats") else None
    return "queued", int(waiting) if waiting is not None else None


async def _detach(
    handle: WorkflowHandle[QueuedToolWorkflow, ConnectorJobResult], session_id: str
) -> bool:
    """Ask the run to deliver its answer to this session; False if the run has already closed.

    Temporal does not lose a signal that races a completion — a run that finishes with a signal
    pending is made to process it first — so "not found" here means the answer exists.
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
