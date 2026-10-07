"""The ASGI front door: a chat service over the Chemclaw agent.

`create_app` is the composition root and nothing else: it seeds `app.state`, installs the
middleware, binds the gauges and registers the routes. Routes live in `chemclaw/api/routes/`,
shapes in `api/schemas.py`, state types and turn leases in `api/state.py`, authorization in
`api/deps.py` and HTTP armour in `api/middleware.py`. Every route reads the process's live
structures through `request.app.state`, never by lexical capture. The graph factory is injectable,
so tests drive the whole app without a model or credentials. The bundled chat UI at `/` is served
only when identity is not enforced.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from chemclaw.agent.audit import NullAuditSink, default_audit_sink
from chemclaw.agent.checkpointer import close_checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs, history_provider
from chemclaw.agent.durable_tools import cancel_job, job_status
from chemclaw.agent.graph_tools import expand_note
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile, registered_profile_names
from chemclaw.agent.session_events import stream_new_events
from chemclaw.agent.subagents import refuse_an_unknown_roster
from chemclaw.agent.turn_graph import refuse_an_unknown_peer_roster
from chemclaw.agent.turn_remotes import TurnRemotes
from chemclaw.agent.verifier import require_verifier_capability
from chemclaw.api.auth import refuse_unusable_entra_ca_bundle
from chemclaw.api.budget import BudgetTracker, drain_pending
from chemclaw.api.deps import CurrentUser
from chemclaw.api.detach import RunningTurns
from chemclaw.api.events import event_schemas
from chemclaw.api.middleware import (
    _add_body_size_limit,
    _add_cors,
    _add_request_observability,
    _add_security_headers,
    _database_unavailable,
    _refuse_unauthenticated_exposure,
    _subsystem_unavailable,
)
from chemclaw.api.routes import (
    calc_artifacts,
    exhibits,
    jobs,
    members,
    notes,
    ops,
    org_skills,
    pending,
    plan,
    proposals,
    protocols,
    results,
    sessions,
    skills,
    streams,
    turns,
    workflows,
)
from chemclaw.api.schemas import _TRANSCRIPT_ARG_CHARS, _transcript
from chemclaw.api.state import (
    LiveSession,
    QueueSignal,
    SessionOwners,
    SessionTurns,
    _default_owner_store,
    _default_turn_claims,
    _default_turn_queue,
    _LiveSessions,
)
from chemclaw.api.tool_results import fetchable_refs, load_tool_result
from chemclaw.api.turn_relay import TurnRelay
from chemclaw.connectors.health import check_connectors_at_startup, probe_connectors
from chemclaw.connectors.registry import skills_dirs as connector_skills_dirs
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.executor import front_door_reserved, install_default_executor
from chemclaw.core.llm_gateway import refuse_unconfigured_llm_gateway
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.metrics import METRICS
from chemclaw.durable.job_record import search_job_records

# The module's surface and its test seam: tests patch route collaborators on this module by name,
# and route modules read them back through it at call time (`from chemclaw.api import app as
# front_door`), so a patch lands wherever the route lives.
__all__ = [
    "create_app",
    # Types and pure helpers the suite imports from here.
    "LiveSession",
    "_LiveSessions",
    "_TRANSCRIPT_ARG_CHARS",
    "_transcript",
    # Collaborators the suite patches on this module; routes read them through it at call time.
    "cancel_job",
    "expand_note",
    "fetchable_refs",
    # Patched to observe the width the lifespan sizes this process's shared `to_thread` pool to —
    # the argument, not a re-derivation of it, is what a test of that arithmetic has to read.
    "install_default_executor",
    "job_status",
    "load_tool_result",
    "probe_connectors",
    "search_job_records",
    "stream_new_events",
]

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def startup_inventory() -> list[str]:
    """What this process is configured to hold, one `subsystem=state` term per subsystem.

    Tells an operator at boot what is unconfigured (log-only audit, memory session store, no skills,
    sources or sinks), since each turn silently degrades around it. Configuration only, no counts:
    this runs before the pool opens and must not query. Connectors are omitted because
    `check_connectors_at_startup` logs them with their reachability.
    """
    skills = sum(
        1
        for directory in [*settings.skills_dirs, *connector_skills_dirs()]
        for _ in Path(directory).glob("*/SKILL.md")
    )
    notes = sum(1 for _ in settings.knowledge_path.rglob("*.md"))
    return [
        f"audit-trail={type(default_audit_sink()).__name__}",
        f"sessions={settings.session_store}",
        f"skills={skills}",
        f"knowledge-notes={notes} in {settings.knowledge_path}",
        f"data-sources={','.join(settings.data_source_list) or 'none'}",
        f"result-sinks={','.join(settings.result_sink_list) or 'none (publishing off)'}",
        f"vector-store={settings.vector_store_provider}",
    ]


def _report_inventory() -> None:
    """Log the inventory, and warn separately where no durable trail is written.

    `default_audit_sink()` is `NullAuditSink` whenever `session_store != "postgres"`, even when a
    database is configured and migrated, so every audit row is discarded. The DSN is not part of the
    condition because it always has a default. Front door only: in `Settings` it would fire on every
    CLI invocation and test collection.
    """
    logger.info("inventory: %s", " ".join(startup_inventory()))
    if isinstance(default_audit_sink(), NullAuditSink):
        logger.warning(
            "no durable audit trail: default_audit_sink() resolved to NullAuditSink because "
            "CHEMCLAW_SESSION_STORE=%s, so no audit_events row and no session_messages row is "
            "written for any turn this pod serves — `python -m chemclaw.cli.explain <session>` "
            "will find nothing, whatever the session did. The tool-call log lines are the whole "
            "record, and they are kept by the log stack rather than by this system. Set "
            "CHEMCLAW_SESSION_STORE=postgres (Helm: values.yaml already does) to write the trail. "
            "The agent is told which of the two it has, so it will not describe a trail this "
            "deployment is not keeping.",
            settings.session_store,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open this process's Postgres pool, probe the connectors, and drain turns before closing.

    The pool belongs to one process and loop; everything under `chemclaw.core.db.connection`
    inherits
    it, so callers do not pay a connect per call — under load a non-fatal guard that cannot get a
    connection disarms silently.

    The connector probe only informs readiness and a gauge, unless `connectors_required`, which
    fails
    startup: refusing to start is the only way to keep a degraded pod out of a rollout.

    On shutdown, running turns are drained first (they outlive their requests on pump tasks nobody
    else
    tracks), bounded by `service_turn_timeout_seconds`, which the chart's
    `terminationGracePeriodSeconds` is derived from. Then pending budget bookings, then the pools;
    `close_checkpointer` owns the order of closing the memory store and checkpointer pool.
    """
    # First, so everything below honours `CHEMCLAW_LOG_LEVEL`/`LOG_FORMAT` and OTel; here rather
    # than in
    # `create_app` because this is the "about to serve" moment, as in each worker's `main()`.
    configure_logging()
    configure_telemetry()
    # Register file-authored profiles before any agent is built. A malformed profile is a deployment
    # error and fails startup.
    load_profiles()
    # A misspelled `task` roster entry fails startup: at run time `_subagents` only skips it with a
    # warning, and the helper would be silently missing.
    refuse_an_unknown_roster(registered_profile_names(), lambda name: get_profile(name).description)
    # Likewise the peer roster: one typo can make the mesh indistinguishable from the feature being
    # off.
    refuse_an_unknown_peer_roster(registered_profile_names())
    # After `configure_logging()` so the line is formatted the way the operator asked, and after
    # the profiles load so a malformed one fails before anything claims the deployment is sound.
    _report_inventory()
    # Before anything offloads. Every `asyncio.to_thread` here (token validation, retrieval,
    # embeddings,
    # parses) shares one pool, and the stock default is small enough for admitted turns to starve
    # authentication. `front_door_reserved()` sizes it for every admitted turn's parallel tool
    # calls;
    # see `core/executor.py`.
    install_default_executor(component="front-door", reserved=front_door_reserved())

    # Before serving: a judge endpoint that cannot enforce structured output would silently degrade
    # every verified answer. A no-op unless `verifier_enabled`.
    await require_verifier_capability()
    async with db.pooling():
        app.state.connector_health = await check_connectors_at_startup()
        app.state.connector_health_at = time.monotonic()
        relay: TurnRelay | None = app.state.turn_relay
        serving = asyncio.create_task(relay.run(), name="turn-relay") if relay else None
        try:
            yield
        finally:
            # Inside `db.pooling()` and before either close, because a draining turn still writes
            # its checkpoint, its transcript and its cost row through all three.
            running_turns: RunningTurns = app.state.running_turns
            await running_turns.drain(settings.service_turn_timeout_seconds)
            # After the drain, so a Stop sent from another replica still reaches a draining turn.
            if serving is not None:
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving
            # After the turns (which book spend as they finish) and before the pool closes (the
            # booking needs
            # it); otherwise a rollout drops each in-flight principal's last booking.
            await drain_pending()
            # One call, not two. `close_checkpointer` drops the memory store itself, in the order
            # the store's dependency on its pool requires — see the paragraph above.
            await close_checkpointer()


