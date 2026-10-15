"""Keep a connector's MCP session across turns, used by one caller's one turn at a time.

A turn otherwise pays a dial, `initialize`, `tools/list`, a standalone stream and a `DELETE` per
connector. The pool holds the session after the turn and hands it to the same actor's next turn,
and keeps each connector's tool listing so a new session does not re-ask.

Invariants:
- A session serves one turn at a time (checked out, not shared), so no request carries two callers'
  identities. It is keyed by actor, connector and endpoint, and its task runs in an empty context
  that pins the headers it stamps (`core.call_identity.IdentityPin`), replaced at every checkout.
- A reused session proves itself first: its holder task is alive and it answers a ping. Otherwise
  it is closed and one fresh session is opened. A session that saw a cancelled or timed-out
  request, or a notification that the tool list changed, is never returned to the pool.
- A listing is reused only while the contract version, server build and URL it was taken under
  are those the new handshake reports, and for at most `connector_session_ttl_seconds`; a
  `tools/list_changed` notification discards it at once. The tool objects a turn gets are built
  from it by the adapter's own converter, so they are what a fresh listing returns.
- Memory is bounded: idle sessions expire after the TTL, at most `connector_session_pool_max` are
  kept (least recently used idle one closed first), and a turn finding the pool full of busy
  sessions runs on an unpooled one. `pooling()` closes everything on exit.
"""

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import InitializeResult, ServerNotification, ToolListChangedNotification
from mcp.types import Tool as McpTool

from chemclaw.connectors.contract import ContractMismatch, check_contract, remembered_version
from chemclaw.connectors.reachability import recently_unreachable, record_reachability
from chemclaw.connectors.transport import (
    ConnectorSpec,
    HeldConnectorSession,
    _allowed,
    _interceptors,
    _record_schema_cost,
    _stamped,
)
from chemclaw.core.call_identity import IdentityPin, pin_connection_identity, turn_headers
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.mcp_session import cancel_on_timeout

logger = logging.getLogger(__name__)

# What makes two specs the same endpoint: URL, call bound, queued tools, and the contract the
# manifest declares and where it is checked.
_Signature = tuple[str, float, str, str | None, str | None]
_Key = tuple[str, str, _Signature]

# The code `cancel_on_timeout` reads on the SDK's local read timeout (pinned in
# `tests/test_upstream_surface.py`): a request abandoned while the server may still run it.
_READ_TIMEOUT_CODE = 408

# Floor on the reaper's sleep, so a tiny TTL cannot spin the loop.
_MIN_SWEEP_SECONDS = 0.05


def _signature(spec: ConnectorSpec) -> _Signature:
    """The parts of `spec` that fix what a session to it is: everything but the allow-list."""
    queued = spec.queued.model_dump_json() if spec.queued is not None else ""
    url = str(cast(dict[str, Any], spec.connection).get("url", ""))
    return (url, spec.request_timeout, queued, spec.contract_version, spec.health_url)


def poolable(spec: ConnectorSpec) -> bool:
    """Whether a spec is reached over streamable HTTP, which carries the identity headers."""
    return spec.connection["transport"] == "streamable_http"


