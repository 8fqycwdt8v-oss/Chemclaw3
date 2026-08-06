"""GxP tool-audit trail: record every agent tool call once, from one place.

Why this exists: in a pharma/GxP setting "who ran what, with which inputs, when, did it
succeed, and to what effect" must be answerable, and it is the first thing needed to
troubleshoot an agent turn. Rather than sprinkle logging into each of the ~13 tools
(duplication that would drift), one MAF **function middleware** wraps *every* registered
tool uniformly — the audit trail is a single reusable piece (DRY), like the PR-gate.

It is observe-only: it never alters the arguments or the result. Each call records the
correlation id (which conversation), the actor (who — a Phase-6 seam, the configured
`service_actor_id` until Entra identity lands), the tool name, its truncated arguments, the
outcome and a short effect summary (e.g. the PR ref a `propose_*` tool returned), and the latency.
Records go to the stdlib log always, and additionally to a durable `AuditSink` when one is
supplied (the Postgres append-only trail) — the log is the floor, the sink is the GxP record.

**Three outcomes, not two, because a turn can end without the tool ending.** A client disconnect
and the front door's turn deadline both arrive as `asyncio.CancelledError` (D-130), which is a
`BaseException` and so slipped past the `except Exception` that records a failure: a tool call
interrupted mid-flight left no row at all, and `audit_events` under-reported *attempted* calls
whenever a turn was torn down. The gap was bounded rather than total — the side effect itself stays
traced by `job_records` for a durable job, the `ToolCallEvent` already streamed to the client, the
teardown warning in `chemclaw.api.runner`, and a `turn_costs` row with `completed=false` — but none
of those is the GxP trail, and "who attempted what" is exactly what the trail is for. A cancelled
attempt is now its own `cancelled` outcome, distinguishable from both a success and a failure,
written on a shielded task so the write outlives the cancellation that caused it.

Note: tool arguments and confirmed-answer payloads are user free text, so audit records may
contain PII. `agent_audit_max_arg_chars` bounds what is stored; treat the trail accordingly.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

from agent_framework import FunctionInvocationContext, function_middleware
from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
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
    # (D-2026-07-31-the-audit-chain-is-versioned).
    # `correlation_id` identifies the *turn* and was
    # stamped on nothing holding the user's words, so a tool call could not be joined to the
    # question that caused it — the trail proved *that* a tool ran and never *why*. D-157 closed
    # this for durable jobs (`job_records` carries the session and a rationale); an ordinary tool
    # call — `gather_evidence`, `predict_pka`, `suggest_next_experiment` — had no such row, and
    # those are most of the trail. Empty off the request path, where there genuinely is no session.
    session_id: str = ""
    # Why this call was made, in the requester's terms. Reserved and deliberately unpopulated: the
    # column is here because schema churn on a hash-chained table is worth doing once, but nothing
    # fills it yet. Making the model author a reason per call means changing every tool signature,
    # and deriving one from the harness's active todo step is a *heuristic* — a provenance field
    # that is sometimes an inference is worse than an empty one, so it stays empty until it can be
    # authored honestly.
    purpose: str = ""
    actor: str
    tool: str
    arguments: str
    # "ok" | "error" | "cancelled". Deliberately a plain string with no CHECK behind it: the column
    # has none (`infra/sql/006`), and adding one to a hash-chained append-only table to police three
    # literals would cost a migration on every future outcome. The producer is this module alone.
    outcome: str
    # Result summary on success, exception text on failure, why the attempt was cut short on a
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
    exactly what failed.** `PostgresAuditSink`, the tamper-evident hash chain, `infra/sql/011`,
    `make audit-verify` and `durable/audit_chain.py` were all built and tested — and the sink
    was constructed in exactly one place, `cli/chat.py`, behind `--audit-postgres`. The deployed
    service passed nothing, so this module installed `NullAuditSink()` and the entire GxP trail was
    log-only in every process a chemist actually talks to. `audit_events` was empty in production
    while every document called it the compliance record. The Temporal template activities had the
    same gap, independently.

    Opting *in* to the compliance record, per call site, is the wrong polarity for a GxP control: a
    forgotten argument must not silently downgrade it. So the durable sink is what you get, and
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


def make_audit_middleware(
    *,
    correlation_id: str,
    actor: str,
    sink: AuditSink | None = None,
) -> Callable[[FunctionInvocationContext, Callable[[], Awaitable[None]]], Awaitable[None]]:
    """Build the tool-audit middleware bound to one conversation's identity.

    `correlation_id` and `actor` are both *fallbacks*, used only when the call has no ambient one
    (`chemclaw.core.identity_context`): the turn's real Entra user takes precedence per call
    (F4-T5), and
    so does the turn's correlation id. The id has to work that way because agents are cached per
    profile for the process's lifetime — an id bound here would be shared by every turn from every
    user on the pod, which would make the audit trail unable to separate two conversations. The
    build-time value still serves the callers that bind a meaningful one and stamp nothing per turn
    (the Temporal template activities pass the workflow id).

    `sink` is the durable trail. Omitted means `default_audit_sink()` — durable wherever a
    database is configured — so a caller that forgets downgrades nothing; pass `NullAuditSink()`
    explicitly to opt out. A sink failure is logged and swallowed: the audit store must never
    break a tool call.
    """
    audit_sink: AuditSink = sink if sink is not None else default_audit_sink()
    # The revision in effect for this process, captured once at build time (AG-14) — every event
    # this middleware records carries it, so a result ties to the exact version that produced it.
    revision = settings.deployment_revision

    @function_middleware
    async def audit_tool_calls(
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Record one audit event per tool invocation (observe-only)."""
        # Tells the dispatch wrapper that this call reached the middleware at all. See
        # `_REACHED_MIDDLEWARE` and `install_rejected_call_audit` for why the wrapper
        # cannot answer that question for itself.
        _REACHED_MIDDLEWARE.set(True)
        name = context.function.name
        args = _truncate(context.arguments)
        # The real actor is the turn's authenticated Entra user (F4-T5); fall back to the static
        # `actor` bound at build time when there is none (tests, the non-service caller).
        event_actor = get_current_actor() or actor
        # Same precedence, same reason: per-turn if a turn stamped one, else the build-time id.
        event_cid = get_current_correlation_id() or correlation_id
        # The conversation, read ambiently for the same reason the actor is: a tool has no request
        # context, and an agent is cached per profile for the process's life, so anything bound at
        # build time would be shared by every user on the pod. Empty off the request path.
        event_session = get_current_session_id() or ""
        start = time.perf_counter()

        def event_for(outcome: str, detail: str, elapsed_ms: float) -> AuditEvent:
            """This call's record under `outcome` — the identity fields resolved once, above."""
            return AuditEvent(
                correlation_id=event_cid,
                session_id=event_session,
                actor=event_actor,
                tool=name,
                arguments=args,
                outcome=outcome,
                detail=detail,
                latency_ms=elapsed_ms,
                revision=revision,
            )

        try:
            # One span per tool call, which with the turn span above it is the whole first-party
            # trace: "this question took 40 seconds and 31 of them were one xTB call" is the
            # question an operator actually asks, and nothing could answer it. Deliberately not a
            # span per loop iteration or per retriever — the finding was that the docs *overstate*
            # the tracing, and answering that with more unread spans is the same mistake mirrored.
            with start_span("chemclaw.tool", **{"tool.name": name}):
                await call_next()
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
                audit_sink,
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
            await _emit(audit_sink, event_for("error", _truncate(exc), elapsed_ms))
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _observe_tool_latency(elapsed_ms)
        detail = _truncate(context.result) if context.result is not None else ""
        logger.info(
            "tool %s ok in %.0f ms [cid=%s actor=%s] (args=%s)",
            name,
            elapsed_ms,
            event_cid,
            event_actor,
            args,
        )
        await _emit(audit_sink, event_for("ok", detail, elapsed_ms))

    return audit_tool_calls


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
        # is watching the logs, whereas an incomplete GxP trail should be visible on the same
        # dashboard as everything else.
        record_metric(lambda metrics: metrics.increment("chemclaw_audit_sink_failures_total"))
        # Swallow-and-continue keeps availability, but a lost GxP audit record must be ALERTABLE,
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


