"""The tool-audit trail: record every agent tool call once, from one place.

One tool-call middleware wraps every registered tool, observe-only, and records the correlation
id, session, actor, agent, tool name, bounded arguments, outcome, a short effect summary and the
latency. Records go to the stdlib log always (the floor) and to an `AuditSink` when supplied (the
durable record).

Outcomes:

- `ok` — the tool returned a result.
- `error` — it raised, or returned a failure (MCP tools never raise; see `returned_failure`).
- `refused` — a governance gate stopped it, classified by `refusal_reason`.
- `cancelled` — the turn was torn down mid-call; written on a shielded task.
- `empty` — a connector call returned no content.

Arguments are user free text and may contain PII; `agent_audit_max_arg_chars` bounds them.
"""

import asyncio
import logging
import reprlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol, runtime_checkable

from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from chemclaw.agent.plan_link import plan_link_for_call
from chemclaw.agent.tool_result_shape import returned_nothing
from chemclaw.connectors.transport import SERVED_BY
from chemclaw.core.authorship import UNNAMED_AGENT
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
)
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tracing import SpanHandle, start_span
from chemclaw.core.turn_signals import RefusalReason

logger = logging.getLogger(__name__)

# The outcome of a call a governance gate stopped; separate from `error` because a refusal and a
# crash have different remedies.
REFUSED = "refused"

# The outcome a connector call earns when it succeeded and carried no content at all.
EMPTY = "empty"

#: `detail` for an `empty` row — what the call returned, said, because the row has nothing to show.
_EMPTY_DETAIL = "the tool returned no content"


@cache
def _refusal_types() -> tuple[tuple[type[BaseException], RefusalReason], ...]:
    """The governance refusals, most specific first, each paired with the reason it records.

    Classified here, in the middleware every call passes, so a new gate is counted as long as it
    raises one of these. Imported lazily because `agent/tool_authz.py` imports this module; cached
    because it sits on every tool call's exception path. Order matters: all but the last are
    `AuthorizationError` subclasses, so a linear scan finds the most specific reason.
    """
    from chemclaw.agent.authz import AuthorizationError
    from chemclaw.agent.plan_gate import PlanNotApprovedError
    from chemclaw.agent.repeat_guard import RepeatedCallRefusal
    from chemclaw.agent.tool_authz import DryRunRefusal, UndeclaredWriteRefusal

    return (
        (DryRunRefusal, "dry_run"),
        (UndeclaredWriteRefusal, "undeclared_write"),
        (PlanNotApprovedError, "plan_gate"),
        (RepeatedCallRefusal, "repeat"),
        (AuthorizationError, "authz"),
    )


def refusal_reason(exc: BaseException) -> RefusalReason | None:
    """Which gate refused this call, or `None` when the exception is a genuine failure.

    Classified by type, never by message.
    """
    for kind, reason in _refusal_types():
        if isinstance(exc, kind):
            return reason
    return None


# What an unregistered tool name becomes on a metric label. One bucket, not one series per string.
UNKNOWN_TOOL = "unknown"


def metric_tool_name(request: Any, name: str) -> str:
    """The tool name safe to use as a metric label — the registered one, or a fixed bucket.

    `name` is the model's string, and `ToolNode` invokes this chain even for unregistered names, so
    using it raw would let model output (influenceable by injected documents) mint unbounded series.
    The registered tool's own name is used so the label cannot differ by case or invisible
    characters. The audit row keeps the raw string (truncated) as the forensic fact.
    """
    registered = getattr(getattr(request, "tool", None), "name", None)
    return registered if isinstance(registered, str) and registered else UNKNOWN_TOOL


def _observe_tool_latency(name: str, elapsed_ms: float) -> None:
    """Record one tool call's duration in the process histogram, under the tool's own name.

    Here because only this middleware sees a call complete. Failed calls are observed too, since
    they often explain a slow turn. `name` is already clamped by `metric_tool_name`.
    """
    record_metric(
        lambda metrics: metrics.observe(
            "chemclaw_tool_duration_seconds", elapsed_ms / 1000.0, labels={"tool": name}
        )
    )


