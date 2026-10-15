"""The operator surfaces: liveness, readiness, the Prometheus exposition, and schedule health.

`/healthz`, `/readyz` and `/metrics` are the only unauthenticated routes (a kubelet and a scrape
have no token); `tests/test_route_auth_coverage.py` pins that allowlist. `/schedules` serves the
same operator audience.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from http import HTTPStatus
from typing import Any, TypeVar

import psycopg
from fastapi import FastAPI, Request
from starlette.responses import Response

from chemclaw.agent import text_overlay
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser
from chemclaw.api.state import FrontDoorState, state
from chemclaw.connectors.health import ConnectorHealth
from chemclaw.core import db
from chemclaw.core.config import MAX_VECTOR_DIM, settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.migrate import newest_shipped_migration
from chemclaw.core.tenant_registry import TenantSweepError
from chemclaw.core.vector_schema import (
    TableVectorState,
    inspect_every_tenant,
    live_embedding_keys,
)
from chemclaw.durable.document_sync import share_sources
from chemclaw.durable.schedules import ScheduleHealth, describe_schedules

log = logging.getLogger(__name__)

# What one readiness probe answers with, so `_shared_probe` serves both the connector sweep and the
# database verdict.
_Probed = TypeVar("_Probed")


async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


async def _connector_health(request: Request) -> list[ConnectorHealth]:
    """The connector sweep: at most once per `service_readiness_cache_seconds`, and once at a time.

    Monotonic time, so a clock step cannot make a sweep look fresh. The window alone would not stop
    concurrent misses: this route is unauthenticated and outside the rate budget, so every request
    in flight during a sweep would start its own fan-out. Single-flight rather than a lock, so all
    waiters share one result.
    """
    front = state(request)
    window = settings.service_readiness_cache_seconds
    if window and time.monotonic() - front.connector_health_at < window:
        return front.connector_health
    return await _shared_probe(front, "connectors", lambda: _sweep_connectors(front))


async def _sweep_connectors(front: FrontDoorState) -> list[ConnectorHealth]:
    """Probe every connector once and refresh the readiness snapshot with what came back.

    Never raises: connector health is reported, never gating, so a failure here must not 500 the
    readiness probe. On failure the last snapshot stands and its timestamp is not refreshed, so the
    next request retries.
    """
    try:
        # Through the front-door module so the suite's patch seam (`chemclaw.api.app.
        # probe_connectors`) keeps reaching the probe this route actually runs.
        health = await front_door.probe_connectors()
    except Exception:
        log.warning("readiness: the connector sweep failed; reporting the last snapshot")
        return front.connector_health
    front.connector_health = health
    front.connector_health_at = time.monotonic()
    return health


async def _shared_probe(
    front: FrontDoorState, name: str, probe: Callable[[], Coroutine[Any, Any, _Probed]]
) -> _Probed:
    """Run `probe` once for every caller that finds the cache stale at the same moment.

    The in-flight task lives on `app.state` under `name`, and check-and-start has no `await` between
    them, so it is atomic on the loop. `shield`, so a client hanging up cannot cancel the probe
    others await. The done callback retrieves a failed probe's exception; awaiting callers still see
    it.
    """
    inflight = front.readiness_probes.get(name)
    if inflight is None or inflight.done():
        inflight = asyncio.create_task(probe())
        inflight.add_done_callback(_drain)
        front.readiness_probes[name] = inflight
    probed: _Probed = await asyncio.shield(inflight)
    return probed


def _drain(task: "asyncio.Task[Any]") -> None:
    """Retrieve a finished probe's outcome, so a failed one nobody awaited is not a traceback."""
    if not task.cancelled():
        task.exception()


