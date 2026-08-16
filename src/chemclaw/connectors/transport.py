"""How a connector is reached, so an unreachable connector degrades instead of failing the turn.

Out-of-process capability makes every connector a network dependency in the tool path, and the
default behaviour for a connector that will not connect is to raise — which turns one dead sidecar
into a dead conversation. That is the wrong trade: losing a capability is a much smaller failure
than losing the turn, and it is the trade decision 7 of `docs/archive/plans/connector-plan.md`
records.

`absorb_connect_failure` is the whole policy — degrade, unless the caller is what cancelled us —
and it is one function on purpose. A second copy of that sentence somewhere else would let a dead
connector fail the turn on one path and cost only its tools on another, which is a difference a
chemist would meet as an outage.

**Why a held session rather than a lazily-connecting tool object.** `langchain-mcp-adapters` has no
tool object that can be handed out unconnected: `load_mcp_tools` needs a *live* session, so a
connector's tools do not exist until it is open. `HeldConnectorSession` is therefore the unit, and
it holds its session inside a task of its own for a measured reason, not a stylistic one. The MCP
client's session is an `anyio` cancel scope, and anyio refuses to let a scope be exited by a task
other than the one that entered it. Opening the sessions the natural concurrent way — `asyncio.
gather` over `AsyncExitStack.enter_async_context` — enters each on a child task and exits it on the
caller's, which raises `RuntimeError: Attempted to exit cancel scope in a different task than it
was entered in` (measured; the sequential form of the same code passes). Holding the whole
lifecycle on one task is what makes the concurrent form legal again, and concurrency is kept: every
connector still opens in parallel, each in its own task.

A failed open leaves the session not-connected and its tool set empty, which gives exactly the
semantics wanted: the connector contributes no tools this turn, the turn proceeds without them, and
the *next* turn tries again — so a connector that comes back needs no restart to be picked up.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from types import TracebackType

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection, create_session
from langchain_mcp_adapters.tools import load_mcp_tools

logger = logging.getLogger(__name__)


def absorb_connect_failure(connector: str, exc: BaseException) -> None:
    """Treat `exc` as "this connector is absent this turn" — unless the caller cancelled us.

    The one decision both engines make about an unreachable connector, extracted so neither can
    drift from the other. Deliberately broad in what it absorbs and narrow in what it does: the
    failure family is wide (a refused TCP connection, a DNS miss, a TLS error, a timeout, an MCP
    `ToolException`, an `anyio` cancel-scope error from a half-finished handshake) and enumerating
    it means the next unlisted member silently restores the fatal behaviour.

    Args:
        connector: The bundle's name, for the log line an operator reads.
        exc: What the connector's open raised.

    Raises:
        BaseException: `exc` itself, when it is the caller's own cancellation — see
            `_is_really_cancelled` for why that case must not be absorbed.
    """
    if isinstance(exc, asyncio.CancelledError) and _is_really_cancelled():
        raise exc
    logger.warning(
        "connector %s is unreachable (%s: %s); its tools are unavailable this turn",
        connector,
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


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """How to reach one connector for one turn, on the LangGraph engine.

    The LangChain twin of an unconnected `ConnectorMcpTool`, and it is a *description* rather than
    an object with a lifecycle because that is the shape the library takes: `create_session` opens
    a connection from a `Connection` mapping, and `load_mcp_tools` needs the live session before any
    tool exists. So the thing built per turn is this, and the thing opened per turn is the session.

    `allowed_tools` is carried here rather than applied at build time because it is the manifest's
    agent-facing allow-list, narrowed again by a profile, and it has to be applied to what the
    *server* advertises — which is not knowable until the session is open.
    """

    name: str
    connection: Connection
    allowed_tools: tuple[str, ...] | None


class HeldConnectorSession:
    """One connector's MCP session, entered and exited inside a single task of its own.

    **The task is the point, and it is a measured requirement rather than a style.** The MCP
    client's session is an `anyio` cancel scope, and anyio refuses to let a scope be exited by a
    task other than the one that entered it. The natural concurrent shape — `asyncio.gather` over
    `AsyncExitStack.enter_async_context` — enters each session on a child task and exits it on the
    caller's, and raises `RuntimeError: Attempted to exit cancel scope in a different task than it
    was entered in`. The sequential form of the same code passes, which is what identifies the
    cause as task affinity rather than the session.

    So the session is opened, used and closed entirely within `_hold`, and the caller only ever
    signals: `__aenter__` waits for the tools, `__aexit__` asks the task to stop. Both of those
    happen on the caller's task, which is what makes this safe to `gather` — every connector still
    opens in parallel, which must not be given back (a dark fleet otherwise costs the sum of its
    connect timeouts before the model is called).

    A connector that fails to open leaves `tools` empty and its name in `unreachable`: the turn
    proceeds without it and the next turn tries again.
    """

    def __init__(self, spec: ConnectorSpec) -> None:
        """Prepare a holder; nothing is opened until the session is entered."""
        self._spec = spec
        self._tools: list[BaseTool] = []
        self._opened = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    @property
    def name(self) -> str:
        """The bundle's name, for the degradation report a surface shows."""
        return self._spec.name

    @property
    def connected(self) -> bool:
        """Whether the session came up, which is what the degradation report is derived from."""
        return self._failure is None and self._task is not None

    async def __aenter__(self) -> list[BaseTool]:
        """Open the session on its own task and return the tools it advertises (`[]` if absent)."""
        self._task = asyncio.create_task(self._hold(), name=f"connector:{self._spec.name}")
        try:
            await self._opened.wait()
        except BaseException:
            # The caller was cancelled while we were connecting. The holder task owns a live cancel
            # scope, so it must be told to unwind on its own task rather than abandoned — an
            # orphaned task holding an MCP session is the leak this class exists to make impossible.
            await self._shut_down()
            raise
        if self._failure is not None:
            absorb_connect_failure(self._spec.name, self._failure)
        return self._tools

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Ask the holder task to close its session, and wait for it to finish doing so."""
        await self._shut_down()
        return False

    async def _shut_down(self) -> None:
        """Signal the holder task and await its unwind, whatever it raises on the way out."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _hold(self) -> None:
        """Own the session end to end: open it, publish its tools, then wait to be told to stop.

        Everything that touches the cancel scope happens here, on this one task. The `finally` sets
        `_opened` unconditionally so a caller waiting on it is released whether the connector came
        up or not — a connector that hangs is bounded by the turn's own clock, but one that *fails*
        must never leave the turn waiting on an event nobody will set.
        """
        try:
            async with create_session(self._spec.connection) as session:
                handshake = await session.initialize()
                self._tools = _stamped(
                    _allowed(await load_mcp_tools(session), self._spec.allowed_tools),
                    connector=self._spec.name,
                    revision=handshake.serverInfo.version,
                )
                self._opened.set()
                await self._stop.wait()
        except (Exception, asyncio.CancelledError) as exc:
            self._failure = exc
            self._tools = []
        finally:
            self._opened.set()


def _allowed(tools: list[BaseTool], allowed: tuple[str, ...] | None) -> list[BaseTool]:
    """Keep only the tools a connector's allow-list names.

    `load_mcp_tools` returns whatever the server advertises, so the allow-list has to be applied
    here or a profile's narrowing would stop at the process boundary. `None` means the manifest
    declared no allow-list, which is "everything this server offers".
    """
    if allowed is None:
        return list(tools)
    keep = set(allowed)
    return [tool for tool in tools if tool.name in keep]


#: What `_stamped` writes and `agent/audit.py::_served_by` reads. One constant, because a key
#: spelled in two files is a provenance field that silently stops being filled the day one of them
#: is renamed — and a blank provenance column reads exactly like an in-process call.
SERVED_BY = "chemclaw.served_by"


def _stamped(tools: list[BaseTool], *, connector: str, revision: str) -> list[BaseTool]:
    """Record which server, at which build, answers each of these tools.

    **The audit trail's remaining provenance hole, and this is where the answer is knowable.**
    `audit_events.revision` names the *orchestrator's* commit, which was the whole story while the
    chemistry ran in this process. It no longer does: the capability moved to `Chemclaw3-mcp`
    servers that release on their own cadence, so "which build produced this number" became a fact
    about a different process — one only the MCP handshake can state.

    `initialize()` returns `serverInfo{name, version}` on every session, which is why nothing new is
    opened, sent or awaited to learn this. The version is `"unknown"` unless that server's image was
    built with its revision (`Chemclaw3-mcp` `docs/integration.md`), and recording `"unknown"` is
    the correct outcome there rather than a reason to omit the field — it says a remote server
    answered and could not name its build, which is a different fact from an in-process tool, whose
    stamp is absent entirely because `revision` already covers it.

    Carried on `BaseTool.metadata` rather than threaded through `open_connector_specs`'s return
    value: it is a fact *about a tool*, and the alternative is a parallel `{name: revision}` map
    passed through four callers and a builder parameter, duplicating the structure of the list it
    travels beside — where a tool dropped from one and not the other is a silent misattribution.
    Merged into whatever metadata the adapter already set, never replacing it.
    """
    served = {"connector": connector, "revision": revision}
    for tool in tools:
        tool.metadata = {**(tool.metadata or {}), SERVED_BY: served}
    return tools