def _count_outcome(name: str, outcome: str, reason: str | None) -> None:
    """Count one finished tool call, and the gate that stopped it when one did.

    Counted here, not in each gate, so no gate has to remember to count itself.
    """
    record_metric(
        lambda metrics: metrics.increment(
            "chemclaw_tool_calls_total", labels={"tool": name, "outcome": outcome}
        )
    )
    if reason is not None:
        record_metric(
            lambda metrics: metrics.increment(
                "chemclaw_tool_refusals_total", labels={"reason": reason}
            )
        )


class AuditEvent(BaseModel):
    """One recorded tool invocation — the row an `AuditSink` persists."""

    correlation_id: str
    # The conversation this call belongs to, joining it to the question that caused it
    # (`correlation_id` identifies only the turn). Empty off the request path.
    session_id: str = ""
    # Why this call was made, in the requester's terms. Reserved and deliberately unpopulated: an
    # inferred reason is worse than an empty one. It is not filled from `plan_step`, which is a
    # position in a plan, not a reason.
    purpose: str = ""
    actor: str
    # Which agent made this call: the `AgentProfile` name of the graph it ran on, `UNNAMED_AGENT`
    # (`""`) for the agent the chemist talks to. Beside `actor` (the human), never instead of it, so
    # a
    # helper's act is never recorded as the chemist's own. A build-time argument to
    # `make_audit_middleware`, not an ambient read, because each helper is a separately compiled
    # graph
    # with its own chain. `NOT NULL`: every tool call is some agent's act.
    agent: str = UNNAMED_AGENT
    # The plan step this call served — the first `in_progress` todo's content, or empty.
    #
    # Computed by `plan_link_for_call` over the request (see `_plan_step`), not read from the
    # ambient
    # link, which is only bound further in; this also names the step a refused call interrupted.
    plan_step: str = ""
    tool: str
    arguments: str
    # "ok" | "refused" | "error" | "cancelled" | "empty". A plain string with no CHECK constraint;
    # this module is the only producer. `refused` is derived from the exception type by
    # `refusal_reason`.
    outcome: str
    # Result summary on success; the raised or returned failure text on error; why the attempt was
    # cut short on cancellation.
    detail: str = ""
    latency_ms: float
    # When the tool call started, stamped here rather than by the INSERT: the sink batches, so a
    # default would date and order rows by flush time. Start time preserves the order the model
    # issued calls in.
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # The deployment revision (Git SHA / image digest) in effect for this call, tying a result to
    # its
    # prompt/skill/config version. "unknown" until `deployment_revision` is set.
    revision: str = "unknown"
    # The build of the out-of-process server that answered: `<connector>@<revision>`, from the MCP
    # handshake. `revision` names only the orchestrator, so this is what a reproduction of remote
    # physics needs. Empty for an in-process tool; `<connector>@unknown` when the server could not
    # name its build.
    tool_revision: str = ""


@runtime_checkable
class AuditSink(Protocol):
    """Durable destination for audit events. Backends implement this (append-only)."""

    async def record(self, event: AuditEvent) -> None:
        """Persist one audit event. Must not raise into the tool call path."""
        ...


class NullAuditSink:
    """Log-only: the stdlib log is the whole record, because no database is configured."""

    async def record(self, event: AuditEvent) -> None:
        """Discard the event — logging in the middleware already recorded it."""
        return None


def default_audit_sink() -> AuditSink:
    """The sink a caller gets when it names none: durable where a database exists, else log-only.

    Durable by default so a forgotten argument cannot silently downgrade the trail. Gated on
    `session_store="postgres"`, the deployment's statement that Postgres exists; imported lazily so
    dev and tests never pull psycopg.
    """
    if settings.session_store != "postgres":
        return NullAuditSink()
    from chemclaw.agent.audit_store import PostgresAuditSink

    return PostgresAuditSink()


