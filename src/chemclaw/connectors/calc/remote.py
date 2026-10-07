"""The calculation client: ask the physics server for a key, then for the answer.

The engines run in `Chemclaw3-mcp`'s `servers/calc`; this repository keeps the D-011 cache, the
calibration ledger and the orchestration, so every calculator here is lookup-then-maybe-compute
across a wire.

Two calls, not one: `cached_compute` needs the `CalculationKey` before computing, so the server's
`calculation_key` derives the identity without running anything (key -> lookup -> compute only on
a miss). Nothing here derives a `calc_version`: it is half the cache key and the calibration
ledger's primary key, and only the server can see what it is built from — a locally derived one
would be well-formed and match nothing.

One session per call, because MCP transport tasks inherit the context of whoever opened the
connection, and a shared session would misattribute concurrent callers.
"""

import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from mcp import ClientSession
from pydantic import BaseModel, ConfigDict

from chemclaw.core.call_identity import turn_identity_hook
from chemclaw.core.config import settings
from chemclaw.core.errors import AtCapacityError, ChemclawError, SubsystemUnavailableError
from chemclaw.core.ids import stable_hash
from chemclaw.core.mcp_session import (
    McpAtCapacity,
    McpConnectFailed,
    McpCredentialRefused,
    McpRequestRefused,
    McpServerFault,
    McpTimeBudget,
    invoke,
    open_session,
)
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded, record_metric
from chemclaw.science.calc.store import (
    CALCULATION_EPOCH,
    CalculationKey,
    ResultPayload,
    ResultStore,
    cached_compute,
)

logger = logging.getLogger(__name__)

# Only outage paths are counted under `degraded`; a refusal is the server working and must not share
# a series with a down pod. One subsystem name for all outage sites, written as a literal at each:
# `tests/test_degraded.py` reads these arguments from source to bound the label space.


class CalcServerError(SubsystemUnavailableError):
    """The calculation server could not be reached, so the calculation never began.

    Retryable (a `SubsystemUnavailableError`, absent from `durable/publish.py`'s non-retryable
    list), unlike a refusal (`CalcToolError`). The message is written for the chemist, because
    `agent/tool_authz.py` hands it to the model verbatim; address and driver text ride on
    `__cause__`.
    """


class CalcToolError(ChemclawError):
    """The calculation server was reached and refused, or answered something unusable.

    Bad data: the identical call fails identically next time, so it is registered non-retryable in
    `durable/publish.py::_BAD_DATA_TYPES`. A full pod is `CalcBusyError`, not this.
    """


class CalcTimeBudgetError(CalcToolError):
    """The calculation server's inline wall clock stopped this calculation before it answered.

    Still non-retryable (registered by its own name, since Temporal matches by name): a retry
    re-runs the same work against the same clock. The distinct name lets a per-item screen record a
    time-budget stop rather than an input failure, since wall clock depends on load.
    """


class CalcBusyError(AtCapacityError):
    """The calculation server was reached, ran nothing, and refused because every slot was busy.

    Retryable by construction: `SubsystemUnavailableError`'s hierarchy is asserted absent from
    `_BAD_DATA_TYPES` (`tests/test_publish.py`), so saturation is never failed as bad data. The
    backoff is the activity's (`durable/publish.py::calculation_retry`). The message tells the
    chemist the molecule is fine and covers both the durable path (automatic retry) and the tool
    path (none).
    """

    server = "calc"


# Transport, timeouts and credential handling live in `core/mcp_session.py`; this module adds the
# error classification and the wording a chemist reads.


#: Sessions to the calculation server this process holds open now: the live half of that backend's
#: admission budget. A plain locked counter rather than a registry metric, because it must fall on
#: every exit path, a failed open included, or it climbs during an outage.
_IN_FLIGHT = 0
_IN_FLIGHT_LOCK = threading.Lock()


def _dispatching(delta: int) -> None:
    """Move the in-flight count by `delta`, so a scrape sees the sessions actually held."""
    global _IN_FLIGHT
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT += delta


# Bound at import: any process that can dispatch a calculation also publishes this gauge.
METRICS.bind_gauge("chemclaw_calc_requests_in_flight", lambda: float(_IN_FLIGHT))
METRICS.bind_gauge(
    "chemclaw_calc_backend_max_concurrent_requests",
    lambda: float(settings.calc_backend_max_concurrent_requests),
)


