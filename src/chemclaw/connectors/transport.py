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

**"The next turn tries again" was every turn, for the whole outage, at full price**
(`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`). Degrading is the right
behaviour and paying `connector_open_timeout_seconds` to rediscover it is not: this process already
knows, because `connectors.health` probes the same fleet at startup and on every `/readyz`, and
because the previous turn's own failed open is a verdict too. So the open consults
`connectors.reachability` first and skips the dial while a recent verdict says the host is down.
The connector is still reported unreachable for that turn — the degradation notice, the log line
and `chemclaw_connectors_unreachable_total` are unchanged, because what a chemist and an operator
must be told does not depend on how we found out.

**And a call that times out is cancelled rather than merely abandoned.** The manifest's
`request_timeout` bounds this side's wait; on its own it bounds nothing on the connector's, because
the SDK raises locally and sends no `notifications/cancelled` while this session stays open for the
rest of the turn. `core.mcp_session.cancel_on_timeout` is what closes that, and it is installed
here on the same line of reasoning that made the session per-turn in the first place: work nobody
is waiting for is work a pod is spending on nobody.
"""

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.sessions import Connection, create_session
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.exceptions import McpError

from chemclaw.connectors.reachability import recently_unreachable, record_reachability
from chemclaw.core.config import settings
from chemclaw.core.mcp_session import cancel_on_timeout
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


def transport_failure(exc: BaseException) -> bool:
    """Whether `exc` says the *wire* failed, rather than the tool behind it.

    The one place that knows what the MCP transport's failures look like, exported so the policy
    layer (`agent.tool_authz.surface_domain_errors`) can word them without importing the
    transport's libraries — the same layering that keeps `mcp` and `httpx` out of `chemclaw.agent`.

    The distinction matters because the two failures deserve opposite advice. A tool-level error
    arrives as a *returned* `ToolMessage(status="error")` carrying the server's own words — the
    adapter never raises for those — so anything the transport raises is a timeout, a reset, a
    refused connection or a dead session: transient by nature, where "do not retry" (the generic
    branch's wording) is exactly wrong. `anyio` is matched by module rather than imported, because
    the stream/cancel-scope errors it raises out of the MCP client are transport failures and the
    import would be a new third-party edge for one isinstance.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(transport_failure(member) for member in exc.exceptions)
    if isinstance(exc, (TimeoutError, ConnectionError, McpError)):
        return True
    module = type(exc).__module__ or ""
    return module.startswith(("httpx", "anyio"))


def describe_failure(exc: BaseException) -> str:
    """Render `exc` as the cause an operator can act on, seeing through an `ExceptionGroup`.

    **A `TaskGroup`'s wrapper is not a diagnosis, and rendering it as one hid a whole class of
    configuration fault.** `create_session` opens the MCP client inside an `anyio` task group, so
    everything a handshake raises arrives wrapped: `type(exc).__name__` is `ExceptionGroup` and
    `str(exc)` is "unhandled errors in a TaskGroup (1 sub-exception)". Measured against a real
    connector with its bearer variable unset, the whole of what an operator saw was

        WARNING connector calc is unreachable (ExceptionGroup: unhandled errors in a TaskGroup
        (1 sub-exception)); its tools are unavailable this turn

    while the sub-exception was `MissingConnectorCredential` carrying the *name of the variable to
    set*. Two docstrings promised that error would be visible; the wrapper is where it went.

    Flattened recursively because a group may nest, and every leaf is rendered rather than the
    first: a handshake that failed for two reasons has two reasons, and picking one is how the
    interesting one gets dropped. An empty group — which anyio does not produce but the type
    permits — falls back to the group itself rather than to an empty string.
    """
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return "; ".join(describe_failure(member) for member in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def absorb_connect_failure(connector: str, exc: BaseException) -> None:
    """Treat `exc` as "this connector is absent this turn" — unless the caller cancelled us.

    The one decision both engines make about an unreachable connector, extracted so neither can
    drift from the other. Deliberately broad in what it absorbs and narrow in what it does: the
    failure family is wide (a refused TCP connection, a DNS miss, a TLS error, a timeout, an MCP
    `ToolException`, an `anyio` cancel-scope error from a half-finished handshake) and enumerating
    it means the next unlisted member silently restores the fatal behaviour.

    **What it says is the cause, not the wrapper** (`describe_failure`). Absorbing broadly is only
    defensible if the line left behind is diagnosable, and for everything raised inside the MCP
    client's task group it was not.

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
        "connector %s is unreachable (%s); its tools are unavailable this turn",
        connector,
        describe_failure(exc),
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

    It is **not** optional, and that is the fix for a real hole rather than a tightening. It used to
    be `tuple[str, ...] | None` with `None` meaning "everything this server offers", and an endpoint
    that omitted `tools:` produced exactly that — so the manifest that enumerated nothing got the
    server's whole surface, unclassified, which `agent.authz.side_effecting_call` then reported as
    read-only. `manifest._check_classification` now refuses an empty `tools` list, which leaves no
    way to build a spec without one; making the field total is what stops the state coming back.
    """

    name: str
    connection: Connection
    allowed_tools: tuple[str, ...]


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
    proceeds without it and the next turn tries again — unless a verdict from the last
    `connector_breaker_window_seconds` already says it is down, in which case the dial is skipped
    and the same outcome is reached for free.
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

        The wait is bounded by `connector_open_timeout_seconds`, and the bound is not redundant
        with the 5 s connect timeout: that one covers the TCP dial only, while the handshake —
        `initialize` plus `tools/list` — is bounded by the session's *read* timeout, which the
        manifest sizes for the slowest tool call (600 s for `calc`). A connector that accepts the
        socket and then never finishes its handshake used to hold every turn for that full read
        bound before the first token; past this bound it is an ordinary unreachable connector.

        **And the bound is not paid at all against a host already known to be down.** The open's
        outcome is recorded either way, so a dark connector costs one turn its open timeout rather
        than every turn of the outage, and a connector that comes back is readmitted by the next
        readiness sweep or by that verdict expiring (`connectors.reachability`).
        """
        if recently_unreachable(self._spec.name):
            # Not `absorb_connect_failure`: nothing was attempted, and a line saying the connector
            # is unreachable "(TimeoutError: …)" would describe a dial that never happened.
            # `connected` stays False because `_task` is None, so the caller reports and counts
            # this connector exactly as it reports one that was dialled and failed.
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
        except TimeoutError as exc:
            await self._shut_down()
            record_reachability(self._spec.name, reachable=False, dialled=True)
            absorb_connect_failure(self._spec.name, exc)
            return []
        except BaseException:
            # The caller was cancelled while we were connecting. The holder task owns a live cancel
            # scope, so it must be told to unwind on its own task rather than abandoned — an
            # orphaned task holding an MCP session is the leak this class exists to make impossible.
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

        Two properties, both load-bearing, from two separate defects. **Bounded**: the await used
        to be unbounded, which made the end of every turn hostage to the slowest session close — a
        connector that would not finish unwinding held the `AsyncExitStack` until the turn deadline
        cancelled everything. `wait_for` cancels the *holder* task at the bound and waits for that
        cancellation to land, so past it the holder is torn down rather than abandoned — the same
        "signal it on its own task" rule, with a clock on it.

        **The caller's own cancellation is not part of what this bound absorbs.** `await
        wait_for(...)` is the suspension point at which a cancellation of *this* task is
        delivered — a client that closed the tab, or the front door's
        `asyncio.timeout(service_turn_timeout_seconds)` (`api/routes/turns.py`) — and a blanket
        `suppress(CancelledError, ...)` swallowed it. Measured: the cancelled turn completed
        normally and ran the code after the teardown, so
        `run_turn`'s `except (GeneratorExit, asyncio.CancelledError)` rollback never ran and
        `asyncio.timeout.__aexit__`, which only converts to `TimeoutError` when it *receives* a
        `CancelledError`, let the turn run past its deadline. `_is_really_cancelled()` — the same
        discriminator `absorb_connect_failure` uses — tells that apart from the holder's own
        `anyio` cancel scope, which also raises `CancelledError` on its way out without anyone
        having cancelled this task; only the former is re-raised. `wait_for`'s own bound expiring
        raises `TimeoutError` on this task without touching its cancel count, so the two paths
        cannot be confused with each other.
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

        Everything that touches the cancel scope happens here, on this one task. The `finally` sets
        `_opened` unconditionally so a caller waiting on it is released whether the connector came
        up or not — a connector that hangs is bounded by the turn's own clock, but one that *fails*
        must never leave the turn waiting on an event nobody will set.
        """
        try:
            async with create_session(self._spec.connection) as session:
                handshake = await session.initialize()
                # A tool call that outlives the manifest's `request_timeout` must tell the server
                # to stop, not merely stop waiting: this session stays open for the rest of the
                # turn, so an abandoned call otherwise runs to completion on the connector's pod
                # with nobody holding the answer (`core.mcp_session.cancel_on_timeout`).
                cancel_on_timeout(session)
                self._tools = _stamped(
                    _allowed(
                        await load_mcp_tools(session),
                        self._spec.allowed_tools,
                        self._spec.name,
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


def _allowed(tools: list[BaseTool], allowed: tuple[str, ...], connector: str) -> list[BaseTool]:
    """Keep only the tools a connector's allow-list names, saying so when the server is short.

    `load_mcp_tools` returns whatever the server advertises, so the allow-list has to be applied
    here or a profile's narrowing would stop at the process boundary. There is no "no allow-list"
    case to fall through: a manifest may not declare an empty `tools` list, so what a server
    advertises beyond the declaration is dropped rather than bound.

    **A declared tool the server no longer serves is a phantom capability, and this is the only
    place in the process that can see one.** The intersection is silent by construction — a name in
    the manifest and not in the handshake simply produces no tool — while every *reader* of the
    manifest goes on counting it: `advertised_tool_names()`, `state_changing_tool_names()`, the
    plan gate's classification, `skill-validate`'s name resolution and the skills backend. Measured
    against a probe serving `echo` behind a manifest declaring `["echo", "does_not_exist"]`: one
    tool bound, zero log output, and `make connector-validate` exiting 0 — that gate imports the
    bundle's own `server/` module, and a bundle we do not run ships none, so for exactly the
    connectors most likely to drift it checks nothing and says so under `unverified_tool_surfaces`.
    The visible symptom is a skill offered for a tool the model can never call.

    **The connector stays usable, and that direction is argued rather than defaulted.** Marking it
    unhealthy would be fail-closed, and fail-closed is right where a *classification* can be wrong
    by omission — an unclassified state-changing tool slipping past the plan gate is the failure
    `manifest._check_classification` refuses to load for. This drift is not that: a tool nobody can
    call cannot be called around a gate, so the gate's partition is still sound over the surface
    that exists. What the other choice would cost is real and asymmetric — one renamed tool on a
    server would take *every* tool it still serves out of the turn, and out of every turn until an
    operator noticed — which inverts this module's own trade one function above. So: keep what is
    served, report the drift as a degradation with the count behind it, and leave the repair to the
    manifest or the server. `Chemclaw3-mcp`'s `assert_manifest_matches` is where the same
    disagreement is caught *before* a release, against its running server, which is the only place
    it can be caught for a bundle this repository does not build.
    """
    keep = set(allowed)
    bound = [tool for tool in tools if tool.name in keep]
    missing = sorted(keep - {tool.name for tool in tools})
    if missing:
        degraded(
            logger,
            "connector_tool_drift",
            "connector %s declares %d tool(s) its server does not advertise (%s); they are "
            "counted by the plan gate, the skills backend and every validator, and cannot be "
            "called. Its remaining %d tool(s) are bound as usual",
            connector,
            len(missing),
            ", ".join(missing),
            len(bound),
            exc_info=False,
        )
    return bound


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
    _record_schema_cost(connector, tools)
    return tools


#: What each connector's advertised tool schemas cost a turn, by connector name. Written at
#: handshake and read on scrape — a plain dict rather than a counter because the quantity is a
#: level, not a rate, and the last handshake is the truth about what a turn now binds.
_SCHEMA_TOKENS: dict[str, float] = {}


def _record_schema_cost(connector: str, tools: list[BaseTool]) -> None:
    """Publish what this connector's tool schemas add to every turn's prefix.

    **The half of the static prefix nothing could gate.** `tests/test_context_floor.py` ratchets
    every in-process tool schema a profile binds — 28,123 tokens on `default`, and a merge that
    added eighteen tools was caught by it at +32%. An *endpoint* tool's schema is not in this
    repository at all: it arrives from a running server at handshake, so a connector's docstrings
    grow what every turn pays, forever, with nothing here able to fail. That test says so about six
    `chem` tools and could do nothing about it.

    It cannot become a ratchet — the number is a property of a server this repository does not
    build — so it becomes a *measurement* instead, by connector, which is what lets a deployment
    see the whole floor rather than the half it happens to own. The sum of this family plus the
    ratcheted floor is what a turn costs before the chemist says anything.

    Never raises and never blocks a handshake: a connector that could not be measured contributes
    nothing to the family, which is the same reading as a connector that is not open.
    """
    try:
        # Imported here rather than at module scope: `connectors -> agent` is a declared edge, but
        # the agent imports this module to build its tool surface, and a module-scope import would
        # make the pair a real import cycle rather than a permitted one.
        from chemclaw.agent.context_budget import estimate_tool_schemas

        _SCHEMA_TOKENS[connector] = float(estimate_tool_schemas(tools))
    except Exception:
        logger.debug("could not measure %s's tool schemas", connector, exc_info=True)


METRICS.bind_gauge_family("chemclaw_connector_tool_schema_tokens", lambda: dict(_SCHEMA_TOKENS))