def bounded_repr(value: object) -> str:
    """Render a value as a single-line string bounded by the configured budget.

    The bound applies to the work, not only the output: strings are sliced before repr and
    everything else goes through `reprlib` with raised container limits, so a large payload is never
    fully materialized. Public because it is the tree's one truncation for model-authored strings
    (tool arguments, unparseable calls, requested skill paths).
    """
    limit = settings.agent_audit_max_arg_chars
    if isinstance(value, str):
        text = repr(value if len(value) <= limit else value[:limit])
    else:
        shaper = reprlib.Repr()
        shaper.maxstring = limit + 1
        shaper.maxother = limit + 1
        shaper.maxdict = shaper.maxlist = shaper.maxtuple = shaper.maxset = 64
        shaper.maxlevel = 6
        text = shaper.repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def returned_failure(result: object) -> ToolMessage | None:
    """The failure a tool *returned* instead of raising, or `None` if the call really succeeded.

    MCP tools never raise: `langchain_mcp_adapters` converts `isError=True` into a
    `ToolMessage(status="error")` return. In-process tools raise, so this returns `None` for them.
    `isinstance`, not a class-name test, so `ToolMessageChunk` is caught. Returns the message so
    callers needing its text need not re-test the type.
    """
    if isinstance(result, ToolMessage) and result.status == "error":
        return result
    return None


def _served_by(request: Any) -> str:
    """`"<connector>@<revision>"` when an out-of-process server answers this call, else `""`.

    Reads `request.tool`, which is `None` for an unregistered name; that is safe here because this
    is observational, and `None` yields the same empty string an in-process tool does.
    """
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    served = metadata.get(SERVED_BY)
    if not isinstance(served, dict):
        return ""
    return f"{served.get('connector', '')}@{served.get('revision', '') or 'unknown'}"


def _plan_step(request: Any) -> str:
    """The plan step this call serves, read off the request's own todo list.

    Uses `plan_link_for_call`, the function durable jobs are stamped with, so a call and the job it
    launched agree. Not `request.state["todos"]` directly: that snapshot predates the status flip
    in the same batch and would name the previous step. Empty when there are no todos.
    """
    return plan_link_for_call(request)[0]


def make_audit_middleware(
    *,
    correlation_id: str,
    actor: str,
    sink: AuditSink | None = None,
    agent: str = UNNAMED_AGENT,
) -> AgentMiddleware[Any, Any]:
    """The trail as tool-call middleware — the wiring, with the recording itself in `_recording`.

    This reads name, arguments and result off the request; `_recording` takes plain values, so a
    row's contents never depend on the engine. The `ok` detail is the `ToolMessage` content, since
    that is what the model is actually handed.

    Args:
        correlation_id: The turn this chain belongs to, used when no turn stamped one ambiently.
        actor: The build-time fallback identity, used when no turn bound an authenticated one.
        sink: Where rows go; the process default when omitted.
        agent: Which graph this chain governs (`AuditEvent.agent`): empty for the chemist's agent,
            the profile name for a helper.
    """
    audit_sink: AuditSink = sink if sink is not None else default_audit_sink()
    revision = settings.deployment_revision

    @wrap_tool_call
    async def audit_tool_calls(request: Any, handler: Callable[[Any], Any]) -> Any:
        """Record one audit event per tool invocation (observe-only)."""
        async with _recording(
            request.tool_call["name"],
            request.tool_call.get("args"),
            actor=actor,
            correlation_id=correlation_id,
            sink=audit_sink,
            revision=revision,
            tool_revision=_served_by(request),
            plan_step=_plan_step(request),
            metric_name=metric_tool_name(request, request.tool_call["name"]),
            agent=agent,
        ) as recorded:
            result = await handler(request)
            recorded.result = getattr(result, "content", result)
            # Only the decision (a string or nothing) crosses into `_recording`, never the library
            # class.
            failed = returned_failure(result)
            recorded.returned_error = None if failed is None else bounded_repr(failed.content)
            recorded.returned_empty = bool(_served_by(request)) and returned_nothing(result)
            return result

    return audit_tool_calls


