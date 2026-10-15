"""A connector's MCP session is kept across turns, for one actor and one turn at a time.

Every test drives the real open path (`open_connector_specs`) against a real uvicorn server running
core's own `connector_app`, the shape the fleet's servers have: the caller identity is read from the
request headers of each tool call, so a kept session that stamped the identity of the turn that
opened it would be visible here as a tool answering with someone else's name.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Self, cast

import httpx
import pytest
from langchain_core.tools import BaseTool
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ListToolsRequest

from chemclaw.connectors import contract
from chemclaw.connectors.manifest import BearerAuth, ConnectorManifest, HttpEndpoint
from chemclaw.connectors.reachability import forget_reachability
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.session_pool import (
    ToolListings,
    active_pool,
    pooling,
)
from chemclaw.connectors.transport import ConnectorSpec, transport_failure
from chemclaw.core.call_identity import IdentityPin, pin_connection_identity, turn_identity_hook
from chemclaw.core.config import settings
from chemclaw.core.identity_context import set_current_correlation_id, set_current_identity
from chemclaw.core.session_context import set_current_session_id
from chemclaw.core.turn_flags import set_dry_run
from tests.conftest import _free_port
from tests.served import ServerThread

_TOOLS = ("whoami", "slow", "refuse")


@pytest.fixture(autouse=True)
def _fresh_verdicts() -> None:
    """One test's dark connector or probed contract must not decide the next one's open."""
    forget_reachability()
    contract.forget_contract_findings()


class ProbeServer:
    """A real `connector_app` on `port` that records what reached it, and can stop and restart.

    `whoami` answers with the raw headers of the very request that carried the call, which is what
    a fleet server reads; `inits` counts sessions minted, `deletes` sessions ended, `lists` the
    `tools/list` requests answered.
    """

    def __init__(
        self, port: int, *, build: str = "build-1", tools: tuple[str, ...] = _TOOLS
    ) -> None:
        """Describe a server for `port`; `build` is the revision its handshake reports."""
        self.port = port
        self.build = build
        self.tools = tools
        self.inits = 0
        self.deletes = 0
        self.lists = 0
        self._thread: ServerThread | None = None

    def start(self) -> Self:
        """Build a fresh app (a session manager runs once) and serve it."""
        server = FastMCP("probe")
        stats = self

        @server.tool()
        async def whoami(ctx: Context[Any, Any, Any]) -> str:
            """Say which identity headers carried this call."""
            request = ctx.request_context.request
            assert request is not None
            headers = request.headers
            return json.dumps(
                {
                    "actor": headers.get("x-chemclaw-actor"),
                    "session": headers.get("x-chemclaw-session"),
                    "correlation": headers.get("x-chemclaw-correlation-id"),
                    "dry_run": headers.get("x-chemclaw-dry-run"),
                }
            )

        @server.tool()
        async def slow() -> str:
            """Run for longer than any test waits."""
            await asyncio.sleep(30)
            return "late"

        @server.tool()
        async def refuse() -> str:
            """Refuse on purpose, with a domain message."""
            raise ValueError("not a thing this tool does")

        lowlevel = server._mcp_server
        lowlevel.version = self.build
        listing = lowlevel.request_handlers[ListToolsRequest]

        async def counting_list(request: ListToolsRequest) -> Any:
            stats.lists += 1
            return await listing(request)

        lowlevel.request_handlers[ListToolsRequest] = counting_list
        for name in _TOOLS:
            if name not in self.tools:
                server._tool_manager._tools.pop(name)

        app = connector_app(server, name="probe", token_env="POOL_PROBE_SERVER_TOKEN")

        @app.middleware("http")
        async def _count(request: Any, call_next: Any) -> Any:
            if request.url.path.endswith("/mcp"):
                if request.method == "POST" and "mcp-session-id" not in request.headers:
                    stats.inits += 1
                if request.method == "DELETE":
                    stats.deletes += 1
            return await call_next(request)

        # The pool holds a stream open, which uvicorn would otherwise wait out before stopping.
        self._thread = ServerThread(app, self.port, timeout_graceful_shutdown=1)
        self._thread.__enter__()
        return self

    def stop(self) -> None:
        """Stop serving; every session this server held is gone with it."""
        if self._thread is not None:
            self._thread.__exit__(None, None, None)
            self._thread = None

    def __enter__(self) -> Self:
        """Serve for the duration of a `with`."""
        return self.start()

    def __exit__(self, *exc: object) -> None:
        """Stop serving."""
        self.stop()


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bearer both sides read per request, as a deployment sets it."""
    monkeypatch.setenv("POOL_PROBE_SERVER_TOKEN", "t-1")
    monkeypatch.setenv("POOL_PROBE_CLIENT_TOKEN", "t-1")


