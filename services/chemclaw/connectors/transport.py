"""How a connector is reached: the MAF MCP tools, built so an unreachable connector degrades.

Out-of-process capability makes every connector a network dependency in the tool path, and the
default MAF behavior for a connector that will not connect is to raise — which turns one dead
sidecar into a dead conversation. That is the wrong trade: losing a capability is a much smaller
failure than losing the turn, and it is the trade decision 7 of `docs/connector-plan.md` records.

Making it non-fatal at the *caller* is not possible, and finding that out is what shaped this
module: `Agent.run` re-enters any `mcp_tools` entry that is not connected
(`agent_framework/_agents.py:1363`), so a caller that catches the failure just has it raised again
from inside the run. The behavior has to belong to the tool.

`connect()` is the one choke point every path funnels through — the context manager calls it, and so
does MAF's run loop — so overriding it is enough, and it leaves the rest of MAF's lifecycle
untouched. A failed connect leaves `is_connected` False and the loaded tool set empty, which gives
exactly the semantics wanted: the connector contributes no tools this turn, the turn proceeds
without them, and the *next* turn tries again — so a connector that comes back needs no restart to
be picked up.
"""

import asyncio
import logging
from typing import Any

from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

logger = logging.getLogger(__name__)


class _DegradeOnConnectFailure:
    """Mixin: a failed connect logs and leaves the tool unconnected instead of raising.

    Deliberately broad in what it catches, and narrow in what it does. Broad, because the failure
    family is wide — a refused TCP connection, a DNS miss, a TLS error, a timeout, MAF's own
    `ToolException`, an `anyio` cancel-scope error from a half-finished handshake — and enumerating
    them means the next unlisted one silently restores the fatal behavior. Nothing wider than
    `Exception` plus `CancelledError` is caught, so `KeyboardInterrupt`/`SystemExit` propagate.

    Narrow, because it does not retry, back off, or cache the failure: MAF asks again on the next
    run, which is a natural retry cadence, and a connector that recovers is used again with no extra
    machinery.

    **A real cancellation is re-raised, and getting this wrong is not cosmetic.** MAF swallows
    `CancelledError` in its own MCP paths on the grounds that an MCP-internal cancel scope cannot
    be told apart from a genuine one — but at this layer it can, and must be: the front door bounds
    a turn's wall-clock and cancels it (`service_turn_timeout_seconds`), and swallowing *that*
    left a hung turn running to completion with its admission permit held — the exact failure the
    bound exists to prevent. `Task.cancelling()` is non-zero only when cancellation was asked of
    *this* task, which an `anyio` scope inside the MCP client never does — so it separates "the
    connector is absent" from "the caller gave up".
    """

    async def connect(self, **kwargs: Any) -> None:
        """Connect, or log and stay disconnected — but never swallow the caller's cancellation."""
        try:
            await super().connect(**kwargs)  # type: ignore[misc]
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and _is_really_cancelled():
                raise
            logger.warning(
                "connector %s is unreachable (%s: %s); its tools are unavailable this turn",
                getattr(self, "name", "?"),
                type(exc).__name__,
                exc,
            )


def _is_really_cancelled() -> bool:
    """Whether cancellation was requested on the *current task*, rather than an inner scope.

    `Task.cancelling()` counts outstanding `task.cancel()` calls against this task — what
    `asyncio.timeout`, the front door's turn bound, and a client disconnect all do. An `anyio`
    cancel scope inside the MCP client unwinds through the same exception type without touching
    that counter, which is what makes the two distinguishable here.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class DegradingHttpConnector(_DegradeOnConnectFailure, MCPStreamableHTTPTool):
    """An HTTP connector whose unavailability costs its tools, not the turn."""


class DegradingStdioConnector(_DegradeOnConnectFailure, MCPStdioTool):
    """A stdio connector whose unavailability costs its tools, not the turn."""
