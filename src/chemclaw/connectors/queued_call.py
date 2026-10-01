"""One queued tool call, made from a worker: the wire model and the activity that sends it.

A connector tool the manifest lists under `queued:` is not called on the turn's own session. The
turn starts a `QueuedToolWorkflow` (`connectors/queued_workflow.py`) on the connector's interactive
queue, and this activity — on a worker sized to the server's slots — makes the call when one is
free. So a burst of chemists waits in one global, first-come queue instead of each being refused by
whichever pod the Service happened to pick
(`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`).

**Three outcomes, and only one of them is retried quickly.** A server that answers is the result,
refusal included: a domain refusal ("unknown solvent") is the tool's answer, and asking again cannot
change it, so it travels back as the `CallToolResult` it was and the agent reads it exactly as it
would have on a direct call. A server that says it is *full* (`core.mcp_session.at_capacity`) raises
the retryable `ConnectorAtCapacity`, re-sent within seconds (`durable.publish.queued_tool_retry`).
Anything else — the connector did not answer, the transport broke — is re-sent a few times and then
failed (`settings.queued_tool_fault_attempts`): a rolling pod is the common cause of that, and an
outage is news the chemist should hear now rather than in an hour.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_mcp_adapters.sessions import create_session
from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity
from temporalio.exceptions import ApplicationError

from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.mcp_session import at_capacity, text_of
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id

#: The `ApplicationError.type` of a full server — retryable, and the only fault that is retried
#: until the call's own deadline.
AT_CAPACITY_TYPE = "ConnectorAtCapacity"
#: The `ApplicationError.type` of a fault that outlived its attempts; non-retryable.
FAULT_TYPE = "QueuedToolFault"


class QueuedToolCall(BaseModel):
    """What a queued call carries across the broker: the call, and the budgets it runs under.

    The *actor* and correlation id are not in here: they travel as the activity's own bare
    arguments, where `durable/interceptor.py` reads them by name, for the reason
    `connectors/calc/activities.py::run_xtb_calculation` gives — identity must not be part of what
    two identical calls are compared on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: How long the call may run once it has a slot — the endpoint's `request_timeout`.
    call_timeout_seconds: float = Field(gt=0)
    #: How long it may wait for a slot and run, in all (`settings.queued_tool_timeout_seconds`).
    queue_timeout_seconds: float = Field(gt=0)


@contextmanager
def _acting_for(actor: str, correlation_id: str, session_id: str) -> Iterator[None]:
    """Stamp the requester's identity ambient, so the call's headers name the person it is for.

    The same bracket `connectors/calc/activities.py::_acting_for` writes, plus the session: a
    queued call is a turn's call made from somewhere else, and the server's log line should join to
    that conversation as a direct call's would.
    """
    identity = set_current_identity(actor, frozenset()) if actor else None
    correlation = set_current_correlation_id(correlation_id) if correlation_id else None
    session = set_current_session_id(session_id) if session_id else None
    try:
        yield
    finally:
        if session is not None:
            reset_current_session_id(session)
        if correlation is not None:
            reset_current_correlation_id(correlation)
        if identity is not None:
            reset_current_identity(identity)


@activity.defn
async def call_queued_tool(
    call: QueuedToolCall, actor: str = "", correlation_id: str = "", session_id: str = ""
) -> dict[str, Any]:
    """Make one queued tool call and return the server's `CallToolResult`, as JSON.

    Returns:
        The result exactly as the server sent it — `isError` and all for a domain refusal — so the
        turn converts it with the adapter's own function and the agent cannot tell it was queued.

    Raises:
        ApplicationError: `ConnectorAtCapacity` (retryable) when the server is full;
            `QueuedToolFault` (non-retryable) when any other fault outlived its attempts, and
            retryable before that.
    """
    # Imported here, not at the top: the turn imports this module (through the workflow it
    # starts) from `connectors/transport.py`, which the registry itself imports — so a top-level
    # import is a cycle, and only the worker ever reaches this line.
    from chemclaw.connectors.registry import connector_spec

    spec = connector_spec(call.connector)
    try:
        with _acting_for(actor, correlation_id, session_id):
            async with create_session(spec.connection) as session:
                await session.initialize()
                result = await session.call_tool(call.tool, call.arguments)
    except Exception as exc:
        # `Exception` rather than the transport's own types: every failure here happened before
        # the server produced an answer, and the one thing that decides what to do next is how
        # many times this has been tried — not which layer of the client noticed.
        final = activity.info().attempt >= settings.queued_tool_fault_attempts
        raise ApplicationError(
            f"{call.tool} could not be sent to {call.connector!r} ({type(exc).__name__}); "
            + ("giving up." if final else "trying again."),
            type=FAULT_TYPE,
            non_retryable=final,
        ) from exc
    if result.isError and at_capacity(text_of(result.content)):
        raise ApplicationError(
            f"{call.connector!r} is full; {call.tool} is waiting for a free slot",
            type=AT_CAPACITY_TYPE,
        )
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