def create_app(
    owner_store: SessionOwners | None = None,
    connector_factory: Callable[[str | None], list[Any]] = connector_specs,
    turn_claims: SessionTurns | None = None,
    graph_factory: Callable[..., Any] = build_langgraph_agent,
) -> FastAPI:
    """Build the front-door FastAPI app.

    Every argument is a test seam, not a deployment knob: production calls `create_app()` bare as
    the
    uvicorn factory, so a branch reached only by a non-default argument is reached only from tests.

    Args:
        owner_store: The durable session-ownership registry used to reattach a client after a pod
            restart. Defaults to the config-gated store (present only under
            `session_store="postgres"`).
        connector_factory: Builds this turn's connector specs for one profile name. Called per turn,
            because a connector session belongs to one turn and the profile narrows it.
        turn_claims: The durable "one turn at a time per session" claim that holds across processes.
            Defaults to the config-gated store (present only under `session_store="postgres"`).
        graph_factory: Builds this turn's compiled graph from the profile, identity and open
            connectors. Per turn, because a graph binds its tools at construction. The seam a test
            uses to run a turn without a model credential.

    Returns:
        A configured `FastAPI` application.
    """
    _refuse_unauthenticated_exposure()
    refuse_unconfigured_llm_gateway()
    refuse_unusable_entra_ca_bundle()
    # `openapi_url=None`: FastAPI would serve the schema on a plain `Route` outside
    # `require_principal`.
    # The document is served from a gated `APIRoute` below instead. `docs_url`/`redoc_url` stay off.
    app = FastAPI(
        title="Chemclaw", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan
    )
    # First, which makes it innermost (`add_middleware` prepends): inside `_SecurityHeaders`, so its
    # 500
    # carries them, and outside FastAPI's `ExceptionMiddleware`, so handler 4xx responses are
    # recorded.
    _add_request_observability(app)
    # Installed before the header stamper so it sits inside it and its 413 carries the security
    # headers.
    # It still sits above the observability layer, so the 413 has no correlation id and no
    # access-log
    # line (it is counted by `chemclaw_requests_too_large_total` and logged where refused); tests
    # assert
    # both. A CORS preflight is answered by the outermost `CORSMiddleware` before any of these run.
    _add_body_size_limit(app)
    _add_security_headers(app)
    # Outermost, and correctly so: a 500 raised anywhere below has to come back out through CORS
    # or a browser cannot read it. A preflight is therefore answered here, above everything.
    _add_cors(app)
    # One handler instead of per-route try/except: `chemclaw.db` maps both "no database" and "pool
    # timeout" to `ConnectionError`. See `_database_unavailable`.
    app.add_exception_handler(ConnectionError, _database_unavailable)
    app.add_exception_handler(SubsystemUnavailableError, _subsystem_unavailable)
    # Both are called per turn: a connector session belongs to one turn and a graph binds its tools
    # at
    # construction. Nothing outlives a turn.
    app.state.connector_factory = connector_factory
    app.state.graph_factory = graph_factory

    def _turn_in_flight(session_id: str) -> bool:
        """Whether `session_id` holds an unexpired in-process turn lease — the eviction pin.

        Reads `app.state.active_turns` at call time, so a leaked lease delays eviction by at most
        one
        lease period.
        """
        lease = app.state.active_turns.get(session_id)
        return lease is not None and lease.deadline > time.monotonic()

    # Bounded LRU of live sessions, each carrying its owner's oid (membership is read per request,
    # never
    # cached here). Sessions with a turn in flight are pinned: evicting one mid-turn would let the
    # next
    # request rehydrate a second handle over the same history.
    app.state.live_sessions = _LiveSessions(
        settings.service_max_live_sessions, pinned=_turn_in_flight
    )
    # Durable session-ownership registry, which a restarted front door rehydrates from. `None` with
    # the
    # in-memory store, where a cache miss stays a 404.
    app.state.session_owners = owner_store if owner_store is not None else _default_owner_store()
    # Through the factory so the plan routes and `chemclaw.agent.plan_gate` share one store.
    app.state.plan_approvals = plan_approval_store()
    # The history provider the agent writes through, used read-only to serve transcripts. Stateless
    # per
    # session in both backends, so one instance serves all.
    app.state.history = history_provider()
    # Admission control on concurrent turns: a permit is held for a turn's whole run, and a turn
    # that
    # cannot get one within the admission timeout is shed with 503. Built here to bind to the app's
    # loop.
    app.state.turn_semaphore = asyncio.Semaphore(settings.service_max_concurrent_turns)
    # Per-session turn serialization: session id → the lease of the turn in flight
    # (`chemclaw.api.state.TurnLease`). Concurrent turns on one session would interleave messages in
    # one
    # thread, so a second turn gets 409. A lease rather than a set because an entry can leak
    # (`_claim_turn_slot` owns atomicity and expiry); also the eviction pin `_turn_in_flight` reads.
    app.state.active_turns = {}
    # The live turns, so the stop route can get a handle: a disconnect only detaches
    # (`chemclaw.api.detach`).
    app.state.running_turns = RunningTurns()
    # The same gate across replicas: a leased row in `session_turns` every process can see. `None`
    # under the in-memory store.
    app.state.turn_claims = turn_claims if turn_claims is not None else _default_turn_claims()
    # Lets a turn held here be followed and stopped from another replica, and vice versa. Only where
    # the
    # claim above is durable.
    app.state.turn_relay = (
        TurnRelay(TurnRemotes(), app.state.running_turns, app.state.active_turns)
        if app.state.turn_claims is not None and settings.session_store == "postgres"
        else None
    )
    # Each session's queue of messages waiting for its running turn, and the wake-up for local
    # waiters.
    # Durable exactly where the claim above is.
    app.state.turn_queue = _default_turn_queue()
    app.state.queue_signal = QueueSignal()
    # This process's waiting messages per `(sender, session)`: bounds the sockets they hold and
    # feeds the
    # per-actor cap. The queue above is the order; this is the load.
    app.state.queue_waiters = {}
    # Per-user count of open push-back event streams. Each polls the database for its lifetime, so
    # it is
    # capped per user; an entry is removed when a user's last stream closes.
    app.state.event_streams = {}
    # Runaway-cost guard: meters tokens and counts turns per session and user, refusing (429) a turn
    # over a configured cap. Off unless `budget_enabled`.
    app.state.budget = BudgetTracker()
    # Gauges read the live structures, so nothing has to be kept in sync. Turns in flight against
    # the
    # cap is the saturation signal to scale on. Counts unexpired leases only, since the sweep runs
    # only
    # when a POST arrives.
    METRICS.bind_gauge(
        "chemclaw_turns_in_flight",
        lambda: float(
            sum(1 for lease in app.state.active_turns.values() if lease.deadline > time.monotonic())
        ),
    )
    METRICS.bind_gauge(
        "chemclaw_turn_capacity", lambda: float(settings.service_max_concurrent_turns)
    )
    METRICS.bind_gauge(
        "chemclaw_turn_actor_capacity",
        lambda: float(settings.service_max_concurrent_turns_per_actor),
    )
    # Declared fleet capacity. Config validation checks the chart's product at startup, but a
    # hand-scaled
    # Deployment never re-reads it; only this pair can show that.
    METRICS.bind_gauge(
        "chemclaw_fleet_turn_ceiling",
        lambda: float(settings.service_fleet_max_concurrent_turns),
    )
    METRICS.bind_gauge("chemclaw_live_sessions", lambda: float(len(app.state.live_sessions)))
    # The push-back streams' saturation pair, like the turn gauges, so nearing the cap is visible
    # before
    # rejections start. Summed over the per-user ledger.
    METRICS.bind_gauge(
        "chemclaw_event_streams_open", lambda: float(sum(app.state.event_streams.values()))
    )
    METRICS.bind_gauge(
        "chemclaw_event_stream_capacity",
        lambda: float(settings.service_max_event_streams_total),
    )
    # Connector health snapshot, refreshed by readiness and at startup; the gauge reads it, since a
    # scrape must not do network I/O.
    app.state.connector_health = []
    # When that snapshot was taken (`time.monotonic`), so readiness can reuse it. Negative infinity
    # so
    # an empty snapshot is always stale.
    app.state.connector_health_at = float("-inf")
    # The database probe's cached verdict and when it was taken. `True` before any probe, so a pod
    # is
    # not refused traffic for never having asked.
    app.state.database_reachable = True
    # Whether the schema carries the newest migration this image ships. `True` before any probe and
    # whenever it cannot be answered: it gates only on positive evidence of a mismatch.
    app.state.schema_current = True
    app.state.database_probed_at = float("-inf")
    # Readiness probe tasks in flight, by name. Probes are single-flight
    # (`chemclaw.api.routes.ops._shared_probe`), so a burst of probes costs one fan-out.
    app.state.readiness_probes = {}
    # Pool gauges are bound by `chemclaw.core.db.pooling`, so every process with a pool reports.
    # `unhealthy` includes `unpolled` (a jobs-only bundle with no poller); the predicate lives on
    # the
    # model so this gauge and the `connectors_required` gate share one definition. `unknown` is
    # neither.
    METRICS.bind_gauge(
        "chemclaw_connectors_unhealthy",
        lambda: float(sum(1 for item in app.state.connector_health if item.unhealthy)),
    )
    # The same probe result, by connector: the count says how many are down, this says which.
    # `unprobed`
    # reads 0 rather than being omitted, so "no series" never means both "reachable" and "never
    # asked".
    METRICS.bind_gauge_family(
        "chemclaw_connector_unhealthy",
        lambda: {
            item.name: 1.0 if item.state == "unreachable" else 0.0
            for item in app.state.connector_health
        },
    )

    # The routes, one module per resource. Order is by audience (probes, chemist, operator) and only
    # affects the OpenAPI listing. Each module registers on the app directly; see any `register`
    # docstring for why `include_router` is not used.
    for module in (
        ops,
        sessions,
        turns,
        streams,
        results,
        plan,
        pending,
        notes,
        skills,
        org_skills,
        proposals,
        jobs,
        protocols,
        workflows,
        members,
        exhibits,
        calc_artifacts,
    ):
        module.register(app)

    # Merge the turn-event union into the published document: the SSE body is `text/event-stream`,
    # which
    # FastAPI cannot infer, and `Chemclaw3_ui` mirrors these types. The streaming routes reference
    # `TURN_EVENT_REF`; this makes it resolve. Wrapped around `app.openapi` because FastAPI caches
    # the
    # generated document.
    _generate = app.openapi

    def _openapi_with_events() -> dict[str, Any]:
        """The generated document with the turn-event components merged into it, generated once."""
        document = _generate()
        components = document.setdefault("components", {}).setdefault("schemas", {})
        for name, schema in event_schemas().items():
            components.setdefault(name, schema)
        return document

    app.openapi = _openapi_with_events  # type: ignore[method-assign]

    # The schema, gated like everything else, registered after the route loop so it lists last.
    # Served
    # because `Chemclaw3_ui/scripts/check-openapi.mjs` diffs its BFF whitelist against it. A handler
    # taking `CurrentUser` puts it under `require_principal` and in
    # `tests/test_route_auth_coverage.py`'s
    # view; `principal` is unused because the document is the same for every caller.
    @app.get("/openapi.json")
    async def openapi_schema(principal: CurrentUser) -> dict[str, Any]:
        """The OpenAPI document, for an authenticated caller only.

        `app.openapi()` caches on first call, so this generates once per process.
        """
        return app.openapi()

    # Only when identity is not enforced: `api/static/app.js` sends no `Authorization` header and
    # uses a
    # native `EventSource`, so under `entra_required` it could not work. `Chemclaw3_ui` is the
    # authenticated front end; `tests/test_route_auth_coverage.py` asserts an enforced app has no
    # ungated surface.
    if _STATIC_DIR.is_dir() and not settings.entra_required:
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app