def _spec(
    port: int, *, allowed: tuple[str, ...] = ("whoami",), timeout: int = 10, **fields: Any
) -> ConnectorSpec:
    """The connector as the registry builds it, pointed at `port` and presenting the bearer."""
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{port}/mcp",
        tools=list(allowed),
        read_only=list(allowed),
        request_timeout=timeout,
        auth=BearerAuth(mode="bearer", token_env="POOL_PROBE_CLIENT_TOKEN"),
    )
    spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name="probe")), endpoint)
    return replace(spec, **fields) if fields else spec


def _text(result: Any) -> str:
    """The text a tool answered with, whichever shape the adapter returns it in."""
    if isinstance(result, str):
        return result
    return "".join(block["text"] if isinstance(block, dict) else str(block) for block in result)


@dataclass
class Turn:
    """What one simulated turn saw: its tools, the unreachable names, and each call's answer."""

    tools: list[BaseTool]
    unreachable: list[str]
    answers: list[dict[str, Any]]


async def run_turn(
    specs: list[ConnectorSpec],
    *,
    actor: str,
    correlation: str,
    session: str = "chat-1",
    calls: int = 1,
    dry_run: bool = False,
    between: Callable[[], Awaitable[None]] | None = None,
) -> Turn:
    """One turn as the runner makes it: stamp the ambient, open the connectors, call, close.

    Run in a task of its own so its ambient identity cannot outlive it or reach another turn.
    """

    async def body() -> Turn:
        set_current_identity(actor, frozenset({"process-chemist"}))
        set_current_session_id(session)
        set_current_correlation_id(correlation)
        set_dry_run(dry_run)
        answers: list[dict[str, Any]] = []
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, specs)
            whoami = next((tool for tool in tools if tool.name == "whoami"), None)
            for _ in range(calls if whoami is not None else 0):
                if between is not None:
                    await between()
                assert whoami is not None
                answers.append(json.loads(_text(await whoami.ainvoke({}))))
        return Turn(tools, unreachable, answers)

    return await asyncio.create_task(body())


async def until(condition: Callable[[], bool], *, seconds: float = 10.0) -> None:
    """Wait for something a background close does, without a fixed sleep."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.02)


def _expect(
    actor: str, correlation: str, *, session: str = "chat-1", dry_run: bool = False
) -> dict[str, Any]:
    """The headers a call of this turn must have arrived with."""
    return {
        "actor": actor,
        "session": session,
        "correlation": correlation,
        "dry_run": "true" if dry_run else "false",
    }


def _surface(tools: list[BaseTool]) -> list[tuple[Any, ...]]:
    """Everything a model is shown of each tool, plus the provenance the audit reads."""
    return [(tool.name, tool.description, tool.args_schema, tool.metadata) for tool in tools]


# --- reuse across turns -------------------------------------------------------------------------


async def test_one_initialize_serves_many_turns_and_every_call_carries_its_own_turn() -> None:
    """Five turns, one session: the server minted one, and each call was stamped by its own turn."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is not None
            for turn_number in range(5):
                turn = await run_turn(
                    [spec], actor="alice", correlation=f"turn-{turn_number}", calls=2
                )
                assert turn.unreachable == []
                assert turn.answers == [_expect("alice", f"turn-{turn_number}")] * 2
            assert pool.size == 1
            assert server.inits == 1
            assert server.lists == 1
        await until(lambda: server.deletes == 1)


