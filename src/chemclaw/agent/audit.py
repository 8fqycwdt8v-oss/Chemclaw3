"""The tool-audit trail: record every agent tool call once, from one place.

Why this exists: "who ran what, with which inputs, when, did it succeed, and to what
effect" must be answerable about work a chemist will cite, and it is also the first
thing needed to troubleshoot an agent turn. Rather than sprinkle logging into each of the ~13 tools
(duplication that would drift), one **tool-call middleware** wraps *every* registered
tool uniformly — the audit trail is a single reusable piece (DRY), like the PR-gate.

It is observe-only: it never alters the arguments or the result. Each call records the
correlation id (which conversation), the actor (who — a Phase-6 seam, the configured
`service_actor_id` until Entra identity lands), which specialist ran it (beside the human, never
instead — empty for the main agent), the tool name, its truncated arguments, the
outcome and a short effect summary (e.g. the PR ref a `propose_*` tool returned), and the latency.
Records go to the stdlib log always, and additionally to a durable `AuditSink` when one is
supplied (the Postgres append-only trail) — the log is the floor, the sink is the durable record.

**Three outcomes, not two, because a turn can end without the tool ending.** A client disconnect
and the front door's turn deadline both arrive as `asyncio.CancelledError` (D-130), which is a
`BaseException` and so slipped past the `except Exception` that records a failure: a tool call
interrupted mid-flight left no row at all, and `audit_events` under-reported *attempted* calls
whenever a turn was torn down. The gap was bounded rather than total — the side effect itself stays
traced by `job_records` for a durable job, the `ToolCallEvent` already streamed to the client, the
teardown warning in `chemclaw.api.runner`, and a `turn_costs` row with `completed=false` — but none
of those is the audit trail, and "who attempted what" is exactly what the trail is for. A cancelled
attempt is now its own `cancelled` outcome, distinguishable from both a success and a failure,
written on a shielded task so the write outlives the cancellation that caused it.

**And control flow alone does not tell a success from a failure.** The three outcomes above were
each derived from whether the handler returned, raised, or was cancelled — which is complete only
for tools that signal failure by raising. An **MCP tool never raises**: `langchain_mcp_adapters`
attaches a `handle_tool_error` callback, so an `isError=True` result is converted *inside*
`StructuredTool.ainvoke` and comes back as a `ToolMessage(status="error")`. The handler returned,
so every failed connector call was written to the trail as `ok` — with the error text sitting
in `detail`, the field an auditor reads as the call's *effect*. `returned_failure` is the missing
half of the test: a returned failure is recorded under `error` like a raised one, so the outcome
column means the same thing for a tool that runs in this process and one that runs behind a
connector.

Note: tool arguments and confirmed-answer payloads are user free text, so audit records may
contain PII. `agent_audit_max_arg_chars` bounds what is stored; treat the trail accordingly.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from langchain.agents.middleware import AgentMiddleware, wrap_tool_call
from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
    get_current_specialist,
)
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tracing import start_span

logger = logging.getLogger(__name__)


def _observe_tool_latency(elapsed_ms: float) -> None:
    """Record one tool call's duration in the process histogram.

    Here rather than in `chemclaw.api.runner` because this is the only place that sees a tool call
    *complete* — the runner sees the model announce one and never learns when it returned. Failed
    calls are observed too: a tool that fails after 30 s is exactly the sample that explains a slow
    turn, and dropping it would make the histogram flatter the worse things get.
    """
    record_metric(
        lambda metrics: metrics.observe("chemclaw_tool_duration_seconds", elapsed_ms / 1000.0)
    )


class AuditEvent(BaseModel):
    """One recorded tool invocation — the row an `AuditSink` persists."""

    correlation_id: str
    # The conversation this call belongs to
    # (D-2026-07-31-the-audit-chain-is-versioned, whose hash chain has since been removed).
    # `correlation_id` identifies the *turn* and was
    # stamped on nothing holding the user's words, so a tool call could not be joined to the
    # question that caused it — the trail proved *that* a tool ran and never *why*. D-157 closed
    # this for durable jobs (`job_records` carries the session and a rationale); an ordinary tool
    # call — `gather_evidence`, `predict_pka`, `suggest_next_experiment` — had no such row, and
    # those are most of the trail. Empty off the request path, where there genuinely is no session.
    session_id: str = ""
    # Why this call was made, in the requester's terms. Reserved and deliberately unpopulated: the
    # column is here because schema churn on an append-only table is worth doing once, but nothing
    # fills it yet. Making the model author a reason per call means changing every tool signature,
    # and deriving one from the harness's active todo step is a *heuristic* — a provenance field
    # that is sometimes an inference is worse than an empty one, so it stays empty until it can be
    # authored honestly.
    purpose: str = ""
    actor: str
    # Which specialist made this call — the `AgentProfile` name of the running subagent, empty for
    # the main agent (D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor, invariant 3).
    #
    # **Beside `actor`, never instead of it**, and deliberately not folded into either neighbouring
    # field. Overloading `actor` — the human's Entra oid — would produce exactly the D-040 failure
    # this system has already been bitten by: the trail recorded an agent's self-authorization under
    # the chemist's identity, which is worse than an unrecorded act because it *looks* attributable.
    # And `purpose` is reserved for why a call was made, which is a different question with a
    # different (still unanswerable) answer; filling it with an agent name would spend the one
    # column that is honest about being empty.
    #
    # A trail that names only the person cannot say which of five specialists ran a tool; a trail
    # that names only the agent is worthless: it has to carry both, so it does.
    agent: str = ""
    tool: str
    arguments: str
    # "ok" | "error" | "cancelled". Deliberately a plain string with no CHECK behind it: the column
    # has none (`infra/sql/006`), and adding one to an append-only table to police three
    # literals would cost a migration on every future outcome. The producer is this module alone.
    outcome: str
    # Result summary on success, the exception text — or the failure the tool *returned*, for a
    # connector tool, which never raises — on failure, why the attempt was cut short on a
    # cancellation.
    detail: str = ""
    latency_ms: float
    # The deployment revision (Git SHA / image digest) in effect for this call (AG-14): ties a past
    # result to the exact prompt/skill/config version that produced it. "unknown" until a deployment
    # sets `deployment_revision`.
    revision: str = "unknown"


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

    **The default is here, and not at each entry point, because "each entry point remembers" is
    exactly what failed.** `PostgresAuditSink`, its table and its tests were all built — and the
    sink was constructed in exactly one place, `cli/chat.py`, behind `--audit-postgres`. The
    deployed service passed nothing, so this module installed `NullAuditSink()` and the entire trail
    was log-only in every process a chemist actually talks to. `audit_events` was empty in
    production while every document called it the durable record. The Temporal template activities
    had the same gap, independently.

    Opting *in* to the durable record, per call site, is the wrong polarity: a forgotten argument
    must not silently downgrade it. So the durable sink is what you get, and
    log-only is what a deployment with no database falls back to.

    Gated on `session_store="postgres"` for the same reason `_default_owner_store` is: that switch
    is the deployment's statement that a Postgres exists and durable records belong in it. Imported
    lazily so the dev/test path never pulls psycopg for a store it will not use.
    """
    if settings.session_store != "postgres":
        return NullAuditSink()
    from chemclaw.agent.audit_store import PostgresAuditSink

    return PostgresAuditSink()


