"""The ASGI front door (plan step F2-T1/F2-T2): a browser chat surface over the Chemclaw agent.

`create_app` builds a FastAPI app that lets a non-developer chemist open a page, start a
session, and converse with the agent — watching its plan, tool calls, and cited answer stream
in. It owns one agent instance per profile for the process and a per-session `AgentSession`
(durable under `session_store="postgres"`, with job→session push-back). The agent factory is
injectable so tests drive the whole app with a fake streaming agent and no live model or
credentials.

**This module is the composition root and nothing else** (R3.2): it seeds `app.state`, installs
the middleware, binds the gauges, and includes the routers. The routes live in
`chemclaw/api/routes/` (one module per resource), the request/response shapes in
`api/schemas.py`, the state types and turn-lease bookkeeping in `api/state.py`, the
authorization dependencies in `api/deps.py`, and the cross-cutting HTTP armor in
`api/middleware.py`. The seam that makes the split behavior-preserving is `app.state`: every
route reads the process's live structures through `request.app.state`, never through lexical
capture, so `create_app` keeps its signature and its injection points while holding no route
code.

Routes: `GET /healthz` (liveness), `GET /readyz` (readiness), `POST /sessions` (start a session),
`GET /sessions` (the caller's conversation list), `POST /sessions/{id}/messages` (send a turn,
Server-Sent-Events stream of `chemclaw.api.events`), `GET /sessions/{id}/messages` (read the
transcript
back), and the static chat UI at `/`. Identity (Entra OIDC on every non-health route) is layered
on in F4.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from chemclaw.agent.agent_pool import AgentPool
from chemclaw.agent.chemclaw_agent import build_agent, connector_tools, history_provider
from chemclaw.agent.durable_tools import cancel_job, job_status, request_note_reindex
from chemclaw.agent.interaction_tools import (
    approval_owner,
    approval_status,
    decide_approval,
    list_pending_approvals,
)
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.session_events import stream_new_events
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.middleware import (
    _add_body_size_limit,
    _add_cors,
    _add_security_headers,
    _database_unavailable,
    _refuse_unauthenticated_exposure,
)
from chemclaw.api.routes import (
    approvals,
    jobs,
    ops,
    plan,
    proposals,
    sessions,
    streams,
    turns,
)
from chemclaw.api.schemas import _TRANSCRIPT_ARG_CHARS, _transcript
from chemclaw.api.state import (
    LiveSession,
    SessionOwners,
    SessionTurns,
    _default_owner_store,
    _default_turn_claims,
    _LiveSessions,
)
from chemclaw.connectors.health import check_connectors_at_startup, probe_connectors
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.metrics import METRICS
from chemclaw.durable.job_record import search_job_records

# The module's surface, and — for everything besides `create_app` — its *test seam*. The suite
# patches the routes' external collaborators on this module by name
# (`monkeypatch.setattr("chemclaw.api.app.job_status", …)`), and the route modules read them back
# through this module at call time (`from chemclaw.api import app as front_door`), so a patch
# lands wherever the route lives. The re-exported types and transcript helpers are here for the
# same reason: `tests/test_service.py` and `tests/test_jobs_api.py` import them from this module,
# which remains the package's front page even though the definitions moved (R3.2).
__all__ = [
    "create_app",
    # Types and pure helpers the suite imports from here.
    "LiveSession",
    "_LiveSessions",
    "_TRANSCRIPT_ARG_CHARS",
    "_transcript",
    # Collaborators the suite patches on this module; routes read them through it at call time.
    "approval_owner",
    "approval_status",
    "cancel_job",
    "decide_approval",
    "job_status",
    "list_pending_approvals",
    "probe_connectors",
    "request_note_reindex",
    "search_job_records",
    "stream_new_events",
]

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open this process's Postgres pool, then probe the connectors once before serving.

    The pool belongs here because it belongs to one process and one event loop, and because
    everything below `chemclaw.core.db.connection` inherits it with no plumbing: the session store,
    the ownership registry, the push-back tailer and the rollback watermark all stop paying a
    TCP+auth handshake per call. That churn — measured at ~2.7 connects per turn — was what made
    a connect fail to be scheduled inside its timeout under load, which silently disarmed the
    non-fatal rollback-watermark guard (D-107).

    The connector probe's *result* only informs (readiness reports it, a gauge counts it) — a
    missing connector costs capability, not correctness, so the default is to serve anyway.
    `connectors_required` is the opt-in inversion: it raises here, which fails startup, for a
    deployment where answering with a silently reduced tool surface is worse than not answering.
    That check belongs at startup rather than in the readiness route because refusing to *start*
    is the only way to keep a pod with degraded capability out of a rollout.
    """
    # First, so everything below is logged the way the operator asked. The front door never
    # configured either of these, so it ran on Python's default root logger (WARNING, no format)
    # while every worker honoured `CHEMCLAW_LOG_LEVEL`/`LOG_FORMAT`, and `CHEMCLAW_OTEL_ENABLED`
    # was simply inert here — the one process a chemist actually talks to was the one with no
    # observability wiring. Here rather than in `create_app` because this is the "about to serve"
    # moment, matching each worker's `main()`.
    configure_logging()
    configure_telemetry()
    # Register the file-authored profiles before any agent is built, so a session can name one
    # on its first request. Failing here is the right outcome for a malformed profile: it is a
    # deployment configuration error, and a front door that started anyway would 400 every
    # request naming that profile with no hint as to why.
    load_profiles()
    async with db.pooling():
        app.state.connector_health = await check_connectors_at_startup()
        app.state.connector_health_at = time.monotonic()
        yield


