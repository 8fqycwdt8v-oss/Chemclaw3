"""Keep a connector's MCP session across turns, lent to one caller's one turn at a time.

A turn otherwise pays a dial, `initialize`, `tools/list`, a standalone stream and a `DELETE` per
connector. The pool keeps the session after the turn for the same actor's next one, and keeps each
connector's tool listing so a new session does not re-ask.

Invariants:
- A session serves one turn at a time, so no request carries two callers' identities. It is keyed
  by actor, connector and endpoint; its task runs in an empty context and stamps an `IdentityPin`
  replaced at every checkout. A turn's tools are bound to that checkout's lease: once the lease
  ends, a tool still held by the turn is refused as a closed session would be.
- A reused session must be up, unabandoned, no older than `connector_session_ttl_seconds` since
  its tool listing was taken, built from the connector's current listing, and answer a ping.
  Otherwise it is closed and one fresh session opened.
- A listing is reused only under the same URL, contract version and server build, and only while
  younger than the TTL. A server that reports no build (`unknown`) is listed afresh by every new
  session; a listing whose fingerprint changed retires the sessions built from the old one.
- Bounded: at most `connector_session_pool_max` sessions, counting those being checked out; idle
  ones expire after the TTL, a lease held past twice the turn ceiling is reaped, the least
  recently used idle one is evicted first, and a turn finding the pool full of busy sessions runs
  on an unpooled one.
"""

import asyncio
import contextvars
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection
from langchain_mcp_adapters.tools import MAX_ITERATIONS, convert_mcp_tool_to_langchain_tool
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import (
    CONNECTION_CLOSED,
    ErrorData,
    InitializeResult,
    ServerNotification,
    ToolListChangedNotification,
)
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
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# What makes two specs the same endpoint: URL, call bound, queued tools, and the contract the
# manifest declares and where it is checked.
_Signature = tuple[str, float, str, str | None, str | None]
_Key = tuple[str, str, _Signature]
_ListingKey = tuple[str, str | None, str]

# The code `cancel_on_timeout` reads on the SDK's local read timeout (pinned in
# `tests/test_upstream_surface.py`): a request abandoned while the server may still run it.
_READ_TIMEOUT_CODE = 408

# Floor on the sweeper's sleep, so a tiny TTL cannot spin the loop.
_MIN_SWEEP_SECONDS = 0.05

# Idle sessions kept per (actor, connector): a second one lets two overlapping turns of one actor
# (two tabs, or a turn beside a template step) each find a warm session instead of one of them
# opening a new one every time. More would be sessions nobody asks for.
_IDLE_PER_KEY = 2

# A lease is reaped after this many turn ceilings: a turn is bounded by
# `service_turn_timeout_seconds`, so a lease older than twice that belongs to a turn that never
# gave it back.
_LEASE_TURN_CEILINGS = 2

# What `serverInfo.version` says when the server was not told its build (`MCP_SERVER_REVISION`
# unset): it then cannot tell two builds apart, so a listing keyed on it proves nothing.
_UNKNOWN_BUILDS = frozenset({"", "unknown"})


def _url(spec: ConnectorSpec) -> str:
    """The endpoint URL of an HTTP spec."""
    return str(cast(dict[str, Any], spec.connection).get("url", ""))


def _signature(spec: ConnectorSpec) -> _Signature:
    """The parts of `spec` that fix what a session to it is: everything but the allow-list."""
    queued = spec.queued.model_dump_json() if spec.queued is not None else ""
    return (_url(spec), spec.request_timeout, queued, spec.contract_version, spec.health_url)


def poolable(spec: ConnectorSpec) -> bool:
    """Whether a spec is reached over streamable HTTP, which carries the identity headers."""
    return spec.connection["transport"] == "streamable_http"


async def _list_all(session: ClientSession) -> list[McpTool]:
    """Every tool the server advertises, following its pagination cursor.

    Bounded as the adapter's own listing is (`MAX_ITERATIONS`, pinned in
    `tests/test_upstream_surface.py`), so a server that never stops paginating fails the open.
    """
    tools: list[McpTool] = []
    cursor: str | None = None
    for _ in range(MAX_ITERATIONS):
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if not page.nextCursor:
            return tools
        cursor = page.nextCursor
    raise RuntimeError(f"Reached max of {MAX_ITERATIONS} iterations while listing tools.")


def _fingerprint(tools: list[McpTool]) -> str:
    """A digest of what the model is shown of each tool: name, description and both schemas."""
    shown = sorted(
        (tool.name, tool.description, tool.inputSchema, tool.outputSchema) for tool in tools
    )
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode()).hexdigest()