async def test_a_dry_run_flag_and_a_new_chat_session_are_this_turns_not_the_openers() -> None:
    """Dry-run and session id are per call as well, so a dry-run turn cannot leak into the next."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling():
            first = await run_turn(
                [spec], actor="alice", correlation="a", session="s1", dry_run=True
            )
            second = await run_turn([spec], actor="alice", correlation="b", session="s2")
            assert first.answers == [_expect("alice", "a", session="s1", dry_run=True)]
            assert second.answers == [_expect("alice", "b", session="s2")]
            assert server.inits == 1


async def test_the_pooled_surface_is_what_a_fresh_listing_returns() -> None:
    """Model-facing text is byte-identical: pooled, cache-built and per-turn tools agree."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port, allowed=_TOOLS)
        per_turn = await run_turn([spec], actor="alice", correlation="x")
        async with pooling():
            gate = asyncio.Event()

            async def hold() -> None:
                await gate.wait()

            # Two sessions for one actor at once: the second is built from the cached listing.
            first = asyncio.create_task(
                run_turn([spec], actor="alice", correlation="a", calls=1, between=hold)
            )
            await until(lambda: server.inits == 2 and server.lists == 2)
            second = asyncio.create_task(
                run_turn([spec], actor="alice", correlation="b", calls=1, between=hold)
            )
            await until(lambda: server.inits == 3)
            gate.set()
            listed = await asyncio.gather(first, second)
            reused = await run_turn([spec], actor="alice", correlation="c")
        assert server.lists == 2, "the third session asked the server for its tools again"
        for turn in (*listed, reused):
            assert _surface(turn.tools) == _surface(per_turn.tools)


# --- identity ------------------------------------------------------------------------------------


async def test_concurrent_actors_on_one_connector_never_see_each_others_identity() -> None:
    """Many actors, many turns, interleaved calls: each call arrives with its own turn's headers."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)

        async def staggered() -> None:
            await asyncio.sleep(0.003)

        async def actor_turns(actor: str) -> list[tuple[str, Turn]]:
            seen = []
            for turn_number in range(4):
                correlation = f"{actor}-{turn_number}"
                seen.append(
                    (
                        correlation,
                        await run_turn(
                            [spec],
                            actor=actor,
                            correlation=correlation,
                            session=f"chat-{actor}",
                            calls=5,
                            dry_run=actor == "dave",
                            between=staggered,
                        ),
                    )
                )
            return seen

        actors = ["alice", "bob", "carol", "dave"]
        async with pooling() as pool:
            assert pool is not None
            results = await asyncio.gather(*(actor_turns(actor) for actor in actors))
            for actor, turns in zip(actors, results, strict=True):
                for correlation, turn in turns:
                    expected = _expect(
                        actor, correlation, session=f"chat-{actor}", dry_run=actor == "dave"
                    )
                    assert turn.answers == [expected] * 5, (actor, correlation)
            assert server.inits == len(actors), "each actor should have kept exactly one session"
            assert pool.size == len(actors)


async def test_two_turns_of_one_actor_at_once_use_two_sessions() -> None:
    """A session is checked out, not shared, so overlapping turns cannot stamp over each other."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        arrived = asyncio.Event()
        waiting = 0

        async def rendezvous() -> None:
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                arrived.set()
            await arrived.wait()

        async with pooling() as pool:
            assert pool is not None
            turns = await asyncio.gather(
                run_turn([spec], actor="alice", correlation="one", calls=3, between=rendezvous),
                run_turn([spec], actor="alice", correlation="two", calls=3, between=rendezvous),
            )
            assert [turn.answers for turn in turns] == [
                [_expect("alice", "one")] * 3,
                [_expect("alice", "two")] * 3,
            ]
            assert server.inits == 2
            assert pool.size == 1, "only one idle session is kept per actor and connector"


async def test_a_kept_connection_stamps_its_pin_and_reads_nothing_from_its_opener() -> None:
    """The hook of a pinned connection ignores the ambient turn, and the pin alone decides."""
    hook = turn_identity_hook("http://127.0.0.1:8860/mcp")
    pin = IdentityPin({"X-Chemclaw-Actor": "pinned-actor"})
    seen: list[httpx.Request] = []

    async def stamp_under_pin() -> None:
        pin_connection_identity(pin)
        for _ in range(2):
            request = httpx.Request("POST", "http://127.0.0.1:8860/mcp")
            await hook(request)
            seen.append(request)
            pin.headers = {}

    set_current_identity("ambient-actor", frozenset())
    import contextvars

    await asyncio.create_task(stamp_under_pin(), context=contextvars.Context())
    assert seen[0].headers["X-Chemclaw-Actor"] == "pinned-actor"
    assert "X-Chemclaw-Actor" not in seen[1].headers


