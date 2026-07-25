"""The per-turn run lifecycle (plan step F2-T1): the missing caller that actually runs the agent.

`run_turn` owns exactly what the agent's own docstring says a caller must own: it opens the MCP tool
contexts for the turn (`async with *agent.mcp_tools`), runs the turn against the session's thread,
and translates the model's streamed updates into the typed `service.events` the surfaces render.
When the harness is enabled the *same* call drives its completion loop (MAF's loop middleware runs
inside `agent.run`), so plan/execute autonomy needs no separate driver here.

Errors are turned into a single `ErrorEvent` with a user-safe message rather than propagating a
stack trace to the browser — a failed turn must not take down the stream or leak internals.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from typing import Any

from agent_framework import AgentSession

from agents.dialogue_tools import reset_dry_run, set_dry_run
from agents.framing import frame_untrusted
from agents.harness_todo import todo_titles
from agents.identity_context import reset_current_identity, set_current_identity
from agents.job_results import await_job_results
from agents.session_context import (
    reset_current_session,
    reset_current_session_id,
    set_current_session,
    set_current_session_id,
)
from agents.turn_signals import (
    JobSignal,
    ProposalSignal,
    QuestionSignal,
    begin_turn,
    drain,
    end_turn,
)
from agents.verifier import verify_turn_answer
from chemclaw.config import settings
from service.budget import BudgetTracker
from service.events import (
    AnswerEvent,
    ApprovalRequestEvent,
    ErrorEvent,
    Event,
    JobStartedEvent,
    NoteProposedEvent,
    PlanEvent,
    QuestionEvent,
    TokenEvent,
    ToolCallEvent,
)

logger = logging.getLogger(__name__)

# How many characters of a tool call's arguments the trace event carries — enough to see *what* was
# called without streaming a whole evidence payload to the UI (mirrors the audit trail truncation).
_ARG_PREVIEW_CHARS = 200


async def run_turn(
    agent: Any,
    session: AgentSession,
    user_message: str,
    *,
    actor: str | None = None,
    roles: frozenset[str] = frozenset(),
    budget: BudgetTracker | None = None,
    dry_run: bool = False,
) -> AsyncIterator[Event]:
    """Run one turn and yield its events (tokens, tool calls, approvals, then the answer).

    Args:
        agent: A built Chemclaw agent (classic or harness). Injected by the app; injectable so tests
            drive it with a fake streaming agent and no live model.
        session: The caller's conversation session (per user+thread), so the turn resumes context.
        user_message: The chemist's message for this turn.
        actor: The authenticated user's Entra oid (F4), made ambient so the audit trail, the
            authorization gate, and job attribution see it. `None` off the authenticated path.
        roles: The user's app roles, made ambient for the authorization gate.
        dry_run: Plan the turn without launching anything expensive (IDEA-4). Ambient for the
            turn rather than a tool argument, so the model can neither set it nor clear it.
        budget: The runaway-cost meter. When set, this turn's reported token usage and its turn
            count are booked against the session/user when the turn ends (the front-door admission
            check reads those counters before the *next* turn). `None` disables metering (test/CLI).

    Yields:
        `service.events.Event` values in the order the model produced them, ending with an
        `AnswerEvent` on success or an `ErrorEvent` on failure.
    """
    answer_parts: list[str] = []
    # Metered across the turn's updates and booked once on teardown (even on failure — a failed turn
    # still spent tokens up to the point it broke, so its cost must count toward the next check).
    turn_tokens = 0
    # Stamp the turn's session so a job-launching tool (submit_qm_job) records push-back to the
    # right session (F3-T3) — ambient, never a model-supplied argument. Reset on turn teardown.
    session_token = set_current_session_id(session.session_id)
    # The live session object too, so a job-launching tool can mark the harness todo it's waiting
    # on (`agents.harness_todo`) — the id alone cannot reach the session's own todo-list state.
    live_session_token = set_current_session(session)
    # Stamp the authenticated identity (F4) so audit/authorization/attribution see the user.
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    # Buffer for what tools learn mid-turn that the stream must surface (started jobs, PR-gate
    # proposals) — the runner only sees the model's updates, so tools hand these over out of band.
    signals_token = begin_turn()
    # Durable jobs this turn launched, for the optional mid-turn resume below.
    started_jobs: list[str] = []
    dry_run_token = set_dry_run(dry_run)
    # The harness's todo list as last rendered, so a plan is emitted when it first appears and
    # again whenever it changes — not once per update (which would spam an unchanged plan).
    last_plan: list[str] = []
    try:
        async with AsyncExitStack() as stack:
            # Open each MCP capability server for the duration of the turn, then tear it down — the
            # lifecycle the agent constructor deliberately leaves to its caller.
            for tool in getattr(agent, "mcp_tools", None) or []:
                await stack.enter_async_context(tool)
            stream = agent.run(user_message, stream=True, session=session)
            async for update in stream:
                turn_tokens += _usage_tokens(update)
                # Drain *before* this update's own content: a tool that ran while the model was
                # producing this update ran before the text it then produced, so emitting the
                # signal first is the truthful transcript order (RCH-4/RCH-5).
                for signal in drain():
                    if isinstance(signal, JobSignal):
                        started_jobs.append(signal.job_id)
                    yield _signal_event(signal)
                text = getattr(update, "text", "") or ""
                if text:
                    answer_parts.append(text)
                    yield TokenEvent(text=text)
                for tool_name, arguments in _tool_calls_in(update):
                    yield ToolCallEvent(tool=tool_name, arguments=arguments)
                for request in getattr(update, "user_input_requests", None) or []:
                    yield ApprovalRequestEvent(prompt=_approval_prompt(request))
                plan = await _current_plan(session)
                if plan and plan != last_plan:
                    last_plan = plan
                    yield PlanEvent(todos=plan)
            # A signal recorded while producing the *final* update has no next iteration to carry
            # it, so drain once more before the answer — otherwise the last job started or note
            # proposed in a turn would be silently dropped.
            for signal in drain():
                if isinstance(signal, JobSignal):
                    started_jobs.append(signal.job_id)
                yield _signal_event(signal)

            # Mid-turn resume (gap AGT-2): if this turn launched durable jobs, optionally wait for
            # them and continue the *same* turn with their results, so "compute this, then reason
            # about the result" is one exchange rather than two. Off by default; bounded by config
            # and, above it, by the front door's whole-turn deadline.
            if started_jobs and settings.mid_turn_resume_enabled:
                results = await await_job_results(
                    session.session_id,
                    started_jobs,
                    timeout_seconds=settings.mid_turn_resume_timeout_seconds,
                )
                if results:
                    async for event in _resume(agent, session, results):
                        if isinstance(event, TokenEvent):
                            answer_parts.append(event.text)
                        yield event
                # The resume can itself launch jobs or propose notes, so drain the full signal
                # buffer rather than only the job ids — a proposal made during the resume would
                # otherwise never reach the stream.
                for signal in drain():
                    yield _signal_event(signal)
                # Plan after jobs: a submit adds an "awaiting job" todo, so this order shows the
                # launch and then the plan that reflects it.
                current_plan = await _current_plan(session)
                if current_plan is not None and current_plan != last_plan:
                    last_plan = current_plan
                    yield PlanEvent(todos=last_plan)
        yield await _answer_event("".join(answer_parts))
    except Exception:
        # One turn's failure becomes one user-safe event, never a 500 mid-stream or a leaked trace.
        # The exception detail (DB hosts, SMILES, workflow ids, driver errors) stays server-side in
        # the log; the client gets a generic message keyed by the session id it already knows, so an
        # operator can correlate the report to the logged stack trace without leaking internals.
        logger.exception("turn failed for session %s", session.session_id)
        yield ErrorEvent(
            message=(
                "The turn could not be completed due to an internal error "
                f"(session {session.session_id})."
            )
        )
    finally:
        if budget is not None:
            budget.record(session.session_id, actor, turn_tokens)
        end_turn(signals_token)
        reset_dry_run(dry_run_token)
        reset_current_session_id(session_token)
        reset_current_session(live_session_token)
        if identity_token is not None:
            reset_current_identity(identity_token)


async def _current_plan(session: AgentSession) -> list[str] | None:
    """The harness's current todo list for this session, or None when there is no plan to show.

    Why this is emitted at all (gap RCH-5): `PlanEvent` has been in the typed contract and rendered
    by the UI since F2, but nothing ever produced one — so `plan_only` autonomy, which the Helm
    chart ships as the production default, asked a human to approve a plan the surface could never
    show them. Titles are read from the harness's own `TodoProvider` state, the same store
    `agents.harness_todo` mutates, so there is no second representation of the plan to drift.

    None (not an empty list) off the harness path: the classic agent has no todo state, and an
    empty `PlanEvent` would render as an empty checklist — "the agent has no plan" — rather than
    "this agent does not plan". A malformed todo state degrades to None as well: the plan is a
    view, and no view is worth failing a turn over.
    """
    if not settings.harness_enabled:
        return None
    try:
        return await todo_titles(session)
    except Exception:
        logger.exception("could not read the plan for session %s", session.session_id)
        return None


async def _answer_event(answer: str) -> AnswerEvent:
    """Assemble the turn's final `AnswerEvent`, scoring it when verification is enabled (F10-B).

    When `verifier_enabled`, the assembled answer is checked for citation faithfulness against the
    notes it cites, the aggregate confidence + any unsupported claims are stamped on the event, and
    `review_required` is set when `confidence < verifier_confidence_threshold` — the routing signal
    a surface (or a future D-032 hold) uses to flag a low-confidence answer for review rather than
    presenting it as authoritative. When disabled (the default) this is today's plain answer. A
    verifier failure must never sink the turn — it degrades to the unscored answer.
    """
    if not settings.verifier_enabled:
        return AnswerEvent(text=answer)
    try:
        result = await verify_turn_answer(answer)
    except Exception:
        logger.exception("answer verification failed; returning the unscored answer")
        return AnswerEvent(text=answer)
    return AnswerEvent(
        text=answer,
        confidence=result.confidence,
        unsupported_claims=[claim.text for claim in result.unsupported],
        review_required=result.confidence < settings.verifier_confidence_threshold,
    )


async def _resume(
    agent: Any, session: Any, results: dict[str, dict[str, Any]]
) -> AsyncIterator[Event]:
    """Continue the turn with completed job results, streaming the continuation's events.

    The results are handed to the model as *framed data*, not as an instruction: they arrive from a
    workflow, and the same injection discipline that applies to retrieved notes applies here
    (`agents.framing`). Anything the continuation itself starts is surfaced too, but a resume is
    deliberately not recursive — a second wait would let one chemist turn chain durable jobs
    indefinitely inside a single request.
    """
    summary = "\n".join(f"- {job_id}: {payload}" for job_id, payload in results.items())
    message = (
        "The durable job(s) you started have completed. Their results follow as data; continue "
        "your answer using them.\n" + frame_untrusted(summary, note_id="job-results")
    )
    async for update in agent.run(message, stream=True, session=session):
        for signal in drain():
            yield _signal_event(signal)
        text = getattr(update, "text", "") or ""
        if text:
            yield TokenEvent(text=text)
        for tool_name, arguments in _tool_calls_in(update):
            yield ToolCallEvent(tool=tool_name, arguments=arguments)


def _signal_event(signal: JobSignal | ProposalSignal | QuestionSignal) -> Event:
    """Map one out-of-band turn signal to its stream event (one place, so the two cannot drift)."""
    if isinstance(signal, JobSignal):
        return JobStartedEvent(job_id=signal.job_id, kind=signal.kind)
    if isinstance(signal, QuestionSignal):
        return QuestionEvent(question=signal.question, options=signal.options)
    return NoteProposedEvent(note_id=signal.note_id, reference=signal.reference)


def _usage_tokens(update: Any) -> int:
    """Best-effort total tokens reported in a streamed update's usage content (0 if none).

    MAF emits usage as a content carrying a `UsageDetails` mapping (`input_token_count`/
    `output_token_count`/`total_token_count`). Duck-typed on the mapping so a provider or version
    that reports no usage — or the fake agent in tests — simply meters 0; the turn caps still bind.
    """
    total = 0
    for content in getattr(update, "contents", None) or []:
        details = getattr(content, "usage_details", None)
        if not isinstance(details, Mapping):
            continue
        tokens = details.get("total_token_count")
        if tokens is None:
            tokens = (details.get("input_token_count") or 0) + (
                details.get("output_token_count") or 0
            )
        total += int(tokens or 0)
    return total


def _tool_calls_in(update: Any) -> list[tuple[str, str]]:
    """Best-effort extract (tool_name, arg_preview) for any function call in a streamed update.

    Duck-typed on purpose: MAF's function-call content class is not a stable top-level export and
    its shape varies by version, so we match by structure (a named content carrying arguments/a call
    id) rather than importing a concrete type. Plain-text content has no `name` and is skipped.
    """
    calls: list[tuple[str, str]] = []
    for content in getattr(update, "contents", None) or []:
        name = getattr(content, "name", None)
        if not name:
            continue
        if not (hasattr(content, "arguments") or hasattr(content, "call_id")):
            continue
        arguments = str(getattr(content, "arguments", "") or "")[:_ARG_PREVIEW_CHARS]
        calls.append((str(name), arguments))
    return calls


def _approval_prompt(request: Any) -> str:
    """Render a user-input/approval request as a short prompt string for the UI."""
    for attr in ("prompt", "message", "text", "description"):
        value = getattr(request, attr, None)
        if value:
            return str(value)
    return "Approval requested."