class _Recorded:
    """What the caller must hand back: the tool's result, and whether that result *was* a failure.

    Mutable because the result is known only inside the context manager's block. `returned_error`
    is a plain string so `_recording` stays framework-free.
    """

    result: object | None = None
    returned_error: str | None = None
    #: A connector call that succeeded with no content (`EMPTY`). Same reasoning as above: the
    #: wrapper does the `ToolMessage` test, and what crosses is the decision.
    returned_empty: bool = False


@asynccontextmanager
async def _recording(
    name: str,
    arguments: object,
    *,
    actor: str,
    correlation_id: str,
    sink: AuditSink,
    revision: str,
    tool_revision: str = "",
    plan_step: str = "",
    metric_name: str = "",
    agent: str = UNNAMED_AGENT,
) -> AsyncIterator[_Recorded]:
    """The trail itself, with no framework in it.

    Owns identity precedence, the span, the latency histogram, every outcome and the shielded write
    that survives teardown, once for every engine. Callers supply only the tool's name, arguments,
    plan step, agent and, inside the block, its result.
    """
    args = bounded_repr(arguments)
    # The real actor is the turn's authenticated Entra user (F4-T5); fall back to the static
    # `actor` bound at build time when there is none (tests, the non-service caller).
    event_actor = get_current_actor() or actor
    # Same precedence, same reason: per-turn if a turn stamped one, else the build-time id.
    event_cid = get_current_correlation_id() or correlation_id
    # Read ambiently like the actor: agents are cached per profile for the process, so anything
    # bound
    # at build time would be shared across users. Empty off the request path.
    event_session = get_current_session_id() or ""
    start = time.perf_counter()
    # `start` measures the call; `started_at` dates it for the row (see `AuditEvent.ts`).
    started_at = datetime.now(UTC)

    def event_for(outcome: str, detail: str, elapsed_ms: float) -> AuditEvent:
        """This call's record under `outcome` — the identity fields resolved once, above."""
        return AuditEvent(
            correlation_id=event_cid,
            session_id=event_session,
            actor=event_actor,
            # Fixed at build time: the graph is known by whatever built this chain, unlike the
            # per-turn actor.
            agent=agent,
            plan_step=plan_step,
            tool=name,
            arguments=args,
            outcome=outcome,
            detail=detail,
            latency_ms=elapsed_ms,
            revision=revision,
            tool_revision=tool_revision,
            ts=started_at,
        )

    def finished(span: SpanHandle, outcome: str, reason: str | None, detail: str) -> float:
        """Close out one call: stamp the span, observe the latency, count the outcome.

        Written once so no exit path can forget one. The span is stamped inside the `with` (it
        cannot
        be marked after ending). `Status(ERROR)` is set for returned failures and cancellations,
        which
        OpenTelemetry does not mark itself; refusals are not marked here, and the `outcome`
        attribute
        separates them from errors.
        """
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        span.set_attribute("outcome", outcome)
        if outcome in ("error", "cancelled") and detail:
            span.failed(detail)
        # Clamped name for metrics (a cardinality decision); the model's own for span and row.
        # Passed in
        # so no library object crosses into this function.
        labelled = metric_name or name
        _observe_tool_latency(labelled, elapsed_ms)
        _count_outcome(labelled, outcome, reason)
        return elapsed_ms

    recorded = _Recorded()
    # One span per tool call, which with the turn span is the whole first-party trace. Attributes
    # follow the `/metrics` rule: identifiers, never an argument.
    with start_span("chemclaw.tool", **{"tool.name": name, "correlation.id": event_cid}) as span:
        try:
            yield recorded
        except asyncio.CancelledError:
            # Turn torn down mid-call (client disconnect or deadline). Its own clause because
            # `CancelledError` is a `BaseException` and would bypass the handler below.
            detail = (
                "the turn was torn down while this tool was running (client disconnect or "
                "turn deadline); whether its side effect completed is not known here"
            )
            elapsed_ms = finished(span, "cancelled", None, detail)
            logger.warning(
                "tool %s was cancelled after %.0f ms [cid=%s actor=%s] (args=%s)",
                name,
                elapsed_ms,
                event_cid,
                event_actor,
                args,
            )
            await _emit_shielded(sink, event_for("cancelled", detail, elapsed_ms))
            raise
        except Exception as exc:
            # A gate refusing and a parser raising both land here, and they are not the same
            # event: `refusal_reason` is what separates them, off the exception's *type*.
            reason = refusal_reason(exc)
            outcome = REFUSED if reason is not None else "error"
            detail = bounded_repr(exc)
            elapsed_ms = finished(span, outcome, reason, detail)
            # Log the exception class beside the message so a refusal and a crash are
            # distinguishable.
            logger.warning(
                "tool %s %s after %.0f ms [cid=%s actor=%s]: %s: %s (args=%s)",
                name,
                "was refused" if reason is not None else "failed",
                elapsed_ms,
                event_cid,
                event_actor,
                type(exc).__name__,
                exc,
                args,
            )
            await _emit(sink, event_for(outcome, detail, elapsed_ms))
            raise
        if recorded.returned_error is not None:
            # A returned failure is recorded exactly like a raised one: raising versus returning is
            # a
            # transport detail.
            detail = recorded.returned_error
            elapsed_ms = finished(span, "error", None, detail)
            logger.warning(
                "tool %s returned a failure after %.0f ms [cid=%s actor=%s]: %s (args=%s)",
                name,
                elapsed_ms,
                event_cid,
                event_actor,
                recorded.returned_error,
                args,
            )
            await _emit(sink, event_for("error", detail, elapsed_ms))
            return
        if recorded.returned_empty:
            # WARNING: an empty answer from a fleet tool is almost always that tool's defect,
            # invisible from
            # the model's side.
            elapsed_ms = finished(span, EMPTY, None, _EMPTY_DETAIL)
            logger.warning(
                "tool %s returned no content after %.0f ms [cid=%s actor=%s] (args=%s)",
                name,
                elapsed_ms,
                event_cid,
                event_actor,
                args,
            )
            await _emit(sink, event_for(EMPTY, _EMPTY_DETAIL, elapsed_ms))
            return
        detail = bounded_repr(recorded.result) if recorded.result is not None else ""
        elapsed_ms = finished(span, "ok", None, "")
        logger.info(
            "tool %s ok in %.0f ms [cid=%s actor=%s] (args=%s)",
            name,
            elapsed_ms,
            event_cid,
            event_actor,
            args,
        )
        await _emit(sink, event_for("ok", detail, elapsed_ms))