async def _database_ready(request: Request) -> bool:
    """Whether Postgres answers and its schema carries this image, re-probed once per window.

    Two verdicts from one round trip (`_probe_database`), cached together and reported separately,
    since "database down" and "pod ahead of schema" have different fixes. Cached per
    `service_readiness_cache_seconds`, since this unauthenticated route would otherwise be a
    database round trip on demand. Bounded by `service_readiness_db_timeout_seconds`, its own short
    budget: a readiness probe should say "not ready" quickly. Never raises; `False` is the answer.
    """
    front = state(request)
    window = settings.service_readiness_cache_seconds
    if window and time.monotonic() - front.database_probed_at < window:
        return front.database_reachable and front.schema_current and not front.vector_width_problem
    return await _shared_probe(front, "database", lambda: _probe_database(front))


async def _probe_database(front: FrontDoorState) -> bool:
    """Ask Postgres one bounded question and cache the answer.

    Single-flight because each miss would borrow from the shared pool.

    `asyncio.wait_for` bounds acquisition (the connect leg), which the connection's own timeouts do
    not cover. It does not reliably bound a query on an already-open connection to an unresponsive
    server: psycopg re-waits on the socket after cancelling. The kubelet's
    `readinessProbe.timeoutSeconds` (derived by the chart from this setting) catches that case; the
    residual is upstream's.

    The timeout kwarg becomes a server-side `statement_timeout`, bounding the query on a responsive
    server. Because `core/db` keys pools on the options string, it also gives this probe its own
    pool, so a busy store pool cannot make readiness report the database unreachable. That pool is
    one connection wide, matching the single-flight. Labelled, so its samples do not land in
    `operation="unspecified"`.
    """
    try:

        async def _ask() -> tuple[bool, list[TableVectorState] | None]:
            async with db.connection(
                settings.session_store_dsn or settings.postgres_dsn,
                statement_timeout_seconds=settings.service_readiness_db_timeout_seconds,
                operation="readyz_probe",
                pool_max_size=1,
            ) as conn:
                await conn.execute("SELECT 1")
                current = await _schema_carries_this_image(conn)
                # Only against a schema this image can read: an unmigrated one is already unready,
                # and its missing columns would be reported twice.
                return current, (await _vector_tables_if_readable(conn) if current else [])

        current, vector_tables = await asyncio.wait_for(
            _ask(), timeout=settings.service_readiness_db_timeout_seconds
        )
        if vector_tables is not None:
            _record_vector_verdict(front, vector_tables)
        reachable = True
    except (psycopg.Error, ConnectionError, TimeoutError):
        log.warning("readiness: Postgres did not answer", exc_info=True)
        reachable = False
        # A database that did not answer was not asked about its schema; False would misreport an
        # outage as a schema mismatch.
        current = True
    front.database_reachable = reachable
    front.schema_current = current
    front.database_probed_at = time.monotonic()
    return reachable and current and not front.vector_width_problem


async def _vector_tables(conn: psycopg.AsyncConnection[Any]) -> list[TableVectorState]:
    """The vector tables' state for the configured width; empty where pgvector holds no vectors.

    Empty too for a width pgvector cannot index: config refuses that where anything writes the note
    index, and where nothing does there is nothing to be wrong about. Scoped to the cuttings the
    enabled shares use, the set `make reembed` converges, so the two agree on what "mixed" means.
    """
    if settings.vector_store_provider != "pgvector" or settings.embedding_dim > MAX_VECTOR_DIM:
        return []
    chunkings = {share.share_binding().chunking_key for share in share_sources().values()}
    return await inspect_every_tenant(
        conn, settings.embedding_dim, live_embedding_keys(), chunkings
    )