async def _list_all(session: ClientSession) -> list[McpTool]:
    """Every tool the server advertises, following its pagination cursor."""
    tools: list[McpTool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if not page.nextCursor:
            return tools
        cursor = page.nextCursor


class ToolListings:
    """Each connector's last `tools/list`, valid for one `(url, contract version, server build)`.

    One entry per connector, so memory is bounded by the fleet. A hit also requires the listing to
    be younger than the session TTL, the bound on a change the key cannot see.
    """

    def __init__(self) -> None:
        """Start with nothing listed."""
        self._held: dict[str, tuple[tuple[str, str | None, str], float, list[McpTool]]] = {}

    def get(self, connector: str, key: tuple[str, str | None, str]) -> list[McpTool] | None:
        """A private copy of the listing taken under `key`, or `None` when it is absent or stale."""
        held = self._held.get(connector)
        if held is None or held[0] != key:
            return None
        if time.monotonic() - held[1] >= settings.connector_session_ttl_seconds:
            return None
        return [tool.model_copy(deep=True) for tool in held[2]]

    def put(self, connector: str, key: tuple[str, str | None, str], tools: list[McpTool]) -> None:
        """Remember a fresh listing, replacing whatever the connector had."""
        self._held[connector] = (key, time.monotonic(), tools)

    def forget(self, connector: str) -> None:
        """Drop a connector's listing, so the next session asks the server again."""
        self._held.pop(connector, None)


class KeptConnectorSession(HeldConnectorSession):
    """A `HeldConnectorSession` that outlives a turn: pinned identity, listing cache, health checks.

    Its holder task starts in an empty context and pins `_pin`, so the opening turn's identity is
    read by nothing and the headers are whatever `stamp()` last set.
    """

    def __init__(self, spec: ConnectorSpec, key: _Key, listings: ToolListings) -> None:
        """Prepare an unopened session for `key`, sharing the process's listing cache."""
        super().__init__(spec)
        self.key = key
        self.pooled = False
        self.tainted = False
        self.inflight = 0
        self.list_changed = False
        self.idle_since = 0.0
        self._listings = listings
        self._pin = IdentityPin()
        self._session: ClientSession | None = None
        self._advertised: list[BaseTool] = []
        self._costed: tuple[str, ...] = ()

    def stamp(self) -> None:
        """Make this session's requests carry the calling turn's identity from now on."""
        self._pin.headers = turn_headers()

    def tools_for(self, spec: ConnectorSpec) -> list[BaseTool]:
        """The advertised tools this turn's allow-list names, their schema cost on the gauge."""
        tools = _allowed(self._advertised, spec.allowed_tools)
        names = tuple(tool.name for tool in tools)
        if names != self._costed:
            _record_schema_cost(spec.name, tools)
            self._costed = names
        return tools

    @property
    def keepable(self) -> bool:
        """Whether the session may serve another turn: up, and nothing of it left in doubt."""
        task = self._task
        return (
            task is not None
            and not task.done()
            and self._failure is None
            and self._session is not None
            and not self.tainted
            and not self.inflight
            and not self.list_changed
        )

    async def answers(self) -> bool:
        """Whether the server still holds this session and speaks for the declared contract.

        One ping, bounded by `connector_health_timeout_seconds`. Any failure (a restarted server
        answers 404, a dead one nothing) is "no": the caller opens a fresh session.
        """
        session = self._session
        if not self.keepable or session is None:
            return False
        try:
            await check_contract(
                self._spec.name, self._spec.contract_version, self._spec.health_url
            )
            async with asyncio.timeout(settings.connector_health_timeout_seconds):
                await session.send_ping()
        except ContractMismatch:
            return False
        except Exception:
            logger.info("connector %s: the kept session did not answer a ping", self._spec.name)
            return False
        return True

    async def close(self) -> None:
        """End the session and its holder task, bounded; never raises."""
        self._session = None
        await self._shut_down()

    def _spawn(self) -> asyncio.Task[None]:
        """Start the holder in an empty context, so it inherits no turn's identity or deadline."""
        return asyncio.create_task(
            self._hold(), name=f"connector:{self._spec.name}", context=contextvars.Context()
        )

    async def _hold(self) -> None:
        """Pin the headers this connection stamps, then hold the session as a per-turn one does."""
        pin_connection_identity(self._pin)
        await super()._hold()

    def _connection(self) -> Connection:
        """The spec's connection with a handler that notices a changed tool list."""
        connection = dict(cast(dict[str, Any], self._spec.connection))
        connection["session_kwargs"] = {
            **connection.get("session_kwargs", {}),
            "message_handler": self._on_message,
        }
        return cast(Connection, connection)

    async def _on_message(self, message: Any) -> None:
        """Mark the session unfit for reuse when the server says its tool list changed."""
        if isinstance(message, ServerNotification) and isinstance(
            message.root, ToolListChangedNotification
        ):
            self.list_changed = True
            self._listings.forget(self._spec.name)

    async def _bind(self, session: ClientSession, handshake: InitializeResult) -> list[BaseTool]:
        """Every advertised tool, from the listing cache when the handshake agrees with it."""
        cancel_on_timeout(session)
        self._track(session)
        self._session = session
        revision = handshake.serverInfo.version
        key = (
            str(cast(dict[str, Any], self._spec.connection).get("url", "")),
            remembered_version(self._spec.name) or self._spec.contract_version,
            revision,
        )
        advertised = self._listings.get(self._spec.name, key)
        if advertised is not None:
            # The SDK validates a result against the output schema `list_tools` recorded and, for a
            # tool it never listed, lists again before the first call. A cached listing seeds that
            # record, so the first call neither pays the listing nor skips the validation.
            for tool in advertised:
                session._tool_output_schemas[tool.name] = tool.outputSchema
        else:
            advertised = await _list_all(session)
            self._listings.put(
                self._spec.name, key, [tool.model_copy(deep=True) for tool in advertised]
            )
        interceptors = _interceptors(self._spec)
        self._advertised = _stamped(
            [
                convert_mcp_tool_to_langchain_tool(session, tool, tool_interceptors=interceptors)
                for tool in advertised
            ],
            connector=self._spec.name,
            revision=revision,
        )
        self._costed = tuple(tool.name for tool in self._advertised)
        return self._advertised

    def _track(self, session: ClientSession) -> None:
        """Count requests in flight, and mark the session tainted when one is abandoned.

        A request that was cancelled or hit its read bound may still be running on the server, so a
        session that saw one is not handed to another turn.
        """
        send_request = session.send_request

        async def tracked(*args: Any, **kwargs: Any) -> Any:
            self.inflight += 1
            try:
                return await send_request(*args, **kwargs)
            except McpError as exc:
                if exc.error.code == _READ_TIMEOUT_CODE:
                    self.tainted = True
                raise
            except BaseException:
                self.tainted = True
                raise
            finally:
                self.inflight -= 1

        session.send_request = tracked  # type: ignore[method-assign]


class PooledConnectorSession:
    """What `open_connector_specs` enters per connector when a pool is active: a session lease.

    Same surface as `HeldConnectorSession`: `name`, `connected`, enter for tools, exit to let go.
    """

    def __init__(self, pool: "ConnectorSessionPool", spec: ConnectorSpec) -> None:
        """Prepare a lease for `spec`; nothing is checked out until entered."""
        self._pool = pool
        self._spec = spec
        self._entry: KeptConnectorSession | None = None
        self._connected = False

    @property
    def name(self) -> str:
        """The bundle's name, for the degradation report a surface shows."""
        return self._spec.name

    @property
    def connected(self) -> bool:
        """Whether a session came up for this turn."""
        return self._connected

    async def __aenter__(self) -> list[BaseTool]:
        """Check out a session for this actor and return this turn's tools (`[]` if absent)."""
        entry = await self._pool.checkout(self._spec)
        self._entry = entry
        self._connected = entry.connected
        return entry.tools_for(self._spec) if self._connected else []

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Give the session back to the pool, or close it if it cannot be reused."""
        entry, self._entry = self._entry, None
        if entry is not None:
            await self._pool.checkin(entry)
        return False


class ConnectorSessionPool:
    """The process's kept connector sessions: idle ones by key, leased ones by identity."""

    def __init__(self) -> None:
        """Start empty; `start()` begins the expiry sweep."""
        self._idle: OrderedDict[_Key, KeptConnectorSession] = OrderedDict()
        self._leased: set[KeptConnectorSession] = set()
        self._listings = ToolListings()
        self._closing: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def size(self) -> int:
        """Sessions the pool holds, idle and leased."""
        return len(self._idle) + len(self._leased)

    def start(self) -> None:
        """Begin closing idle sessions that outlive the TTL, so a quiet process holds none."""
        self._sweeper = asyncio.create_task(self._sweep(), name="connector-session-sweeper")

    def session(self, spec: ConnectorSpec) -> PooledConnectorSession:
        """A lease on a session for `spec`, taken when entered."""
        return PooledConnectorSession(self, spec)

    async def checkout(self, spec: ConnectorSpec) -> KeptConnectorSession:
        """A session for the current actor and `spec`: the kept one if it answers, else a new one.

        Never raises for an unreachable connector; the returned session is then not `connected`,
        exactly as a per-turn open reports it. A kept session that fails its checks is replaced
        once, in this call.
        """
        key: _Key = (spec.name, get_current_actor() or "", _signature(spec))
        kept = self._idle.pop(key, None)
        if kept is not None:
            if await self._still_good(kept):
                self._leased.add(kept)
                return kept
            logger.info("connector %s: the kept session is gone; opening a fresh one", spec.name)
        fresh = KeptConnectorSession(spec, key, self._listings)
        fresh.stamp()
        await fresh.__aenter__()
        if fresh.connected:
            fresh.pooled = self._admit(fresh)
        return fresh

    async def checkin(self, session: KeptConnectorSession) -> None:
        """Take a session back: idle for the next turn if it is fit, else closed."""
        self._leased.discard(session)
        if (
            session.pooled
            and not self._closed
            and settings.connector_session_ttl_seconds > 0
            and session.keepable
        ):
            session.idle_since = time.monotonic()
            superseded = self._idle.pop(session.key, None)
            self._idle[session.key] = session
            if superseded is not None:
                self._close_later(superseded)
            return
        await session.close()

    async def aclose(self) -> None:
        """Close every session, idle or leased, and stop the sweep."""
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        sessions = [*self._idle.values(), *self._leased]
        self._idle.clear()
        self._leased.clear()
        await asyncio.gather(
            *(session.close() for session in sessions), *self._closing, return_exceptions=True
        )

    async def _still_good(self, session: KeptConnectorSession) -> bool:
        """Whether an idle session may serve this turn; closes it when not.

        Stamped before the ping so the ping carries the turn's identity, and closed rather than
        leaked if the caller is cancelled while waiting.
        """
        spec_name = session.key[0]
        fresh_enough = (
            time.monotonic() - session.idle_since < settings.connector_session_ttl_seconds
        )
        if fresh_enough and not recently_unreachable(spec_name):
            session.stamp()
            try:
                if await session.answers():
                    # The ping is a fresh reachability reading, as a per-turn open's would be.
                    record_reachability(spec_name, reachable=True, dialled=True)
                    return True
            except BaseException:
                await session.close()
                raise
        await session.close()
        return False

    def _admit(self, session: KeptConnectorSession) -> bool:
        """Whether the pool keeps this new session, closing the oldest idle one to make room."""
        if self._closed:
            return False
        if self.size >= settings.connector_session_pool_max:
            if not self._idle:
                return False
            _, oldest = self._idle.popitem(last=False)
            self._close_later(oldest)
        self._leased.add(session)
        return True

    def _close_later(self, session: KeptConnectorSession) -> None:
        """Close a session off the caller's path, keeping the task referenced until it ends."""
        task = asyncio.create_task(session.close(), name=f"connector-close:{session.key[0]}")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _sweep(self) -> None:
        """Close idle sessions past the TTL, for as long as the pool lives."""
        while True:
            ttl = settings.connector_session_ttl_seconds
            await asyncio.sleep(max(ttl / 2, _MIN_SWEEP_SECONDS))
            now = time.monotonic()
            for key, session in list(self._idle.items()):
                if now - session.idle_since >= ttl or not session.keepable:
                    del self._idle[key]
                    self._close_later(session)


_POOLS: dict[asyncio.AbstractEventLoop, ConnectorSessionPool] = {}


def active_pool() -> ConnectorSessionPool | None:
    """The pool of the running loop, or `None` when none is open or reuse is switched off."""
    if settings.connector_session_ttl_seconds <= 0:
        return None
    return _POOLS.get(asyncio.get_running_loop())


@asynccontextmanager
async def pooling() -> AsyncIterator[ConnectorSessionPool | None]:
    """Keep connector sessions across turns for the life of this scope, then close them all.

    A no-op yielding `None` when `connector_session_ttl_seconds` is 0. A nested scope on the same
    loop shares the outer pool, which only the outer scope closes.
    """
    if settings.connector_session_ttl_seconds <= 0:
        yield None
        return
    loop = asyncio.get_running_loop()
    existing = _POOLS.get(loop)
    if existing is not None:
        yield existing
        return
    pool = ConnectorSessionPool()
    _POOLS[loop] = pool
    pool.start()
    try:
        yield pool
    finally:
        del _POOLS[loop]
        await pool.aclose()