async def _emit_shielded(sink: AuditSink, event: AuditEvent) -> None:
    """Persist an event from inside a cancellation, on a task that outlives it.

    A plain await would be cancelled at its first suspension and write nothing; `asyncio.shield`
    moves the write to its own task. The `CancelledError` coming back out is the caller's teardown
    and is swallowed so the middleware's own re-raise stands; `_emit` logs any write failure itself.
    """
    try:
        await asyncio.shield(_emit(sink, event))
    except asyncio.CancelledError:
        logger.debug(
            "the audit write for tool %s outlived its cancelled turn; it completes on its own task",
            event.tool,
        )


async def _emit(sink: AuditSink, event: AuditEvent) -> None:
    """Persist an event, never letting a sink failure escape into the tool path."""
    try:
        await sink.record(event)
    except Exception as exc:  # a broken audit store must not fail a tool call
        # Counted as well as logged, so an incomplete trail shows on the dashboard.
        record_metric(lambda metrics: metrics.increment("chemclaw_audit_sink_failures_total"))
        # Continue for availability, but log at ERROR with the stable `audit_sink_failure` marker so
        # monitoring can alert on a lost record.
        logger.error(
            "audit_sink_failure: sink failed to record tool %s (correlation_id=%s actor=%s): %s",
            event.tool,
            event.correlation_id,
            event.actor,
            exc,
            extra={
                "event": "audit_sink_failure",
                "tool": event.tool,
                "correlation_id": event.correlation_id,
                "actor": event.actor,
            },
        )
