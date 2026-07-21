"""The per-turn run lifecycle (plan step F2-T1): the missing caller that actually runs the agent.

`run_turn` owns exactly what the agent's own docstring says a caller must own: it opens the MCP tool
contexts for the turn (`async with *agent.mcp_tools`), runs the turn against the session's thread,
and translates the model's streamed updates into the typed `service.events` the surfaces render.
When the harness is enabled the *same* call drives its completion loop (MAF's loop middleware runs
inside `agent.run`), so plan/execute autonomy needs no separate driver here.

Errors are turned into a single `ErrorEvent` with a user-safe message rather than propagating a
stack trace to the browser — a failed turn must not take down the stream or leak internals.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

from agent_framework import AgentSession

from service.events import (
    AnswerEvent,
    ApprovalRequestEvent,
    ErrorEvent,
    Event,
    TokenEvent,
    ToolCallEvent,
)

# How many characters of a tool call's arguments the trace event carries — enough to see *what* was
# called without streaming a whole evidence payload to the UI (mirrors the audit trail truncation).
_ARG_PREVIEW_CHARS = 200


async def run_turn(agent: Any, session: AgentSession, user_message: str) -> AsyncIterator[Event]:
    """Run one turn and yield its events (tokens, tool calls, approvals, then the answer).

    Args:
        agent: A built Chemclaw agent (classic or harness). Injected by the app; injectable so tests
            drive it with a fake streaming agent and no live model.
        session: The caller's conversation session (per user+thread), so the turn resumes context.
        user_message: The chemist's message for this turn.

    Yields:
        `service.events.Event` values in the order the model produced them, ending with an
        `AnswerEvent` on success or an `ErrorEvent` on failure.
    """
    answer_parts: list[str] = []
    try:
        async with AsyncExitStack() as stack:
            # Open each MCP capability server for the duration of the turn, then tear it down — the
            # lifecycle the agent constructor deliberately leaves to its caller.
            for tool in getattr(agent, "mcp_tools", None) or []:
                await stack.enter_async_context(tool)
            stream = agent.run(user_message, stream=True, session=session)
            async for update in stream:
                text = getattr(update, "text", "") or ""
                if text:
                    answer_parts.append(text)
                    yield TokenEvent(text=text)
                for tool_name, arguments in _tool_calls_in(update):
                    yield ToolCallEvent(tool=tool_name, arguments=arguments)
                for request in getattr(update, "user_input_requests", None) or []:
                    yield ApprovalRequestEvent(prompt=_approval_prompt(request))
        yield AnswerEvent(text="".join(answer_parts))
    except Exception as exc:
        # One turn's failure becomes one user-safe event, never a 500 mid-stream or a leaked trace.
        yield ErrorEvent(message=f"The turn could not be completed: {exc}")


def _tool_calls_in(update: Any) -> list[tuple[str, str]]:
    """Best-effort extract (tool_name, arg_preview) for any function call in a streamed update.

    Duck-typed on purpose: MAF's function-call content class is not a stable top-level export and
    its shape varies by version, so we match by structure (a named content carrying arguments/a call
    id) rather than importing a concrete type. Plain-text content has no `name` and is skipped.
    """
    calls: list[tuple[str, str]] = []
    for content in getattr(update, "contents", None) or []:
        name = getattr(content, "name", None)
        if not name:
            continue
        if not (hasattr(content, "arguments") or hasattr(content, "call_id")):
            continue
        arguments = str(getattr(content, "arguments", "") or "")[:_ARG_PREVIEW_CHARS]
        calls.append((str(name), arguments))
    return calls


def _approval_prompt(request: Any) -> str:
    """Render a user-input/approval request as a short prompt string for the UI."""
    for attr in ("prompt", "message", "text", "description"):
        value = getattr(request, attr, None)
        if value:
            return str(value)
    return "Approval requested."
