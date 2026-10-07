"""How a connector is reached, so an unreachable connector degrades instead of failing the turn.

Losing a capability is a smaller failure than losing the turn. `absorb_connect_failure` is the
whole policy (degrade, unless the caller is what cancelled us), in one function so no path can
differ.

`HeldConnectorSession` is the unit, because `load_mcp_tools` needs a live session. It holds the
session inside a task of its own: the MCP session is an `anyio` cancel scope, which must be exited
by the task that entered it, and that is what lets every connector open concurrently. A failed
open contributes no tools and the next turn tries again, unless `connectors.reachability` says the
host was recently found down, in which case the dial is skipped and the connector is reported
unreachable the same way. A call that exceeds `request_timeout` is cancelled on the server too
(`core.mcp_session.cancel_on_timeout`).
"""

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.interceptors import ToolCallInterceptor
from langchain_mcp_adapters.sessions import Connection, create_session
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.exceptions import McpError

from chemclaw.connectors.manifest import QueuedDispatch
from chemclaw.connectors.queued import queued_interceptor
from chemclaw.connectors.reachability import recently_unreachable, record_reachability
from chemclaw.core.config import settings
from chemclaw.core.mcp_session import cancel_on_timeout
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)


def transport_failure(exc: BaseException) -> bool:
    """Whether `exc` says the wire failed, rather than the tool behind it.

    Exported so `agent.tool_authz.surface_domain_errors` can word it without importing `mcp` or
    `httpx`. A tool-level error arrives as a returned `ToolMessage(status="error")`, so anything the
    transport raises is transient and must not be worded as "do not retry". `anyio` is matched by
    module name to avoid a new import edge.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(transport_failure(member) for member in exc.exceptions)
    if isinstance(exc, (TimeoutError, ConnectionError, McpError)):
        return True
    module = type(exc).__module__ or ""
    return module.startswith(("httpx", "anyio"))


def _leaves(exc: BaseException) -> list[BaseException]:
    """Every non-group exception inside `exc`, flattening nested `BaseExceptionGroup`s.

    A `TaskGroup` opens a connector, so failures arrive as (possibly nested) groups whose `str()`
    says nothing about the cause.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for member in exc.exceptions for leaf in _leaves(member)]
    return [exc]


def describe_connect_failure(exc: BaseException) -> str:
    """What went wrong, as one grep-able line: the leaf, never the group that wrapped it.

    The leaf is what tells an operator an HTTP 500 or a missing credential from a network fault.
    Cancellation leaves are dropped when any other leaf survives (they are siblings cancelled by the
    real failure) and kept when they are all there is. Whitespace is collapsed because some messages
    (e.g. `httpx.HTTPStatusError`) contain newlines.
    """
    leaves = _leaves(exc)
    named = [leaf for leaf in leaves if not isinstance(leaf, asyncio.CancelledError)] or leaves
    return "; ".join(_one(leaf) for leaf in named) or type(exc).__name__


def _one(exc: BaseException) -> str:
    """One leaf as `Type: message`, or the bare type when it has no message.

    A `TimeoutError` stringifies to `""`, so `Type: message` alone would end where the reason should
    be.
    """
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def absorb_connect_failure(connector: str, exc: BaseException) -> None:
    """Treat `exc` as "this connector is absent this turn", unless the caller cancelled us.

    Broad in what it absorbs (refused connection, DNS, TLS, timeout, MCP `ToolException`, anyio
    cancel-scope errors), because enumerating the family would let the next unlisted member fail the
    turn. Re-raises `exc` when it is the caller's own cancellation (`_is_really_cancelled`).
    """
    if isinstance(exc, asyncio.CancelledError) and _is_really_cancelled():
        raise exc
    logger.warning(
        "connector %s is unreachable (%s); its tools are unavailable this turn",
        connector,
        describe_connect_failure(exc),
    )