async def _vector_tables_if_readable(
    conn: psycopg.AsyncConnection[Any],
) -> list[TableVectorState] | None:
    """`_vector_tables`, nothing for a role that may not read them, `None` when it cannot be asked.

    A split session store may use a role with no access to the vector tables, as it may have none to
    the ledger (`_schema_carries_this_image`); refusing would be an outage over another server's
    grants. The ledger read may have aborted the transaction, so it is cleared first.

    The question reads the tables, so a migration holding a lock on one (`ensure_embedding_width`
    on a large table) would otherwise stall it past the probe's budget and turn "a table is busy"
    into `database unreachable` for every new pod. It gets short local bounds of its own and, when
    they fire, no answer: the caller keeps the last verdict.
    """
    await conn.rollback()
    budget = settings.service_readiness_db_timeout_seconds
    try:
        await conn.execute(
            "SELECT set_config('lock_timeout', %s, true), "
            "set_config('statement_timeout', %s, true)",
            (f"{int(budget * 250)}ms", f"{int(budget * 500)}ms"),
        )
        return await _vector_tables(conn)
    except psycopg.errors.InsufficientPrivilege:
        log.warning(
            "readiness: the vector tables cannot be read by this role, so their width and the "
            "embedding configurations they hold are not being checked",
            exc_info=True,
        )
        return []
    except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled):
        log.warning(
            "readiness: the vector tables are locked or slow to read, so their width and the "
            "embedding configurations they hold keep their last verdict",
            exc_info=True,
        )
        return None
    except TenantSweepError:
        log.warning(
            "readiness: more tenants are registered than the embedding check may walk "
            "(CHEMCLAW_TENANCY_SWEEP_MAX_TENANTS), so the embedding configurations they hold keep "
            "their last verdict",
            exc_info=True,
        )
        return None
    finally:
        await conn.rollback()


def _record_vector_verdict(front: FrontDoorState, tables: list[TableVectorState]) -> None:
    """Turn the tables' state into the two readiness verdicts, saying so once when one changes.

    A column that is missing or the wrong width refuses readiness: nothing can read or write that
    table's vectors, which no retry fixes. Rows from another configuration only degrade it: they
    are never ranked (`embedding_key` is a read predicate) and the re-embed (`make reembed`) heals
    them. Logged on the transition, not on every probe.
    """
    wrong = [
        f"{one.table}.{one.column} is "
        f"{'missing' if one.column_width is None else f'vector({one.column_width})'}"
        for one in tables
        if not one.width_ok(settings.embedding_dim)
    ]
    mixed = [f"{one.table}: {one.describe_foreign()}" for one in tables if one.mixed]
    problem = "; ".join(wrong)
    if problem and problem != front.vector_width_problem:
        degraded(
            log,
            "embedding_width",
            "embedding_dim=%d but %s — run the migration with this embedding_dim "
            "(`make db-migrate`, or the chart's pre-upgrade hook)",
            settings.embedding_dim,
            problem,
            level=logging.ERROR,
            exc_info=False,
        )
    if mixed and not front.embedding_corpus_mixed:
        degraded(
            log,
            "embedding_corpus_mixed",
            "stored vectors from other embedding configurations are not ranked until re-embedded "
            "(`make reembed`) — %s",
            "; ".join(mixed),
            level=logging.WARNING,
            exc_info=False,
        )
    front.vector_width_problem = problem
    front.embedding_corpus_mixed = bool(mixed)


async def _schema_carries_this_image(conn: psycopg.AsyncConnection[Any]) -> bool:
    """Whether `schema_migrations` records the newest migration this image ships.

    One-directional: an image ahead of the schema is unready (its code would hit missing columns),
    while a rollback, whose image is behind a forward-only schema, stays ready. `core/migrate.py`'s
    `migrate.database_ahead` warning reports the other direction. Normally the Helm pre-upgrade hook
    migrates first; this catches `--no-hooks`, `kubectl set image` and a sync past a failed hook.

    A missing ledger (`UndefinedTable`) means nothing was applied and is unready. An unreadable one
    (`InsufficientPrivilege`) is admitted: under a split session store the probe's role may lawfully
    lack access, and refusing would turn a diagnostic into an outage. Readiness rather than startup,
    so applying the migration restores the pod without a restart. Runs on the probe's connection;
    reading the newest shipped filename is one directory listing.
    """
    newest = newest_shipped_migration()
    if newest is None:
        return True
    try:
        cursor = await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)", (newest,)
        )
        row = await cursor.fetchone()
    except psycopg.errors.UndefinedTable:
        # No ledger at all means no migration has been applied: unready.
        log.warning(
            "readiness: schema_migrations does not exist, so no migration has been applied to "
            "this database at all — this pod's code is ahead of the schema and would fail in "
            "traffic. Run the migration (the chart's pre-upgrade hook Job, or `make db-migrate`). "
            "Newest shipped: %s",
            newest,
            exc_info=True,
        )
        return False
    except psycopg.errors.InsufficientPrivilege:
        # A role that may not read the ledger is a legitimate split-store deployment; admit rather
        # than refuse.
        log.warning(
            "readiness: schema_migrations exists but cannot be selected by this role, so the "
            "schema is not being checked against this image (newest shipped: %s)",
            newest,
            exc_info=True,
        )
        return True
    if row is not None and bool(row[0]):
        return True
    log.warning(
        "readiness: this image ships migration %s and the database has not applied it — "
        "this pod's code is ahead of the schema and would fail in traffic. Run the migration "
        "(the chart's pre-upgrade hook Job, or `make db-migrate`).",
        newest,
    )
    return False