class ToolListings:
    """Each connector's last `tools/list`, valid for one `(url, contract version, server build)`.

    One entry per connector, so memory is bounded by the fleet. A hit also requires the listing to
    be younger than the session TTL. Each connector has a generation that moves when a listing's
    fingerprint differs from the previous one or the listing is forgotten, so sessions built from
    the old listing can tell they are out of date.
    """

    def __init__(self) -> None:
        """Start with nothing listed."""
        self._held: dict[str, tuple[_ListingKey, float, str, list[McpTool]]] = {}
        self._generations: dict[str, int] = {}

    def get(self, connector: str, key: _ListingKey) -> tuple[list[McpTool], float] | None:
        """A private copy of the listing taken under `key` and when it was taken, or `None`."""
        held = self._held.get(connector)
        if held is None or held[0] != key:
            return None
        if time.monotonic() - held[1] >= settings.connector_session_ttl_seconds:
            return None
        return [tool.model_copy(deep=True) for tool in held[3]], held[1]

    def put(self, connector: str, key: _ListingKey, tools: list[McpTool]) -> float:
        """Remember a fresh listing, replacing the previous one; returns when it was taken."""
        fingerprint = _fingerprint(tools)
        previous = self._held.get(connector)
        if previous is not None and previous[2] != fingerprint:
            self._generations[connector] = self.generation(connector) + 1
            logger.info("connector %s: its tool listing changed; older sessions retire", connector)
        taken = time.monotonic()
        copies = [tool.model_copy(deep=True) for tool in tools]
        self._held[connector] = (key, taken, fingerprint, copies)
        return taken

    def forget(self, connector: str) -> None:
        """Drop a connector's listing and retire the sessions built from it."""
        self._held.pop(connector, None)
        self._generations[connector] = self.generation(connector) + 1

    def generation(self, connector: str) -> int:
        """Which listing generation sessions of this connector must have been built from."""
        return self._generations.get(connector, 0)


class _Lease:
    """The token of one checkout; a session's current lease is the only one its tools may use."""

    __slots__ = ()