def _is_really_cancelled() -> bool:
    """Whether cancellation was requested on the current task, rather than an inner scope.

    `Task.cancelling()` counts real `task.cancel()` calls (timeouts, the turn bound, a disconnect);
    an anyio cancel scope inside the MCP client raises the same exception without touching it.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """How to reach one connector for one turn, on the LangGraph engine.

    A description rather than an object with a lifecycle, because `create_session` opens from a
    `Connection` mapping and tools exist only once the session is live. `allowed_tools` (the
    manifest's allow-list, narrowed by a profile) is applied to what the server advertises after
    opening. It is mandatory: an empty allow-list would bind the server's whole unclassified
    surface.
    """

    name: str
    connection: Connection
    allowed_tools: tuple[str, ...]
    #: The endpoint's queued tools, if it declares any (`manifest.QueuedDispatch`); those calls go
    #: through `connectors.queued.queued_interceptor` instead of this session.
    queued: QueuedDispatch | None = None
    #: How long one call may run once it has a slot — the queued call's `start_to_close`.
    request_timeout: float = 60.0


class HeldConnectorSession:
    """One connector's MCP session, entered and exited inside a single task of its own.

    The MCP session is an `anyio` cancel scope that must be exited on the task that entered it;
    entering sessions via `gather` over `AsyncExitStack.enter_async_context` would violate that. So
    `_hold` opens, uses and closes the session, and the caller only signals: `__aenter__` waits for
    the tools, `__aexit__` asks the task to stop. That keeps opens concurrent, so a dark fleet costs
    one connect timeout, not their sum.

    A connector that fails to open leaves `tools` empty and its name in `unreachable`; a recent
    down verdict skips the dial with the same outcome.
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
        """Open the session on its own task and return the tools it advertises (`[]` if absent).

        Bounded by `connector_open_timeout_seconds`, because the handshake is otherwise bounded only
        by the session's read timeout, sized for the slowest tool call. Skipped entirely for a host
        recently found down; the outcome is recorded either way (`connectors.reachability`).
        """
        if recently_unreachable(self._spec.name):
            # Not `absorb_connect_failure`: nothing was dialled. `connected` stays False, so the
            # caller reports and counts this connector like one that failed.
            logger.warning(
                "connector %s was found unreachable within the last %.0fs; not dialling it this "
                "turn — its tools are unavailable",
                self._spec.name,
                settings.connector_breaker_window_seconds,
            )
            return []
        self._task = asyncio.create_task(self._hold(), name=f"connector:{self._spec.name}")
        try:
            async with asyncio.timeout(settings.connector_open_timeout_seconds):
                await self._opened.wait()
        except TimeoutError:
            await self._shut_down()
            record_reachability(self._spec.name, reachable=False, dialled=True)
            # Named rather than rendered, since `asyncio.timeout`'s `TimeoutError` has no message.
            absorb_connect_failure(
                self._spec.name,
                TimeoutError(
                    "the MCP handshake did not complete within "
                    f"{settings.connector_open_timeout_seconds}s"
                ),
            )
            return []
        except BaseException:
            # Cancelled while connecting: the holder owns a live cancel scope, so tell it to unwind
            # on its own task rather than orphan a task holding an MCP session.
            await self._shut_down()
            raise
        # Both outcomes are recorded, and the healthy one is not an optimisation: it is what lets a
        # connector that recovered be readmitted in a process whose readiness route never runs.
        record_reachability(self._spec.name, reachable=self._failure is None, dialled=True)
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
        """Signal the holder task and await its unwind, bounded, and reraising the caller's own.

        Bounded so a slow session close cannot hold the turn's exit stack; at the bound `wait_for`
        cancels the holder and waits for that to land. A cancellation of this task (a disconnect,
        the turn deadline) is re-raised, so the caller's rollback and `asyncio.timeout` still work;
        `_is_really_cancelled()` distinguishes it from the holder's own anyio scope unwinding.
        `wait_for`'s own expiry raises `TimeoutError` without touching the cancel count.
        """
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=settings.connector_teardown_timeout_seconds)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                if _is_really_cancelled():
                    raise
            except Exception:
                # A connector that errors while closing costs its own close, never the turn.
                pass

    async def _hold(self) -> None:
        """Own the session end to end: open it, publish its tools, then wait to be told to stop.

        Everything touching the cancel scope happens on this task. The `finally` always sets
        `_opened`, so a failed connector never leaves the turn waiting on an event nobody sets.
        """
        try:
            async with create_session(self._spec.connection) as session:
                handshake = await session.initialize()
                # A call past `request_timeout` must tell the server to stop, since this session
                # stays open for the turn (`core.mcp_session.cancel_on_timeout`).
                cancel_on_timeout(session)
                self._tools = _stamped(
                    _allowed(
                        await load_mcp_tools(session, tool_interceptors=_interceptors(self._spec)),
                        self._spec.allowed_tools,
                    ),
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


def _interceptors(spec: ConnectorSpec) -> list[ToolCallInterceptor] | None:
    """The adapter's call interceptors for this connector: the queue, where it declares one.

    Uses the adapter's seam, so the bound tool (name, schema, `SERVED_BY` stamp, content conversion)
    is unchanged and only a queued call's last hop differs.
    """
    if spec.queued is None:
        return None
    return [queued_interceptor(spec.name, spec.queued, spec.request_timeout)]


def _allowed(tools: list[BaseTool], allowed: tuple[str, ...]) -> list[BaseTool]:
    """Keep only the tools a connector's allow-list names.

    `load_mcp_tools` returns everything the server advertises; anything beyond the declaration is
    dropped here, so a profile's narrowing reaches across the process boundary.
    """
    keep = set(allowed)
    return [tool for tool in tools if tool.name in keep]


#: Metadata key `_stamped` writes and `agent/audit.py::_served_by` reads; one constant so the
#: provenance column cannot silently stop filling.
SERVED_BY = "chemclaw.served_by"


def _stamped(tools: list[BaseTool], *, connector: str, revision: str) -> list[BaseTool]:
    """Record which server, at which build, answers each of these tools.

    `audit_events.revision` names the orchestrator's commit; a remote server's build comes from the
    handshake's `serverInfo{name, version}`, so nothing extra is requested. `"unknown"` is recorded
    as is (a remote server that cannot name its build); in-process tools carry no stamp. Kept on
    `BaseTool.metadata`, merged with what the adapter set, so it cannot drift from the tool list.
    """
    served = {"connector": connector, "revision": revision}
    for tool in tools:
        tool.metadata = {**(tool.metadata or {}), SERVED_BY: served}
        _neutralise_advertised_text(connector, tool)
    _record_schema_cost(connector, tools)
    return tools


def _neutralise_advertised_text(connector: str, tool: BaseTool) -> None:
    """Defang and bound what a server said about itself, before the model is ever shown it.

    Tool descriptions and schema strings are sent in the `tools` block of every model call, outside
    the result framing (`agent/tool_framing.py`). Two halves are closable in code: forgery (`defang`
    over the description and every schema string, keeping them readable) and budget (a
    per-description ceiling, `connector_max_tool_description_chars`, cut with a notice and a
    WARNING).

    A description can still carry instructions; it is trusted as far as the connector is (image
    provenance, the `SERVED_BY` revision). Any call it asks for still passes authorization, the plan
    gate, the dry-run guard and the repeat guard.
    """
    # Imported lazily: `agent/tool_framing.py` imports this module for `SERVED_BY`, so a
    # module-scope import would be a cycle. `defanged_payload` is the one recursion over every
    # string in a payload.
    from chemclaw.agent.framing import defang
    from chemclaw.agent.tool_framing import defanged_payload

    tool.description = _bounded_description(connector, tool.name, defang(tool.description or ""))
    if isinstance(tool.args_schema, dict):
        tool.args_schema = defanged_payload(tool.args_schema)


def _bounded_description(connector: str, name: str, description: str) -> str:
    """`description` cut to `connector_max_tool_description_chars`, saying so where it was cut.

    Head and tail, because a docstring's ending holds its `Returns:` and caveats. The notice names
    itself as the system's. A limit below the notice's length returns the notice alone: saying a cut
    happened outranks the arithmetic.
    """
    limit = settings.connector_max_tool_description_chars
    if not limit or len(description) <= limit:
        return description
    notice = (
        f"\n[…{len(description) - limit:,} characters of this description cut by the system…]\n"
    )
    head = max(limit - len(notice), 0) * 3 // 5
    tail = max(limit - len(notice) - head, 0)
    logger.warning(
        "connector %s advertises %s with a %d-character description; cut to %d",
        connector,
        name,
        len(description),
        limit,
    )
    return description[:head] + notice + (description[-tail:] if tail else "")


#: What each connector's advertised tool schemas cost a turn, by connector. A level, not a rate: the
#: last handshake is the truth.
_SCHEMA_TOKENS: dict[str, float] = {}


def _record_schema_cost(connector: str, tools: list[BaseTool]) -> None:
    """Publish what this connector's tool schemas add to every turn's prefix.

    Endpoint schemas arrive from a running server, outside `tests/test_context_floor.py`'s ratchet,
    so they are measured per connector instead; with the ratcheted floor this is a turn's cost
    before the chemist says anything. Never raises and never blocks a handshake.
    """
    try:
        # Imported lazily: the agent imports this module, so a module-scope import would be a cycle.
        from chemclaw.agent.context_budget import estimate_tool_schemas

        _SCHEMA_TOKENS[connector] = float(estimate_tool_schemas(tools))
    except Exception:
        logger.debug("could not measure %s's tool schemas", connector, exc_info=True)


METRICS.bind_gauge_family("chemclaw_connector_tool_schema_tokens", lambda: dict(_SCHEMA_TOKENS))