async def readyz(request: Request, response: Response) -> dict[str, str | int]:
    """Readiness: the agent can be built, Postgres answers, and how many connectors are down.

    The database gates (under `session_store="postgres"` a pod cannot serve a turn without it); the
    connectors are reported, not gating — `connectors_required` fails startup instead. A process
    running a model-text overlay adds its digest, as `model_text_overlay`. Reported as a
    count, not names: this body is public, and names stay on `/metrics` and in logs. The database
    verdict is reachability plus `_schema_carries_this_image` (directional, so rollbacks stay
    ready).

    503 rather than an exception, so `curl` shows the reason. `/healthz` stays untouched: an outage
    should drain pods, not restart them. Both probes are cached for
    `service_readiness_cache_seconds` (0 probes every time).
    """
    health = await _connector_health(request)
    ready = True
    status = "ready"
    if settings.session_store == "postgres":
        ready = await _database_ready(request)
        if not ready:
            # Which verdict failed, so an operator running `curl` goes to the right system.
            front = state(request)
            status = (
                "database unreachable"
                if not front.database_reachable
                else "schema behind image"
                if not front.schema_current
                else "vector width behind config"
            )
    if not ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    body: dict[str, str | int] = {
        "status": status,
        # `unhealthy`, the same predicate `/metrics` uses, so a jobs-only bundle with no poller
        # counts.
        "connectors_unhealthy": sum(1 for item in health if item.unhealthy),
    }
    if state(request).embedding_corpus_mixed:
        # Degraded, not unready: lexical and graph retrieval answer, and dense retrieval ranks only
        # the rows already under the live configuration. A flag, not names: this body is public.
        body["embedding_corpus"] = "mixed"
    overlay = text_overlay.active()
    if overlay is not None:
        # Present only on a model-text candidate arm, so an evaluation can confirm the door it was
        # pointed at runs the text it was told to (`cli/model_text_eval.check_arms`).
        body["model_text_overlay"] = overlay.digest[: text_overlay.DIGEST_CHARS]
    return body


async def metrics() -> Response:
    """Prometheus exposition for this pod.

    Unauthenticated, like the probes. Safe because the exposition holds only counts, capacity and a
    `profile` label — never a session, user or turn content (enforced by the declared-label
    allowlist). The Route exposes it externally; `route.ipWhitelist` in `deploy/values.yaml`
    restricts that.
    """
    return Response(content=METRICS.render(), media_type=CONTENT_TYPE)


async def schedules(
    principal: CurrentUser,
) -> list[ScheduleHealth]:
    """Health of every periodic job: when it last ran, and whether it succeeded.

    So a failing scheduled sync is visible. Read from Temporal's schedule state, the authority, not
    a mirrored table.
    """
    return await describe_schedules()


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    """
    app.get("/healthz")(healthz)
    app.get("/readyz")(readyz)
    app.get("/metrics")(metrics)
    app.get("/schedules")(schedules)