@asynccontextmanager
async def calc_session(timeout_seconds: float | None = None) -> AsyncIterator[ClientSession]:
    """Open one MCP session to the calculation server, and name its failures for a chemist.

    Session mechanics are `core.mcp_session.open_session`'s. This adds the classification: a refused
    credential is bad data (it never fixes itself), an unreachable host is an outage. It is also
    where backend load is counted, as sessions held, since every remote calculation passes through
    here.

    `timeout_seconds` overrides the read bound (default `calc_server_timeout_seconds`) for CREST
    searches, which run minutes to hours; a client bound shorter than the server's wastes the work.
    """
    _dispatching(+1)
    try:
        async with open_session(
            settings.calc_server_url,
            token_env=settings.calc_server_token_env,
            timeout_seconds=timeout_seconds or settings.calc_server_timeout_seconds,
            # The same hook every connector client carries: trace context, correlation id, actor and
            # session, plus the origin-strip guard that removes them on a cross-origin redirect
            # (this client follows redirects).
            request_hook=turn_identity_hook(settings.calc_server_url),
        ) as session:
            yield session
    except McpCredentialRefused as exc:
        raise CalcToolError(
            f"the calculation service refused this client's credential "
            f"(HTTP {exc.status} from {settings.calc_server_url}). The service is running and "
            f"answering; it does not accept the bearer taken from "
            f"{settings.calc_server_token_env}. Set that variable to the value the server "
            f"verifies — retrying will not help."
        ) from exc
    except McpConnectFailed as exc:
        degraded(
            logger,
            "calc_server",
            "cannot reach the calculation server at %s; no calculation was run",
            settings.calc_server_url,
        )
        raise CalcServerError(
            "the calculation service is not answering, so no calculation was run. This is an "
            "outage rather than a problem with what was asked; the same request will work once "
            "it is back."
        ) from exc
    finally:
        _dispatching(-1)


async def _call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    """Invoke one tool and return its decoded payload, in this service's error vocabulary.

    Maps `core.mcp_session.invoke`'s refused / full / broken onto the classes a durable retry policy
    reads. A domain refusal keeps the server's message (it is the whole content); a saturation
    refusal is reworded, with the original on `__cause__`.
    """
    try:
        return await invoke(session, tool, arguments)
    except McpAtCapacity as exc:
        # Must precede `McpRequestRefused`, its base class. A full pod is the gate working, so it
        # gets its own saturation counter rather than the `degraded` outage series.
        record_metric(
            lambda m: m.increment("chemclaw_calc_backend_at_capacity_total", labels={"tool": tool})
        )
        # Says what happened, not "will be retried": this is reached from both the durable and the
        # tool path.
        logger.warning(
            "the calculation server refused %s because every calculation slot was taken", tool
        )
        # Worded to be true on both paths: the durable job retries, the tool surface does not.
        raise CalcBusyError(
            f"the calculation service is busy: every calculation slot was taken when {tool} was "
            "asked for, so it was turned away before any work started. Nothing is wrong with what "
            "was asked, and the same request succeeds once a calculation finishes: a durable job "
            "waits and asks again on its own, and a direct call has to be made again."
        ) from exc
    except McpTimeBudget as exc:
        # Before `McpRequestRefused`, of which this is a subclass: order is the whole behaviour.
        raise CalcTimeBudgetError(str(exc)) from exc
    except McpRequestRefused as exc:
        raise CalcToolError(str(exc)) from exc
    except McpServerFault as exc:
        if exc.internal:
            degraded(
                logger,
                "calc_server",
                "the calculation server raised an internal error running %s",
                tool,
            )
            raise CalcServerError(
                f"the calculation service hit an internal error running {tool}, so no result was "
                "produced. This is a fault on the calculation service rather than a problem with "
                "what was asked; the same request may work on a retry."
            ) from exc
        degraded(
            logger,
            "calc_server",
            "the calculation server stopped answering during %s",
            tool,
        )
        raise CalcServerError(
            f"the calculation service stopped answering during {tool}, so no result was produced. "
            "This is an outage rather than a problem with what was asked."
        ) from exc


class KeyedCalculation(BaseModel):
    """A calculation's identity, and the geometry it is about when it is about one.

    Two facts from one `calculation_key` round trip, because the server answers both and this
    client used to read only the first. `structure_id` is what makes "have we already relaxed this
    conformer?" answerable at all (D-2026-08-21): a `calculation_results` row's `input_hash` is a
    digest, so nothing about a stored row said which geometry it described — the server knows,
    and says so, and the answer was being dropped on the floor.

    Empty for a molecule-keyed calculation (pKa, solubility, descriptors), which is the honest
    value: those are about a compound and not about any particular geometry of it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: CalculationKey
    structure_id: str = ""


async def _identity(session: ClientSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """`calculation_key`'s answer for `tool`, refused as `CalcToolError` unless it is an object."""
    identity = await _call(session, "calculation_key", {"tool": tool, "arguments": arguments})
    if not isinstance(identity, dict):
        raise CalcToolError(f"calculation_key returned {type(identity).__name__} for {tool}")
    return identity