# --- a dead or restarted server ------------------------------------------------------------------


async def test_a_restarted_server_is_met_with_one_fresh_session_in_the_same_turn() -> None:
    """The kept session is gone with the server; the next turn replaces it without a word."""
    port = _free_port()
    first = ProbeServer(port).start()
    spec = _spec(port)
    async with pooling() as pool:
        assert pool is not None
        await run_turn([spec], actor="alice", correlation="before")
        first.stop()
        second = ProbeServer(port).start()
        try:
            turn = await run_turn([spec], actor="alice", correlation="after")
            assert turn.unreachable == []
            assert turn.answers == [_expect("alice", "after")]
            assert second.inits == 1
            assert pool.size == 1
        finally:
            second.stop()


async def test_a_server_that_is_down_costs_the_connector_not_the_turn_and_it_comes_back() -> None:
    """Down: absent and not kept. Up again: the next turn opens it and keeps it."""
    port = _free_port()
    first = ProbeServer(port).start()
    spec = _spec(port)
    async with pooling() as pool:
        assert pool is not None
        await run_turn([spec], actor="alice", correlation="up")
        first.stop()
        down = await run_turn([spec], actor="alice", correlation="down")
        assert down.unreachable == ["probe"] and down.tools == []
        assert pool.size == 0
        forget_reachability()
        with ProbeServer(port) as again:
            back = await run_turn([spec], actor="alice", correlation="back")
            assert back.unreachable == [] and back.answers == [_expect("alice", "back")]
            assert again.inits == 1 and pool.size == 1


async def test_a_server_stopped_during_a_turn_fails_that_call_as_a_transport_fault() -> None:
    """The error a chemist-facing layer words as "try again" is still what a dead server raises."""
    port = _free_port()
    server = ProbeServer(port).start()
    spec = _spec(port, timeout=2)
    try:
        async with pooling():
            async with AsyncExitStack() as stack:
                tools, _ = await open_connector_specs(stack, [spec])
                whoami = next(tool for tool in tools if tool.name == "whoami")
                server.stop()
                with pytest.raises(BaseException) as raised:
                    await asyncio.wait_for(whoami.ainvoke({}), 20)
                assert transport_failure(raised.value), repr(raised.value)
    finally:
        server.stop()


# --- expiry, eviction, shutdown ------------------------------------------------------------------


async def test_an_idle_session_is_closed_when_its_ttl_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL expiry closes the idle session at the server and the next turn opens a new one."""
    monkeypatch.setattr(settings, "connector_session_ttl_seconds", 0.4)
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is not None
            await run_turn([spec], actor="alice", correlation="one")
            assert pool.size == 1
            await until(lambda: pool.size == 0 and server.deletes == 1)
            turn = await run_turn([spec], actor="alice", correlation="two")
            assert turn.answers == [_expect("alice", "two")]
            assert server.inits == 2


async def test_a_session_past_its_ttl_is_not_reused_even_before_the_sweep_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout checks age itself, so a late sweep cannot hand out an expired session."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is not None
            await run_turn([spec], actor="alice", correlation="one")
            (kept,) = pool._idle.values()
            kept.idle_since -= settings.connector_session_ttl_seconds + 1
            turn = await run_turn([spec], actor="alice", correlation="two")
            assert turn.answers == [_expect("alice", "two")]
            assert server.inits == 2 and pool.size == 1


async def test_the_least_recently_used_idle_session_is_evicted_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three actors against a ceiling of two keep two, and the oldest is closed at the server."""
    monkeypatch.setattr(settings, "connector_session_pool_max", 2)
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is not None
            for actor in ("alice", "bob", "carol"):
                await run_turn([spec], actor=actor, correlation=actor)
            assert pool.size == 2
            await until(lambda: server.deletes == 1)
            assert {key[1] for key in pool._idle} == {"bob", "carol"}
            # Alice comes back: her session was evicted, so she gets a new one and Bob loses his.
            again = await run_turn([spec], actor="alice", correlation="again")
            assert again.answers == [_expect("alice", "again")]
            assert server.inits == 4 and pool.size == 2