def _default_agent_factory(profile: str | None) -> Any:
    """Build the agent for one profile — `build_agent` with the profile passed by keyword.

    A named adapter rather than a lambda so the app's default factory has the same one-argument
    shape a test's fake does, and so the signature is somewhere a reader can find it.

    No `audit_sink` argument, deliberately: `chemclaw.agent.audit.default_audit_sink` supplies the
    durable
    GxP trail wherever a database is configured. This line used to be the whole of the finding —
    it passed no sink, so the compliance record was log-only in the one process chemists use.
    Fixing it *here* would have left the same trap set for the Temporal template activities and
    every future entry point, so the default moved to the one place that decides.
    """
    return build_agent(profile=profile)


def create_app(
    agent_factory: Callable[[str | None], Any] = _default_agent_factory,
    owner_store: SessionOwners | None = None,
    connector_factory: Callable[[str | None], list[Any]] = connector_tools,
    turn_claims: SessionTurns | None = None,
) -> FastAPI:
    """Build the front-door FastAPI app.

    Args:
        agent_factory: Builds the agent for one profile name (`None` = the default profile). Called
            once per distinct profile and cached, since an agent is configuration rather than
            per-conversation state. Tests pass a factory returning a fake streaming agent so the
            whole HTTP surface is exercised without a live model.
        owner_store: The durable session-ownership registry used to reattach a client to its session
            after a pod restart. Defaults to the config-gated store (present only under
            `session_store="postgres"`); tests inject an in-memory fake to exercise rehydration
            without a database.
        connector_factory: Builds *this turn's* connector tools for one profile name. A factory
            rather than a list because a connector's connection must belong to a single turn
            (see `chemclaw.agent.chemclaw_agent.connector_tools`), so the app calls it per turn; and
            per-profile because the profile narrows the connector surface as well as the
            in-process one. Injectable for the same reason `agent_factory` is: a test drives the
            whole HTTP surface without a connector server running.
        turn_claims: The durable "one turn at a time per session" claim, which is what makes that
            guard hold across processes rather than only within one (D-121). Defaults to the
            config-gated store (present only under `session_store="postgres"`); tests inject an
            in-memory fake to exercise the cross-process conflict without a database.

    Returns:
        A configured `FastAPI` application.
    """
    _refuse_unauthenticated_exposure()
    # `openapi_url=None` for the same reason as `docs_url`/`redoc_url`: FastAPI serves the schema
    # from a plain `Route`, not an `APIRoute`, so `require_principal` never applied to it and
    # `tests/test_route_auth_coverage.py` could not see it — the full route/parameter/model surface
    # was readable by anyone who could reach the pod. Nothing consumes it (the UI is static and the
    # docs pages are already off), so it is closed rather than gated.
    app = FastAPI(
        title="Chemclaw", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan
    )
    _add_security_headers(app)
    _add_body_size_limit(app)
    _add_cors(app)
    # One handler rather than a try/except per route: every route that touches durable session
    # state can hit the pool, and `chemclaw.db` already funnels both "no database" and "no free
    # connection in time" into `ConnectionError` precisely because a caller cannot act on the
    # difference. See `_database_unavailable`.
    app.add_exception_handler(ConnectionError, _database_unavailable)
    # One agent per process, built lazily on first use so importing the app needs no
    # credentials; per-session threads keep conversations apart. F3 replaces the in-memory
    # session map with a durable store and wires job→session push-back. One agent per profile
    # name, built lazily on first use so importing the app needs no credentials. `None` is the
    # default profile — the key a session gets when it names none.
    app.state.agents = {}
    app.state.agent_factory = agent_factory
    # Called once per turn, not once per process — a connector connection belongs to a single turn.
    app.state.connector_factory = connector_factory

    def _turn_in_flight(session_id: str) -> bool:
        """Whether `session_id` holds an unexpired in-process turn lease — the eviction pin.

        Reads `app.state.active_turns` (seeded below) at call time, so the pin appears when a
        turn claims the slot and vanishes when the turn releases it *or* when the lease expires —
        a leaked entry (see `chemclaw.api.state._claim_turn_slot`) can therefore delay an
        eviction by at most one lease, never wedge it.
        """
        deadline = app.state.active_turns.get(session_id)
        return deadline is not None and deadline > time.monotonic()

    # Bounded LRU of live sessions, each carrying its owner Entra oid so a session can only be
    # posted to / streamed by its creator (defense-in-depth beyond the unguessable uuid4 id).
    # The bound keeps the map from growing for the pod's lifetime (COR-3). Sessions with a turn
    # in flight are pinned against eviction: dropping the handle mid-turn lets the next request
    # rehydrate a second one over the same durable history, and the two diverge (A5).
    app.state.live_sessions = _LiveSessions(
        settings.service_max_live_sessions, pinned=_turn_in_flight
    )
    # Durable session-ownership registry (F3): the record a restarted front door rehydrates from
    # so a returning client reattaches to its session instead of being forced onto a new one.
    # None with the in-memory session store (nothing durable to reattach to — a cache miss stays
    # a 404).
    app.state.session_owners = owner_store if owner_store is not None else _default_owner_store()
    # Through the factory, not by construction, so the plan routes and the enforcement gate
    # (`chemclaw.agent.plan_gate`) hold the *same* store: a decision recorded here has to be a
    # decision the gate can see, which under the in-memory backend means the same object (D-167).
    app.state.plan_approvals = plan_approval_store()
    # The same history provider the agent writes turns through, used read-only to serve a
    # transcript back. Shared rather than re-derived per request: the Postgres provider holds
    # only a DSN and the in-memory one holds nothing at all (its messages live in
    # `session.state`), so one instance is correct for both and neither carries per-session
    # state.
    app.state.history = history_provider()
    # One agent — and therefore one chat client — per concurrent turn (D-123). The cached
    # per-profile agent above still serves everything that does not stream; only a streaming turn
    # needs exclusivity, because that is where the Anthropic client keeps tool-call identity on
    # itself.
    app.state.agent_pool = AgentPool(app.state.agent_factory, settings.service_max_concurrent_turns)
    # Admission control on concurrent turns (AG-15): a bounded permit set caps how many turns
    # hit the shared LLM endpoint at once. A permit is held for a turn's whole streamed run; a
    # turn that cannot get one within the admission timeout is shed with 503. Built here so it
    # binds to the app's event loop on first await.
    app.state.turn_semaphore = asyncio.Semaphore(settings.service_max_concurrent_turns)
    # Per-session turn serialization: session id → monotonic lease deadline for the turn in
    # flight. Two concurrent turns on one session would drive `agent.run` against the same
    # AgentSession state at once, interleaving two turns' messages in one thread — so a second
    # turn is rejected with 409 while one runs, matching the admission semaphore's
    # shed-don't-queue semantics (a queued turn would silently pin a second permit and still
    # interleave from the user's point of view; a 409 tells the client — a double-submit or a
    # second tab — to wait for the running turn). A deadline map rather than a bare set because
    # an entry can leak (see `chemclaw.api.state._claim_turn_slot`, which owns the claim's
    # atomicity and expiry); this is also the pin set `_turn_in_flight` above reads for the live
    # cache's eviction.
    app.state.active_turns = {}
    # The same gate at the width the deployment actually has (D-121). The map above is one
    # process's view, and the chart runs the front door at two replicas, so the second POST can
    # land on a process that has never heard of the first. A leased row in `session_turns` is what
    # both processes can see; None under the in-memory session store, where two processes share no
    # history to corrupt.
    app.state.turn_claims = turn_claims if turn_claims is not None else _default_turn_claims()
    # Per-user count of open push-back event streams. The turn semaphore only guards POSTed
    # turns; each event stream polls the database for its whole lifetime, so without a cap one
    # user's scripted (or abandoned-tab) streams could pile up unbounded DB load. Entries are
    # removed when a user's last stream closes, so the map stays small.
    app.state.event_streams = {}
    # Runaway-cost guard (service.budget): meters each turn's token usage and counts turns per
    # session and per user, refusing a turn (429) that would exceed a configured cap. In-process
    # and off unless `budget_enabled`; the missing ceiling above the per-turn loop cap.
    app.state.budget = BudgetTracker()
    # Gauges read the live structures rather than a mirrored counter, so there is nothing to
    # keep in sync (gap DEP-4). In-flight turns against the cap is the saturation signal the HPA
    # should scale on — CPU is close to noise for a stream-bound, model-latency-dominated
    # service.
    # Counts unexpired leases only: a leaked entry waiting out its deadline is not a turn in
    # flight, and the sweep in `chemclaw.api.state._claim_turn_slot` only runs when a POST
    # arrives, so `len` alone would report a phantom turn until then.
    METRICS.bind_gauge(
        "chemclaw_turns_in_flight",
        lambda: float(
            sum(1 for deadline in app.state.active_turns.values() if deadline > time.monotonic())
        ),
    )
    METRICS.bind_gauge(
        "chemclaw_turn_capacity", lambda: float(settings.service_max_concurrent_turns)
    )
    # Per-pod capacity summed across pods is what the fleet admits; this is what it was declared
    # allowed to admit. Config validation refuses the product at startup, but only for the shape the
    # chart rendered — a hand-scaled Deployment or an in-cluster HPA edit never re-reads it, and
    # only this pair can see that.
    METRICS.bind_gauge(
        "chemclaw_fleet_turn_ceiling",
        lambda: float(settings.service_fleet_max_concurrent_turns),
    )
    METRICS.bind_gauge("chemclaw_live_sessions", lambda: float(len(app.state.live_sessions)))
    # Out-of-process capability is a new failure mode, so it gets a signal an operator can alert
    # on. Refreshed by the readiness probe (and at startup), read from the snapshot here — a
    # gauge must not perform network I/O when Prometheus scrapes it.
    app.state.connector_health = []
    # When that snapshot was taken (`time.monotonic`), so the readiness route can reuse it instead
    # of fanning out to the whole connector fleet on every unauthenticated probe. Negative
    # infinity, not 0: an empty snapshot must always be treated as stale, and 0 would be "fresh"
    # for the first `service_readiness_cache_seconds` of process uptime.
    app.state.connector_health_at = float("-inf")
    # The database probe's cached verdict and when it was taken. `True` before any probe has run,
    # because readiness must not report a store unreachable on the strength of never having asked —
    # the kubelet's first probe answers within one interval, and refusing traffic until then would
    # turn every rollout into a needless gap.
    app.state.database_reachable = True
    app.state.database_probed_at = float("-inf")
    # The pool gauges are deliberately *not* bound here any more (D-119's saturation signal, plus
    # the connection-budget pair). `chemclaw.core.db.pooling` binds them, so every process that
    # opens a pool reports on it rather than only the one process that happened to have the
    # binding — the workers and connector servers pool too and were reporting nothing
    # (D-2026-08-05-the-connection-budget-is-a-fleet-number).
    METRICS.bind_gauge(
        "chemclaw_connectors_unhealthy",
        lambda: float(sum(1 for item in app.state.connector_health if item.state == "unreachable")),
    )

    # The routes, one module per resource (`chemclaw/api/routes/`). Order mirrors the audience:
    # probes first, then the chemist surfaces, then the review/operator surfaces — it changes
    # nothing about matching (every APIRoute path here is distinct) and keeps the OpenAPI listing
    # stable relative to the pre-split file. Each module registers its handlers on the app
    # directly rather than contributing an `APIRouter` — see any `register` docstring for why
    # `include_router` cannot be used here since FastAPI 0.139.
    for module in (ops, sessions, turns, streams, plan, approvals, proposals, jobs):
        module.register(app)

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app