async def remote_key(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> KeyedCalculation | None:
    """The `CalculationKey` this tool would stamp on its result, without computing anything.

    `None` when the server reports no derivable key; `cached_remote` treats that as a miswiring. The
    key arrives as its four parts, not the flat string, because a real `calc_version` can contain
    both `@` and `:`.
    """
    identity = await _identity(session, tool, arguments)
    key = identity.get("key")
    if key is None:
        return None
    # `CALCULATION_EPOCH` is folded into `params_hash` here, because these keys are rebuilt field by
    # field rather than through `CalculationKey.build`; bumping it invalidates every `calc` row
    # without touching the server's notion of identity.
    try:
        return KeyedCalculation(
            key=CalculationKey(
                calc_type=key["calc_type"],
                calc_version=key["calc_version"],
                input_hash=key["input_hash"],
                params_hash=stable_hash(
                    {"epoch": CALCULATION_EPOCH, "remote_params": key["params_hash"]}
                ),
            ),
            # The server's own value, never re-derived; absent for a molecule-keyed calculation.
            structure_id=str(identity.get("structure_id") or ""),
        )
    except (KeyError, TypeError) as exc:
        raise CalcToolError(f"calculation_key returned an unusable key for {tool}: {key}") from exc


async def remote_compute(
    session: ClientSession, tool: str, arguments: dict[str, Any]
) -> ResultPayload:
    """Run one calculation on the server and return its payload as the cache stores it."""
    payload = await _call(session, tool, arguments)
    if not isinstance(payload, dict):
        raise CalcToolError(f"{tool} returned {type(payload).__name__}, not an object")
    return payload


async def remote_call(tool: str, arguments: dict[str, Any]) -> ResultPayload:
    """One round trip to a tool that has no cache row, in its own session.

    For `embed_structure` and `combine_structures`, which `calculation_key` refuses. They run on the
    server because their output feeds a key: a geometry from a different RDKit build would change
    every downstream `structure_id`.
    """
    async with calc_session() as session:
        return await remote_compute(session, tool, arguments)


#: The calibrated calculators, and the only tools `remote_version` accepts. A `Literal` rather than
#: `str` so mypy and the sibling-manifest seam walker check every call site against the fleet.
CalibratedTool = Literal["predict_solubility", "predict_pka"]


async def remote_version(tool: CalibratedTool, arguments: dict[str, Any]) -> str:
    """The `calc_version` this tool would stamp on a result, without computing one.

    The only way to learn a calculator's current version, which `calculator_trust` needs because the
    calibration ledger is keyed exactly on version. `arguments` are required by `calculation_key`
    but do not affect the version; callers pass `settings.calc_version_probe_smiles`.
    """
    async with calc_session() as session:
        identity = await _identity(session, tool, arguments)
    version = identity.get("calc_version")
    if not isinstance(version, str) or not version:
        raise CalcToolError(f"calculation_key returned no calc_version for {tool}: {identity}")
    return version


# Calculation keys reached inside the current `collecting()` block, in first-seen order. A
# contextvar so concurrent activities stay separate, and mutated rather than rebound so it stays
# visible across child tasks.
_collected: ContextVar[list[str] | None] = ContextVar("chemclaw_calc_refs", default=None)


@contextmanager
def collecting() -> Iterator[list[str]]:
    """Collect the calculation keys reached inside this block, for a run to cite afterwards.

    A collector rather than a return value, so cache bookkeeping stays out of every chemistry
    signature between a durable job and its primitives. The job puts the keys on its envelope so a
    note drafted from the run can cite them. De-duplicated, order preserved.
    """
    keys: list[str] = []
    token = _collected.set(keys)
    try:
        yield keys
    finally:
        _collected.reset(token)


def _record(key: CalculationKey) -> None:
    """Note one key against the enclosing `collecting()` block, if there is one."""
    keys = _collected.get()
    if keys is None:
        return
    flat = key.as_str()
    if flat not in keys:
        keys.append(flat)


async def cached_remote(
    store: ResultStore,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
) -> tuple[ResultPayload, bool]:
    """One calculation: look it up by the server's own key, compute remotely only on a miss.

    A persisted result is never recomputed. Both calls share one session, which is safe because they
    belong to one caller; a hit costs one `calculation_key` round trip. A tool the server will not
    key is a caller error, not a silent uncached compute.
    """
    async with calc_session(timeout_seconds) as session:
        keyed = await remote_key(session, tool, arguments)
        if keyed is None:
            raise CalcToolError(
                f"{tool} has no derivable cache key, so it cannot be routed through the cache. "
                "Either it is composed here from keyed primitives (as predict_logd is), or it "
                "should be called with remote_call."
            )

        # Recorded on hit and miss alike: what a run rested on is the same either way.
        _record(keyed.key)

        async def _compute() -> ResultPayload:
            return await remote_compute(session, tool, arguments)

        return await cached_compute(store, keyed.key, _compute, structure_id=keyed.structure_id)