def _truncate(value: object) -> str:
    """Render a value as a single-line string bounded by the configured budget.

    A tool argument or result can be a large object (a full optimization problem, an
    evidence sweep); truncating keeps one audit record from ballooning while still
    identifying the call and its effect.
    """
    text = repr(value)
    limit = settings.agent_audit_max_arg_chars
    return text if len(text) <= limit else text[:limit] + "…"


def returned_failure(result: object) -> ToolMessage | None:
    """The failure a tool *returned* instead of raising, or `None` if the call really succeeded.

    Why this has to exist: **an MCP tool never raises.** `langchain_mcp_adapters` builds each
    connector tool with a `handle_tool_error` callback, so a server that reports `isError=True` is
    converted inside `StructuredTool.ainvoke` and surfaces as an ordinary *return* —
    a `ToolMessage` whose `status` is `"error"`. Every reader that decides success by control flow
    therefore reads a failed connector call as a success: the audit trail wrote `ok` with the error
    text in `detail`, and the chemist's transcript announced no failure at all. In-process tools and
    job tools do raise, so this returns `None` for them and nothing is reported twice.

    `isinstance`, deliberately, and not a class-name test: `ToolMessageChunk` is a real subclass, so
    a name comparison misses it *silently* — the branch simply does not run, and the outcome is the
    same wrong `ok`. `api/graph_stream.py` makes the identical point at its own `ToolMessage` check.

    Returned rather than reduced to a bool so the one caller that needs the message's text
    (`agent/tool_authz.returned_failure_detail`) does not have to re-test the type to get it.
    """
    if isinstance(result, ToolMessage) and result.status == "error":
        return result
    return None