async def test_a_full_pool_of_busy_sessions_runs_the_next_turn_unpooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing blocks and nothing grows: the overflow turn gets a private session of its own."""
    monkeypatch.setattr(settings, "connector_session_pool_max", 1)
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        release = asyncio.Event()

        async def hold() -> None:
            await release.wait()

        async with pooling() as pool:
            assert pool is not None
            busy = asyncio.create_task(
                run_turn([spec], actor="alice", correlation="a", calls=1, between=hold)
            )
            await until(lambda: pool.size == 1 and server.inits == 1)
            overflow = await run_turn([spec], actor="bob", correlation="b")
            assert overflow.answers == [_expect("bob", "b")]
            await until(lambda: server.deletes == 1)
            assert pool.size == 1
            release.set()
            assert (await busy).answers == [_expect("alice", "a")]
            assert pool.size == 1


async def test_leaving_the_pooling_scope_closes_every_session() -> None:
    """Shutdown ends each session at the server and leaves no holder task behind."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling():
            for actor in ("alice", "bob", "carol"):
                await run_turn([spec], actor=actor, correlation=actor)
            assert server.inits == 3
        assert server.deletes == 3
        assert active_pool() is None
        leftovers = [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("connector")]
        assert leftovers == []


async def test_a_turn_that_is_cancelled_mid_call_does_not_hand_its_session_on() -> None:
    """A request abandoned in flight may still run at the server, so that session is closed."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port, allowed=_TOOLS)
        async with pooling() as pool:
            assert pool is not None

            async def cancelled_turn() -> None:
                set_current_identity("alice", frozenset())
                async with AsyncExitStack() as stack:
                    tools, _ = await open_connector_specs(stack, [spec])
                    slow = next(tool for tool in tools if tool.name == "slow")
                    await asyncio.wait_for(slow.ainvoke({}), 0.3)

            with pytest.raises(TimeoutError):
                await asyncio.create_task(cancelled_turn())
            assert pool.size == 0
            turn = await run_turn([spec], actor="alice", correlation="next")
            assert turn.answers == [_expect("alice", "next")]
            assert server.inits == 2


# --- the tool listing ----------------------------------------------------------------------------


def test_a_listing_is_valid_for_its_key_and_for_the_ttl_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different contract version, build or URL is a miss, and so is a listing past the TTL."""
    monkeypatch.setattr(settings, "connector_session_ttl_seconds", 60.0)
    clock = [1000.0]
    monkeypatch.setattr("chemclaw.connectors.session_pool.time.monotonic", lambda: clock[0])
    listings = ToolListings()
    key = ("http://x/mcp", "1.0.0", "build-1")
    listings.put("probe", key, [])
    assert listings.get("probe", key) == []
    assert listings.get("probe", ("http://x/mcp", "1.1.0", "build-1")) is None
    assert listings.get("probe", ("http://x/mcp", "1.0.0", "build-2")) is None
    assert listings.get("probe", ("http://y/mcp", "1.0.0", "build-1")) is None
    clock[0] += 61
    assert listings.get("probe", key) is None
    listings.put("probe", key, [])
    listings.forget("probe")
    assert listings.get("probe", key) is None


