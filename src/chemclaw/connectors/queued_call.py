"""One queued tool call, made from a worker: the wire model and the activity that sends it.

A tool the manifest lists under `queued:` runs as a `QueuedToolWorkflow` on the connector's
interactive queue, and this activity, on a worker sized to the server's slots, makes the call when
one is free, so a burst waits first-come-first-served instead of being refused.

Three outcomes: any server answer (a domain refusal included) is the result and is returned as
sent; a full server raises the retryable `ConnectorAtCapacity`
(`durable.publish.queued_tool_retry`); any other fault is retried
`settings.queued_tool_fault_attempts` times and then failed, so an outage reaches the chemist
promptly.
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

    Includes the session, so the server's log joins to the conversation as a direct call's would.
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
        The result exactly as the server sent it, `isError` included, so the turn converts it with
        the adapter's own function and the agent cannot tell it was queued.

    Raises:
        ApplicationError: `ConnectorAtCapacity` (retryable) when the server is full;
            `QueuedToolFault` (non-retryable) when any other fault outlived its attempts, and
            retryable before that.
    """
    # Imported lazily: a top-level import would be a cycle through `connectors/transport.py`.
    from chemclaw.connectors.registry import connector_spec

    spec = connector_spec(call.connector)
    try:
        with _acting_for(actor, correlation_id, session_id):
            async with create_session(spec.connection) as session:
                await session.initialize()
                result = await session.call_tool(call.tool, call.arguments)
    except Exception as exc:
        # Any `Exception`: every failure here precedes an answer, and only the attempt count decides
        # what happens next.
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