class _LeaseGate:
    """What a checkout's tools are bound to in place of the session: `call_tool`, if still leased.

    The tools of a turn are built per checkout. One the turn keeps past its checkin, or a retained
    copy called while another turn holds the session, would go out on that turn's identity, so it
    is refused instead, with the error a closed session gives.
    """

    def __init__(self, owner: "KeptConnectorSession", lease: _Lease) -> None:
        """Bind the gate to the session and the checkout it serves."""
        self._owner = owner
        self._lease = lease

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        """Call the tool on the session when this gate's lease is the session's current one."""
        session = self._owner.live_session(self._lease)
        return await session.call_tool(*args, **kwargs)


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
        self.lease: _Lease | None = None
        self._listings = listings
        self._pin = IdentityPin()
        self._session: ClientSession | None = None
        self._templates: list[BaseTool] = []
        self._mcp_tools: dict[str, McpTool] = {}
        self._costed: tuple[str, ...] = ()
        self._listed_at = 0.0
        self._generation = 0

    def stamp(self) -> None:
        """Make this session's requests carry the calling turn's identity from now on."""
        self._pin.headers = turn_headers()

    def live_session(self, lease: _Lease) -> ClientSession:
        """The session, for the checkout that holds `lease`; anyone else is refused.

        Raises:
            McpError: `lease` is not the session's current one, as a closed session would say.
        """
        if lease is not self.lease or self._session is None:
            raise McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed"))
        return self._session

    def tools_for(self, spec: ConnectorSpec) -> list[BaseTool]:
        """This checkout's tools: the allow-listed ones, bound to the current lease.

        Built here rather than shared between turns, by the adapter's converter, then given the
        text the stamped templates carry, so the model sees what a fresh listing would show.
        """
        if self.lease is None:
            raise RuntimeError("tools_for needs a session that has been leased")
        gate = cast(ClientSession, _LeaseGate(self, self.lease))
        interceptors = _interceptors(self._spec)
        tools: list[BaseTool] = []
        for template in _allowed(self._templates, spec.allowed_tools):
            tool = convert_mcp_tool_to_langchain_tool(
                gate, self._mcp_tools[template.name], tool_interceptors=interceptors
            )
            tool.description = template.description
            tool.args_schema = template.args_schema
            tool.metadata = template.metadata
            tools.append(tool)
        names = tuple(tool.name for tool in tools)
        if names != self._costed:
            _record_schema_cost(spec.name, tools)
            self._costed = names
        return tools

    @property
    def keepable(self) -> bool:
        """Whether the session may serve another turn: up, current, and nothing of it in doubt."""
        task = self._task
        return (
            task is not None
            and not task.done()
            and self._failure is None
            and self._session is not None
            and not self.tainted
            and not self.inflight
            and not self.list_changed
            and self._generation == self._listings.generation(self._spec.name)
            and time.monotonic() - self._listed_at < settings.connector_session_ttl_seconds
        )

    async def answers(self) -> bool:
        """Whether the server still holds this session and speaks for the declared contract.

        One ping, bounded by `connector_health_timeout_seconds`. Any failure (a restarted server
        answers 404, a dead one nothing) is "no": the caller opens a fresh session. A pod too busy
        to answer within the bound costs the reuse, not the turn.
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
        self.lease = None
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
        """Stamp every advertised tool once, from the cached listing when the handshake allows."""
        cancel_on_timeout(session)
        self._track(session)
        self._session = session
        name = self._spec.name
        revision = handshake.serverInfo.version
        key = (_url(self._spec), remembered_version(name) or self._spec.contract_version, revision)
        cached = None if revision in _UNKNOWN_BUILDS else self._listings.get(name, key)
        if cached is not None:
            advertised, self._listed_at = cached
            # The SDK validates a result against the output schema `list_tools` recorded and, for a
            # tool it never listed, lists again before the first call. A cached listing seeds that
            # record, so the first call neither pays the listing nor skips the validation.
            for tool in advertised:
                session._tool_output_schemas[tool.name] = tool.outputSchema
        else:
            advertised = await _list_all(session)
            self._listed_at = self._listings.put(name, key, advertised)
        self._generation = self._listings.generation(name)
        self._mcp_tools = {tool.name: tool for tool in advertised}
        interceptors = _interceptors(self._spec)
        self._templates = _stamped(
            [
                convert_mcp_tool_to_langchain_tool(session, tool, tool_interceptors=interceptors)
                for tool in advertised
            ],
            connector=name,
            revision=revision,
        )
        self._costed = tuple(tool.name for tool in self._templates)
        return self._templates

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
    """The process's kept sessions: idle ones by key, leased ones, and ones being checked out."""

    def __init__(self) -> None:
        """Start empty; `start()` begins the expiry sweep."""
        self._idle: dict[_Key, list[KeptConnectorSession]] = {}
        self._leased: dict[KeptConnectorSession, float] = {}
        # Sessions taken off the idle list to be pinged, or being opened: counted so concurrent
        # checkouts cannot together pass the ceiling.
        self._checking: set[KeptConnectorSession] = set()
        self._listings = ToolListings()
        self._closing: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def size(self) -> int:
        """Sessions the pool holds: idle, leased and being checked out."""
        return sum(map(len, self._idle.values())) + len(self._leased) + len(self._checking)

    def start(self) -> None:
        """Begin closing idle sessions that outlive the TTL, so a quiet process holds none."""
        self._sweeper = asyncio.create_task(self._sweep(), name="connector-session-sweeper")

    def session(self, spec: ConnectorSpec) -> PooledConnectorSession:
        """A lease on a session for `spec`, taken when entered."""
        return PooledConnectorSession(self, spec)

    async def checkout(self, spec: ConnectorSpec) -> KeptConnectorSession:
        """A session for the current actor and `spec`: a kept one if it answers, else a new one.

        Never raises for an unreachable connector; the returned session is then not `connected`,
        exactly as a per-turn open reports it. A kept session that fails its checks is replaced
        once, in this call.
        """
        key: _Key = (spec.name, get_current_actor() or "", _signature(spec))
        kept = self._take_idle(key)
        if kept is not None:
            self._checking.add(kept)
            try:
                good = await self._still_good(kept)
            except BaseException:
                self._checking.discard(kept)
                raise
            if good:
                _count_checkout(spec.name, "reused")
                return self._lease(kept)
            self._checking.discard(kept)
            logger.info("connector %s: the kept session is gone; opening a fresh one", spec.name)
        fresh = KeptConnectorSession(spec, key, self._listings)
        fresh.stamp()
        fresh.pooled = self._reserve(fresh)
        try:
            await fresh.__aenter__()
        except BaseException:
            self._checking.discard(fresh)
            raise
        if not fresh.connected:
            self._checking.discard(fresh)
            return fresh
        if not fresh.pooled:
            _count_checkout(spec.name, "unpooled")
        else:
            _count_checkout(spec.name, "opened" if kept is None else "replaced")
        return self._lease(fresh)

    async def checkin(self, session: KeptConnectorSession) -> None:
        """Take a session back: idle for the next turn if it is fit, else closed."""
        self._leased.pop(session, None)
        session.lease = None
        if (
            session.pooled
            and not self._closed
            and settings.connector_session_ttl_seconds > 0
            and session.keepable
        ):
            session.idle_since = time.monotonic()
            bucket = self._idle.setdefault(session.key, [])
            bucket.append(session)
            while len(bucket) > _IDLE_PER_KEY:
                self._close_later(bucket.pop(0))
            return
        await session.close()

    async def aclose(self) -> None:
        """Close every session, idle, leased or being checked out, and stop the sweep."""
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
        sessions = [
            *(session for bucket in self._idle.values() for session in bucket),
            *self._leased,
            *self._checking,
        ]
        self._idle.clear()
        self._leased.clear()
        self._checking.clear()
        await asyncio.gather(
            *(session.close() for session in sessions), *self._closing, return_exceptions=True
        )

    def expire(self) -> None:
        """Close idle sessions past the TTL or no longer fit, and reap leases held far too long."""
        now = time.monotonic()
        ttl = settings.connector_session_ttl_seconds
        for key, bucket in list(self._idle.items()):
            for session in list(bucket):
                if now - session.idle_since >= ttl or not session.keepable:
                    bucket.remove(session)
                    self._close_later(session)
            if not bucket:
                del self._idle[key]
        deadline = _LEASE_TURN_CEILINGS * settings.service_turn_timeout_seconds
        for session, since in list(self._leased.items()):
            if now - since >= deadline:
                del self._leased[session]
                logger.warning(
                    "connector %s: a session lease was held for %.0f s, past twice the turn "
                    "ceiling; closing it as leaked",
                    session.key[0],
                    now - since,
                )
                self._close_later(session)

    def _take_idle(self, key: _Key) -> KeptConnectorSession | None:
        """The most recently used idle session of this key, removed from the idle list."""
        bucket = self._idle.get(key)
        if not bucket:
            return None
        session = bucket.pop()
        if not bucket:
            del self._idle[key]
        return session

    def _lease(self, session: KeptConnectorSession) -> KeptConnectorSession:
        """Hand a session to the calling turn under a new lease."""
        self._checking.discard(session)
        session.lease = _Lease()
        if session.pooled:
            self._leased[session] = time.monotonic()
        return session

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

    def _reserve(self, session: KeptConnectorSession) -> bool:
        """Whether the pool will keep this new session, closing the oldest idle one to make room.

        The slot is counted from here, before the session is opened.
        """
        if self._closed:
            return False
        if self.size >= settings.connector_session_pool_max:
            oldest = self._take_oldest_idle()
            if oldest is None:
                return False
            self._close_later(oldest)
        self._checking.add(session)
        return True

    def _take_oldest_idle(self) -> KeptConnectorSession | None:
        """Remove and return the idle session unused for longest, or `None` when none is idle."""
        candidates = [
            (session.idle_since, key, session)
            for key, bucket in self._idle.items()
            for session in bucket
        ]
        if not candidates:
            return None
        _, key, oldest = min(candidates, key=lambda item: item[0])
        self._idle[key].remove(oldest)
        if not self._idle[key]:
            del self._idle[key]
        return oldest

    def _close_later(self, session: KeptConnectorSession) -> None:
        """Close a session off the caller's path, keeping the task referenced until it ends."""
        task = asyncio.create_task(session.close(), name=f"connector-close:{session.key[0]}")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _sweep(self) -> None:
        """Expire sessions for as long as the pool lives."""
        while True:
            await asyncio.sleep(max(settings.connector_session_ttl_seconds / 2, _MIN_SWEEP_SECONDS))
            self.expire()


def _count_checkout(connector: str, outcome: str) -> None:
    """Book how a turn came by its session; an unreachable connector books nothing here."""
    record_metric(
        lambda m: m.increment(
            "chemclaw_connector_session_checkouts_total",
            labels={"connector": connector, "outcome": outcome},
        )
    )


_POOLS: dict[asyncio.AbstractEventLoop, ConnectorSessionPool] = {}

# Bound at import, across every loop's pool, so any process that keeps sessions publishes it.
METRICS.bind_gauge(
    "chemclaw_connector_sessions_kept", lambda: float(sum(p.size for p in list(_POOLS.values())))
)


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