# Whether the audit middleware ran for the call currently being dispatched.
#
# Set by the middleware, read by `install_rejected_call_audit` right after it awaits
# MAF's dispatch. A `ContextVar` rather than an attribute because it has to be per *call*: an
# `await` runs in the caller's context, so a value set inside the middleware is visible to the
# wrapper that awaited it, while `asyncio.gather` copies the context per task, so two parallel tool
# calls cannot see each other's marker. Both halves measured before this was built.
_REACHED_MIDDLEWARE: ContextVar[bool] = ContextVar(
    "chemclaw_tool_reached_middleware", default=False
)

# The outcome recorded for a call the model made and the framework refused before running anything.
# Its own value rather than reusing `error`: nothing executed, so there is no failure to report —
# what happened is that the *attempt* was malformed, and a trail that cannot tell those apart cannot
# answer "what did the agent try" without also implying it ran.
REJECTED_OUTCOME = "rejected"


def install_rejected_call_audit() -> Callable[[], None]:
    """Record the tool calls MAF refuses before any middleware runs. Returns an undo.

    **The gap.** `agent_framework._tools._auto_invoke_function` returns early on two paths — a name
    that is in no tool map, and arguments that fail schema validation — and both return *before*
    the function-middleware pipeline. So "the model asked for `find_notes` with arguments it could
    not satisfy" left no row at all. Authorization not running there is harmless, because nothing
    executed; the *audit* gap is not, for a trail whose stated purpose is to answer what the agent
    attempted.

    **Why a wrapper on the dispatcher rather than a check in the runner.** The runner sees the
    turn's messages afterwards and could spot a `function_result` carrying an exception — but it
    cannot tell one MAF composed from one a tool raised, and the second is already audited by the
    middleware. Double-recording every failed tool call to catch the refused ones is a worse trail
    than the gap. The dispatcher is the one place both facts are known.

    **Why the marker, and not a string match.** MAF's two refusal messages are recognisable
    (`"Error: Argument parsing failed."`), and matching them would break silently the day upstream
    rewords one or adds a third early return. `_REACHED_MIDDLEWARE` asks the question that actually
    matters — did anything audit this call — so a new refusal path is covered the day it appears
    rather than the day someone notices.

    It ends with being deleted, like every other upstream workaround in `DEFERRED.md`: if MAF ever
    runs its middleware for refused calls, this becomes a double-record and the marker says so.

    **It takes no sink, actor or correlation id, and that is a correction rather than a
    simplification.** The first version bound all three at install time, like the middleware does —
    and the patch is *process-global and permanent* while the middleware is per-agent, so the first
    `build_agent` to run would have owned every rejection recorded by every later agent in the
    process. Benign in production, where there is one sink; not benign as a design, and the tests
    that run beside another agent's build are what showed it. Everything is resolved per call
    instead: the ambient identity a turn stamps, and the one configured sink.

    Returns:
        A callable that restores MAF's original dispatcher — for tests, and for a process that
        wants the patch scoped rather than permanent.
    """
    from agent_framework import _tools as maf_tools

    revision = settings.deployment_revision
    original = maf_tools._auto_invoke_function  # noqa: SLF001 - the only dispatch choke point
    if getattr(original, "_chemclaw_rejection_audit", False):
        # Already installed. `build_agent` runs once per profile per process, so a second patch
        # over the first would wrap our own wrapper and record every refusal twice — and the
        # doubling would be invisible, because both rows would be correct.
        return lambda: None

    async def _dispatch(function_call_content: Any, *args: Any, **kwargs: Any) -> Any:
        """Dispatch through MAF, recording the call if nothing else did."""
        _REACHED_MIDDLEWARE.set(False)
        result = await original(function_call_content, *args, **kwargs)
        if _REACHED_MIDDLEWARE.get():
            return result  # the middleware recorded it, in full, with its latency
        name = getattr(function_call_content, "name", "") or "<unknown>"
        detail = str(getattr(result, "exception", "") or getattr(result, "result", ""))
        await _emit_shielded(
            default_audit_sink(),
            AuditEvent(
                correlation_id=get_current_correlation_id() or "",
                session_id=get_current_session_id() or "",
                actor=get_current_actor() or settings.service_actor_id,
                tool=name,
                # The *raw* arguments the model sent, which is the whole point: they are what was
                # rejected, so the validated form does not exist and recording nothing would leave
                # the trail unable to say what the attempt looked like.
                arguments=_truncate(_raw_arguments(function_call_content)),
                outcome=REJECTED_OUTCOME,
                detail=_truncate(detail),
                # No latency: nothing ran. Zero is the honest number, and it is distinguishable
                # from a fast tool by the outcome beside it.
                latency_ms=0.0,
                revision=revision,
            ),
        )
        logger.warning(
            "tool call refused before execution: %s (%s)",
            name,
            _truncate(detail),
        )
        return result

    _dispatch._chemclaw_rejection_audit = True  # type: ignore[attr-defined]
    maf_tools._auto_invoke_function = _dispatch  # noqa: SLF001 - see the docstring

    def _restore() -> None:
        """Put MAF's own dispatcher back."""
        maf_tools._auto_invoke_function = original  # noqa: SLF001 - see the docstring

    return _restore


def _raw_arguments(function_call_content: Any) -> dict[str, Any]:
    """The arguments as the model sent them, however MAF happens to carry them.

    Defensive because this reads a vendored content object on a path that only ever runs for an
    already-malformed call: a second failure here would replace a recorded refusal with an
    exception inside the audit trail, which is the one place that must not add failures.
    """
    try:
        parsed = function_call_content.parse_arguments()
    except Exception:  # noqa: BLE001 - the arguments are malformed by definition on this path
        return {"<unparseable>": str(getattr(function_call_content, "arguments", ""))[:200]}
    return dict(parsed or {})