async def test_a_new_contract_version_relists_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server comes back as another contract version with another tool: the turn sees it."""
    served = {"version": "1.0.0"}

    async def probe(url: str, budget: float) -> str:
        return served["version"]

    monkeypatch.setattr(contract, "served_version", probe)
    port = _free_port()
    spec = _spec(
        port,
        allowed=_TOOLS,
        contract_version="1.0.0",
        health_url=f"http://127.0.0.1:{port}/healthz",
    )
    first = ProbeServer(port, tools=("whoami", "slow")).start()
    async with pooling():
        before = await run_turn([spec], actor="alice", correlation="v1")
        assert {tool.name for tool in before.tools} == {"whoami", "slow"}
        first.stop()
        served["version"] = "1.1.0"
        contract.forget_contract_findings()
        with ProbeServer(port, tools=_TOOLS) as second:
            after = await run_turn([spec], actor="alice", correlation="v2")
            assert {tool.name for tool in after.tools} == set(_TOOLS)
            assert second.lists == 1


async def test_the_listing_is_asked_for_again_when_the_ttl_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change the key cannot show is bounded by the TTL: a new session past it lists afresh."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        release = asyncio.Event()

        async def hold() -> None:
            await release.wait()

        async with pooling():
            first = asyncio.create_task(
                run_turn([spec], actor="alice", correlation="a", calls=1, between=hold)
            )
            await until(lambda: server.inits == 1)
            monkeypatch.setattr(settings, "connector_session_ttl_seconds", 0.01)
            await asyncio.sleep(0.05)
            second = await run_turn([spec], actor="alice", correlation="b")
            release.set()
            await first
            assert second.answers == [_expect("alice", "b")]
            assert server.lists == 2


# --- credentials, failure wording, switches ------------------------------------------------------


async def test_a_rotated_bearer_is_read_per_request_and_a_refused_one_costs_only_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotation needs no restart; a credential the server refuses reads as absent, then recovers."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is not None
            await run_turn([spec], actor="alice", correlation="one")
            monkeypatch.setenv("POOL_PROBE_SERVER_TOKEN", "t-2")
            monkeypatch.setenv("POOL_PROBE_CLIENT_TOKEN", "t-2")
            rotated = await run_turn([spec], actor="alice", correlation="two")
            assert rotated.answers == [_expect("alice", "two")]
            assert server.inits == 1, "a rotation the server accepted must not drop the session"

            monkeypatch.setenv("POOL_PROBE_CLIENT_TOKEN", "revoked")
            refused = await run_turn([spec], actor="alice", correlation="three")
            assert refused.unreachable == ["probe"] and refused.tools == []
            assert pool.size == 0

            forget_reachability()
            monkeypatch.setenv("POOL_PROBE_CLIENT_TOKEN", "t-2")
            healed = await run_turn([spec], actor="alice", correlation="four")
            assert healed.answers == [_expect("alice", "four")]


async def test_a_tool_that_refuses_answers_the_same_pooled_and_per_turn() -> None:
    """The model reads one wording for a refusal, whichever way the session was obtained."""
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port, allowed=_TOOLS)

        async def refusal() -> str:
            async with AsyncExitStack() as stack:
                tools, _ = await open_connector_specs(stack, [spec])
                refuse = next(tool for tool in tools if tool.name == "refuse")
                try:
                    return _text(await refuse.ainvoke({}))
                except Exception as exc:
                    return f"{type(exc).__name__}: {exc}"

        per_turn = await asyncio.create_task(refusal())
        async with pooling():
            first = await asyncio.create_task(refusal())
            reused = await asyncio.create_task(refusal())
        assert "not a thing this tool does" in per_turn
        assert first == per_turn == reused


async def test_a_ttl_of_zero_switches_reuse_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every turn opens and closes its own session, as before the pool existed."""
    monkeypatch.setattr(settings, "connector_session_ttl_seconds", 0.0)
    with ProbeServer(_free_port()) as server:
        spec = _spec(server.port)
        async with pooling() as pool:
            assert pool is None and active_pool() is None
            for number in range(3):
                turn = await run_turn([spec], actor="alice", correlation=str(number))
                assert turn.answers == [_expect("alice", str(number))]
        assert server.inits == 3 and server.deletes == 3


async def test_no_connectors_is_still_no_connectors() -> None:
    """A turn with nothing to open behaves exactly as before, pool or not."""
    async with pooling():
        async with AsyncExitStack() as stack:
            assert await open_connector_specs(stack, []) == ([], [])


async def test_the_front_door_keeps_sessions_for_its_lifespan_and_closes_them_with_it() -> None:
    """The service's lifespan opens the scope and closes it before the stores the turns write to."""
    from chemclaw.api.app import create_app

    app = create_app(connector_factory=lambda _profile: [])
    assert active_pool() is None
    async with app.router.lifespan_context(app):
        assert active_pool() is not None
    assert active_pool() is None


def test_a_worker_enters_the_same_scope_as_the_front_door() -> None:
    """A template step opens every connector for one call, so the worker keeps sessions too."""
    from pathlib import Path

    from chemclaw.durable import serve

    source = Path(serve.__file__).read_text(encoding="utf-8")
    assert "connector_sessions.pooling()" in source