def make_audit_middleware(
    *,
    correlation_id: str,
    actor: str,
    sink: AuditSink | None = None,
) -> AgentMiddleware[Any, Any]:
    """The trail as tool-call middleware — the wiring, with the recording itself in `_recording`.

    Split that way on purpose. `_recording` is where an audit row is decided and written, and it
    takes plain values; this reads the tool's name, arguments and result off the request and hands
    them over. Keeping the decision framework-free is what let the engine underneath be replaced
    without an audit row's contents depending on which engine ran (D-2026-08-10 §4), and it is
    what a second caller — a template step, a job replay — reuses instead of re-deriving.

    The result recorded as the `ok` detail is the `ToolMessage`'s content rather than a raw return
    value, because that is what the model is actually handed: an audit row saying what the tool
    computed, where the model read something else, would be a record of the wrong event.
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
        ) as recorded:
            result = await handler(request)
            recorded.result = getattr(result, "content", result)
            # The `ToolMessage` test lives in `returned_failure`, and what crosses into `_recording`
            # is the *decision* — a string or nothing — never the library class. That is the same
            # line the name/arguments split above draws, and it is what keeps the recording itself
            # framework-free.
            failed = returned_failure(result)
            recorded.returned_error = None if failed is None else _truncate(failed.content)
            return result

    return audit_tool_calls


class _Recorded:
    """What the caller must hand back: the tool's result, and whether that result *was* a failure.

    A mutable holder rather than a return value because `_recording` is a context manager, and the
    result is only known inside the block — the wrapper assigns what `handler` returned before the
    block exits and the row is written.

    `returned_error` is set when the tool reported its failure by returning rather than raising (an
    MCP tool always does; see `returned_failure`). It is a plain string so the recording below stays
    framework-free: the wrapper does the `ToolMessage` test, and what crosses this boundary is the
    decision it reached.
    """

    result: object | None = None
    returned_error: str | None = None


@asynccontextmanager
async def _recording(
    name: str,
    arguments: object,
    *,
    actor: str,
    correlation_id: str,
    sink: AuditSink,
    revision: str,
) -> AsyncIterator[_Recorded]:
    """The trail itself, with no framework in it — both engines' middlewares are wrappers.

    Everything that makes this the *record* lives here: the identity precedence, the span, the
    latency histogram, the three outcomes, and the shielded write that survives a teardown. A
    second copy of it for the second engine would be the one duplication this system cannot
    afford — an audit trail that disagrees with itself depending on a config flag is not a trail,
    and the `cancelled` outcome exists precisely because a subtle omission here went unnoticed
    until it was measured (D-130).

    What each engine supplies is only the three things it alone knows: the tool's name, its
    arguments, and — inside the block — its result.
    """
    args = _truncate(arguments)
    # The real actor is the turn's authenticated Entra user (F4-T5); fall back to the static
    # `actor` bound at build time when there is none (tests, the non-service caller).
    event_actor = get_current_actor() or actor
    # Same precedence, same reason: per-turn if a turn stamped one, else the build-time id.
    event_cid = get_current_correlation_id() or correlation_id
    # The conversation, read ambiently for the same reason the actor is: a tool has no request
    # context, and an agent is cached per profile for the process's life, so anything bound at
    # build time would be shared by every user on the pod. Empty off the request path.
    event_session = get_current_session_id() or ""
    # Which specialist is running, read here and once, so the trail names the agent beside the human
    # without any tool signature growing a field. Empty means the main agent, which is a complete
    # answer rather than a missing one. No fallback: unlike the actor and the correlation id there
    # is nothing sensible to bind at build time — an agent is cached per profile for the process's
    # life, so a build-time specialist would label every turn on the pod with whichever subgraph
    # happened to be built first.
    event_agent = get_current_specialist()
    start = time.perf_counter()

    def event_for(outcome: str, detail: str, elapsed_ms: float) -> AuditEvent:
        """This call's record under `outcome` — the identity fields resolved once, above."""
        return AuditEvent(
            correlation_id=event_cid,
            session_id=event_session,
            actor=event_actor,
            agent=event_agent,
            tool=name,
            arguments=args,
            outcome=outcome,
            detail=detail,
            latency_ms=elapsed_ms,
            revision=revision,
        )

    recorded = _Recorded()
    try:
        # One span per tool call, which with the turn span above it is the whole first-party
        # trace: "this question took 40 seconds and 31 of them were one xTB call" is the
        # question an operator actually asks, and nothing could answer it. Deliberately not a
        # span per loop iteration or per retriever — the finding was that the docs *overstate*
        # the tracing, and answering that with more unread spans is the same mistake mirrored.
        with start_span("chemclaw.tool", **{"tool.name": name}):
            yield recorded
    except asyncio.CancelledError:
        # The turn was torn down while this tool was still running — a client disconnect or the
        # front door's wall-clock deadline, which both deliver exactly this (D-130). Its own
        # clause because `CancelledError` derives from `BaseException`, so the handler below
        # never saw it and an interrupted attempt left no row in the trail at all.
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _observe_tool_latency(elapsed_ms)
        logger.warning(
            "tool %s was cancelled after %.0f ms [cid=%s actor=%s] (args=%s)",
            name,
            elapsed_ms,
            event_cid,
            event_actor,
            args,
        )
        await _emit_shielded(
            sink,
            event_for(
                "cancelled",
                "the turn was torn down while this tool was running (client disconnect or "
                "turn deadline); whether its side effect completed is not known here",
                elapsed_ms,
            ),
        )
        raise
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _observe_tool_latency(elapsed_ms)
        logger.warning(
            "tool %s failed after %.0f ms [cid=%s actor=%s]: %s (args=%s)",
            name,
            elapsed_ms,
            event_cid,
            event_actor,
            exc,
            args,
        )
        await _emit(sink, event_for("error", _truncate(exc), elapsed_ms))
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _observe_tool_latency(elapsed_ms)
    if recorded.returned_error is not None:
        # The handler returned, and what it returned was a failure. Recorded exactly like a raised
        # one — same outcome, same WARNING — because the difference between raising and returning is
        # a property of the tool's transport, and an auditor reading the `outcome` column is asking
        # about the call's effect.
        logger.warning(
            "tool %s returned a failure after %.0f ms [cid=%s actor=%s]: %s (args=%s)",
            name,
            elapsed_ms,
            event_cid,
            event_actor,
            recorded.returned_error,
            args,
        )
        await _emit(sink, event_for("error", recorded.returned_error, elapsed_ms))
        return
    detail = _truncate(recorded.result) if recorded.result is not None else ""
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

    The reason the cancelled-attempt row needs its own writer: this runs while the task is already
    being cancelled, so a plain `await _emit(...)` is cancelled at its first suspension point — it
    would reach the sink's first `await` and write nothing, which is the same missing row it was
    added to fix. `asyncio.shield` puts the write on its own task, exactly as
    `chemclaw.api.runner`'s durable-history rollback does for the identical reason.

    The `CancelledError` that comes straight back out of the shield is the caller's teardown
    resuming, not a failure of the write, so it is swallowed here: letting it out would replace the
    cancellation the middleware is re-raising. The write itself carries on and reports its own
    failure — `_emit` already swallows and logs, which is what a shielded task must do, since once
    the awaiting task is cancelled nothing collects its result and an escaping error would surface
    only as an unattributed `Task exception was never retrieved`.
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
        # Counted as well as logged (gap DEP-4): the ERROR marker is alertable only if something
        # is watching the logs, whereas an incomplete audit trail should be visible on the same
        # dashboard as everything else.
        record_metric(lambda metrics: metrics.increment("chemclaw_audit_sink_failures_total"))
        # Swallow-and-continue keeps availability, but a lost audit record must be ALERTABLE,
        # not a generic warning (SEC-3): log at ERROR with a stable `audit_sink_failure` marker and
        # the trail identifiers, so monitoring can fire on the marker and name the affected trail.
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
