"""The per-turn run lifecycle (plan step F2-T1): the missing caller that actually runs the agent.

`run_turn` owns exactly what the agent's own docstring says a caller must own: it opens the MCP
tool connectors for the turn (`agent.mcp_tools`), runs the turn against the session's thread,
and translates the model's streamed updates into the typed `chemclaw.api.events` the surfaces
render.
When the harness is enabled the *same* call drives its completion loop (MAF's loop middleware
runs inside `agent.run`), so plan/execute autonomy needs no separate driver here.

Errors are turned into a single `ErrorEvent` with a user-safe message rather than propagating a
stack trace to the browser — a failed turn must not take down the stream or leak internals.

What is left here is the *lifecycle* — the exit stack, the state snapshot and its rollback, the
contextvars a turn stamps and must unstamp. The three pieces that are pure functions of what the
stream hands them live beside it, one module each, because they are the parts that can be tested by
passing an object in and comparing what comes back: `api/runner_trace.py` (reassembling a streamed
tool call and rendering an approval prompt), `api/runner_usage.py` (the turn's token arithmetic) and
`api/runner_answer.py` (scoring the final answer against this turn's tool outputs).
"""

import asyncio
import copy
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from typing import Any

from agent_framework import AgentSession

from chemclaw.agent.chemclaw_agent import connector_tools
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.harness_todo import apply_deferred_completions, todo_titles
from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.live_session import reset_current_session, set_current_session
from chemclaw.agent.loop_cap import begin_loop_watch, end_loop_watch, loop_hit_cap
from chemclaw.agent.plan_gate import consume_turn_approval, gate_applies
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.turn_cost import TurnCost, record_turn_cost
from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import (
    ApprovalRequestEvent,
    CapabilityDegradedEvent,
    ErrorCode,
    ErrorEvent,
    Event,
    JobStartedEvent,
    NoteProposedEvent,
    PlanEvent,
    QuestionEvent,
    TokenEvent,
    ToolFailedEvent,
)
from chemclaw.api.runner_answer import build_answer_event
from chemclaw.api.runner_trace import ToolCallTrace, approval_prompt
from chemclaw.api.runner_usage import TurnUsage, usage_tokens
from chemclaw.connectors.registry import open_reachable
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import (
    reset_current_session_id,
    set_current_session_id,
)
from chemclaw.core.temporal_client import connect
from chemclaw.core.tracing import start_span
from chemclaw.core.turn_signals import (
    ApprovalSignal,
    JobSignal,
    QuestionSignal,
    Signal,
    ToolFailureSignal,
    begin_turn,
    drain,
    end_turn,
)

logger = logging.getLogger(__name__)

# What the durable subsystem is called when its outage is announced. `CapabilityDegradedEvent`
# carries a list of *connector* names today, and this is not a connector — it is the whole durable
# execution layer, so every connector's jobs are down with it. It rides in the same list because
# what a surface does with the name is identical (say this capability is missing this turn), and a
# second event type for one more unreachable capability would be a contract change for no
# additional meaning. The name is prefixed so it cannot be mistaken for a bundle in the registry.
_DURABLE_SUBSYSTEM = "durable-jobs (Temporal)"


def _classify(error: BaseException) -> tuple[ErrorCode, bool]:
    """Map a turn failure onto a user-facing code and whether retrying could plausibly help.

    Deliberately a short, closed mapping rather than an exception hierarchy walk. Each arm answers
    "what should the person do now?", which is the only question the code exists to answer, and an
    unrecognised failure stays `internal` — admitting the classification is missing beats guessing
    a friendlier one.

    `ConnectionError` is what `chemclaw.core.db` raises for an unreachable or saturated database,
    and it is deliberately not a `ChemclawError` so Temporal retries it; the same reasoning makes
    it retryable here. `ChemclawError` is the bad-data contract — a malformed SMILES, an
    unbalanced equation — so retrying it unchanged cannot work, and saying so saves the user a
    wasted turn.
    """
    if isinstance(error, ConnectionError):
        return "storage_unavailable", True
    if isinstance(error, TimeoutError):
        return "llm_timeout", True
    if isinstance(error, ChemclawError):
        return "bad_tool_arguments", False
    return "internal", False


async def run_turn(
    agent: Any,
    session: AgentSession,
    user_message: str,
    *,
    actor: str | None = None,
    roles: frozenset[str] = frozenset(),
    budget: BudgetTracker | None = None,
    dry_run: bool = False,
    connectors: Sequence[Any] | None = None,
    history: Any | None = None,
    profile: str | None = None,
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
        connectors: This turn's connector tools. Defaults to every enabled connector
            (`chemclaw.agent.chemclaw_agent.connector_tools`); a caller that selected an agent
            profile
            passes that profile's narrowed set, and a test passes an empty list to run with
            none.
        budget: The runaway-cost meter. When set, this turn's reported token usage and its turn
            count are booked against the session/user when the turn ends (the front-door
            admission check reads those counters before the *next* turn). `None` disables
            metering (test/CLI).
        history: The session's history provider, when it stores durably. Only used to roll the
            turn's committed rows back on a client disconnect that lands *before* the turn
            answered — under the in-memory provider the state snapshot below is the whole story,
            but a durable one has already written them.
        profile: The session's agent profile, used only to label this turn's token spend
            (REV-10) — the surface it selects is chosen by the caller, which passes the matching
            agent and connectors. `None` labels the spend `default`, so every series carries the
            same label set and the family sums to the deployment's whole spend.

    Yields:
        `chemclaw.api.events.Event` values in the order the model produced them, ending with an
        `AnswerEvent` on success or an `ErrorEvent` on failure.
    """
    turn_started = time.perf_counter()
    # Whether this turn's approval is spendable, asked exactly as `build_agent` asks whether to
    # attach the gate — one predicate, so the two cannot disagree about a profile that overrides
    # the deployment's autonomy.
    plan_gated = gate_applies(get_profile(profile))
    # Whether this turn produced its answer — the cost ledger's question ("did the user get an
    # answer for the money"), and only that. It is deliberately *not* the rollback predicate:
    # `answered` becomes true only after the verifier and any mid-turn resume have run, windows in
    # which the exchange is already committed and paired, so gating the rollback on it deleted
    # finished turns whose teardown merely landed in one of those windows. `run_complete` below is
    # the rollback's predicate; the two questions have different right answers.
    answered = False
    # Whether the last `agent.run` returned. That is the fact the rollback cares about: the
    # rollback exists to discard a half-written exchange (an orphaned `tool_use` that would brick
    # the session), and once `agent.run` has returned the history provider has committed a
    # complete, paired exchange — there is nothing half-written left to undo, however much
    # bookkeeping (loop-cap reporting, job-result waits, answer verification) still lies between
    # here and the AnswerEvent. Cleared again for the mid-turn resume's second run, which can
    # half-write exactly like the first.
    run_complete = False
    answer_parts: list[str] = []
    # Metered across the turn's updates and booked once on teardown (even on failure — a failed
    # turn still spent tokens up to the point it broke, so its cost must count toward the next
    # check).
    turn_usage = TurnUsage()
    # Stamp the turn's session so a job-launching tool (compute_dft_energy) records push-back to the
    # right session (F3-T3) — ambient, never a model-supplied argument. Reset on turn teardown.
    session_token = set_current_session_id(session.session_id)
    # The live session object too, so a job-launching tool can mark the harness todo it's
    # waiting on (`agents.harness_todo`) — the id alone cannot reach the session's own todo-list
    # state.
    live_session_token = set_current_session(session)
    # Stamp the authenticated identity (F4) so audit/authorization/attribution see the user.
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    # One correlation id per *turn*, stamped here rather than bound inside `build_agent`: agents
    # are cached per profile for the process's lifetime, so a build-time id was shared by every
    # turn from every user on the pod — the audit trail could not tell two conversations apart,
    # which is the one thing a correlation id exists to do.
    correlation_id = uuid.uuid4().hex
    correlation_token = set_current_correlation_id(correlation_id)
    # Buffer for what tools learn mid-turn that the stream must surface (started jobs, PR-gate
    # proposals) — the runner only sees the model's updates, so tools hand these over out of
    # band.
    signals_token = begin_turn()
    # Watch the harness loop's stop decisions, so a turn stopped by the runaway cap can say so
    # instead of looking exactly like one that finished (`chemclaw.agent.loop_cap`). No-op for the
    # classic agent, which has no loop to watch.
    loop_token = begin_loop_watch()
    # Durable jobs this turn launched, for the optional mid-turn resume below.
    started_jobs: list[str] = []
    dry_run_token = set_dry_run(dry_run)
    # The harness's todo list as last rendered, so a plan is emitted when it first appears and
    # again whenever it changes — not once per update (which would spam an unchanged plan).
    last_plan: list[str] = []
    # Apply job completions the push-back stream recorded while no turn could safely take the
    # write (`chemclaw.agent.harness_todo.defer_job_completion`). Turn start is the one moment
    # nothing else writes `session.state`, and it must happen *before* the snapshot below: the
    # flip then belongs to the pre-turn state, so a disconnect that restores the snapshot keeps
    # it instead of silently un-completing the todo. Guarded because it is bookkeeping — a
    # failed flip must not cost the chemist the turn it precedes.
    if settings.harness_enabled:
        try:
            await apply_deferred_completions(session)
        except Exception:  # noqa: BLE001 - a todo flip must never fail the turn it precedes
            logger.exception(
                "could not apply deferred job completions for session %s", session.session_id
            )
    # Snapshot the session state before the turn so a client disconnect can roll it back
    # (ISSUE-B-10). A disconnect mid-tool-call otherwise leaves a `tool_use` block in the stored
    # history with no matching `tool_result`, and every later turn on that session replays it —
    # the model rejects the thread outright ("tool_use ids found without tool_result blocks"),
    # so one dropped connection permanently bricks the conversation rather than costing it a
    # turn.
    state_snapshot = copy.deepcopy(session.state)
    # The durable half of that snapshot. `session.state` is not where a Postgres-backed history
    # lives — `save_messages` has already committed its rows — so restoring the state alone left
    # the orphaned `tool_use` in the database and bricked the session anyway, which is exactly the
    # failure the snapshot exists to prevent. A watermark lets the rollback delete what this turn
    # actually wrote, and nothing else.
    #
    # The read stays non-fatal, and that is a decision rather than an omission. Failing the turn
    # would trade a *conditional* future fault — this session breaks only if the client also
    # disconnects mid-tool-call — for a *certain* immediate one: every turn on the pod fails
    # whenever the session store hiccups, including the turns that would have completed fine. The
    # guard is a mitigation, not the thing being guarded.
    #
    # What was wrong is that it was silent. A load test ran 32 turns unguarded in 126 seconds and
    # said so only in a WARNING nobody scrapes. It is now an ERROR *and* a counter, so "the
    # rollback guard is off" is alertable — which is the property that lets an operator act before
    # a chemist finds a bricked session. (The cause was connect churn; that is fixed by pooling in
    # the previous commit. This is the part that must not depend on having fixed the cause.)
    #
    # `watermark_read` is what "the guard is off" means mechanically. It used to be encoded as the
    # watermark staying `None`, which the store then treated as 0 — so the failure that was meant
    # to disarm the guard instead armed it with the maximally destructive value, and a disconnect
    # after a failed read deleted the session's *entire* history rather than this turn's rows. The
    # two meanings of `None` ("no history yet" and "could not read") are now separate: an empty
    # session reads as watermark 0 with the guard armed, and a failed read leaves the guard
    # disarmed so the teardown skips the durable delete entirely and leans on the next read's
    # `message_pairing` repair — the same fallback the rollback's own failure branch relies on.
    history_watermark = 0
    watermark_read = False
    if history is not None and hasattr(history, "latest_message_id"):
        try:
            watermark = await history.latest_message_id(session.session_id)
            history_watermark = 0 if watermark is None else int(watermark)
            watermark_read = True
        except Exception:  # noqa: BLE001 - a rollback aid must never fail the turn it guards
            METRICS.increment("chemclaw_rollback_watermark_unavailable_total")
            logger.error(
                "could not read the history watermark for session %s; this turn runs WITHOUT the "
                "durable-history rollback guard, so a client disconnect during it can leave an "
                "orphaned tool_use and brick the session",
                session.session_id,
                exc_info=True,
            )
    try:
        async with AsyncExitStack() as stack:
            # The turn span, which is the parent every other span in this request hangs from — the
            # model calls MAF emits, the tool spans `agent/audit.py` opens, and (through
            # `traceparent`) whatever a connector does on our behalf. Pushed onto the stack that is
            # already here rather than wrapping the body, so the span's lifetime is exactly the
            # turn's teardown and there is no second place that has to remember to close it.
            stack.enter_context(
                start_span(
                    "chemclaw.turn",
                    **{"session.id": session.session_id, "profile": profile or ""},
                )
            )
            # This turn's own connector tools, connected for its duration and torn down after.
            # Built per turn rather than held on the agent because a connector's connection must
            # belong to exactly one turn — see `agents.chemclaw_agent.connector_tools`. They are
            # passed to `agent.run`, which appends run-scoped tools to the agent's configured
            # ones, so the model sees one combined surface. An unreachable connector costs its
            # tools, not the turn.
            turn_connectors = connectors if connectors is not None else connector_tools()
            # Surfaced before the first token rather than discarded (REV-6): the model cannot tell
            # the chemist that a tool was missing, because it never saw one missing — it answers
            # from the surface it was handed. Only this layer knows the surface was short.
            unreachable = await open_reachable(stack, turn_connectors)
            # The durable subsystem is announced the same way and for the same reason. It was not,
            # and connectors were: Temporal was never probed, so a turn whose every durable
            # launcher was going to fail planned exactly like a turn that could run one. Measured
            # in the 190-probe live run: 0 of 7 durable launchers ran, and the model repeatedly
            # read the launch failure as bad input from the chemist and re-asked for parameters it
            # already had. Before the first token, so the model plans against the surface it will
            # actually get rather than discovering the outage by calling into it.
            if not await _durable_subsystem_reachable():
                unreachable = [*unreachable, _DURABLE_SUBSYSTEM]
            if unreachable:
                yield CapabilityDegradedEvent(connectors=unreachable)
            stream = agent.run(
                user_message, stream=True, session=session, tools=turn_connectors or None
            )
            tool_trace = ToolCallTrace()
            async for update in stream:
                turn_usage.add(usage_tokens(update))
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
                for call in tool_trace.feed(update):
                    yield call
                for request in getattr(update, "user_input_requests", None) or []:
                    yield ApprovalRequestEvent(prompt=approval_prompt(request))
                plan = await _current_plan(session)
                if plan and plan != last_plan:
                    last_plan = plan
                    yield PlanEvent(todos=plan)
            # The stream is exhausted, so `agent.run` has returned and the history provider has
            # committed this turn's rows as a complete, paired exchange. From here on a teardown
            # has nothing half-written to discard — set the fact the rollback gate reads at the
            # moment it becomes true, not at the answer, which is still a verifier call and
            # possibly a job-result wait away.
            run_complete = True
            # A signal recorded while producing the *final* update has no next iteration to
            # carry it, so drain once more before the answer — otherwise the last job started or
            # note proposed in a turn would be silently dropped. The same is true of a tool call
            # whose arguments finished on that update: nothing follows to close it out.
            for call in tool_trace.flush():
                yield call
            for signal in drain():
                if isinstance(signal, JobSignal):
                    started_jobs.append(signal.job_id)
                yield _signal_event(signal)

            # Mid-turn resume (gap AGT-2): if this turn launched durable jobs, optionally wait
            # for them and continue the *same* turn with their results, so "compute this, then
            # reason about the result" is one exchange rather than two. Off by default; bounded
            # by config and, above it, by the front door's whole-turn deadline.
            if started_jobs and settings.mid_turn_resume_enabled:
                results = await await_job_results(
                    session.session_id,
                    started_jobs,
                    timeout_seconds=settings.mid_turn_resume_timeout_seconds,
                )
                if results:
                    # The resume drives a *second* `agent.run`, which can half-write exactly like
                    # the first — so the exchange is incomplete again until it returns, and a
                    # teardown landing inside it must roll the turn back after all.
                    run_complete = False
                    async for event in _resume(
                        agent, session, results, turn_connectors, tool_trace, turn_usage
                    ):
                        if isinstance(event, TokenEvent):
                            answer_parts.append(event.text)
                        yield event
                    run_complete = True
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
        # The runaway guard fired: the harness loop still had work it wanted to do and its
        # iteration cap stopped it (`chemclaw.agent.loop_cap`). Said out loud, before the answer,
        # for the same reason `CapabilityDegradedEvent` precedes the tokens — the answer that
        # follows is whatever the last iteration managed, and a surface must be able to mark it
        # partial rather than present it as the finished work. The turn is not failed by this: the
        # answer still goes out, and the ledger still bills it as completed.
        if loop_hit_cap():
            METRICS.increment("chemclaw_turn_loop_caps_total")
            logger.warning(
                "the harness loop for session %s hit its %d-iteration cap with work still open",
                session.session_id,
                settings.harness_max_loop_iterations,
            )
            yield ErrorEvent(
                message=(
                    f"The turn reached its {settings.harness_max_loop_iterations}-iteration limit "
                    "and stopped with work still open, so the answer below is partial "
                    f"(session {session.session_id})."
                ),
                code="loop_cap_reached",
                # Not retryable unchanged: the same request drives the same loop into the same
                # cap. The useful next step is a narrower request, not another 25 iterations.
                retryable=False,
                correlation_id=correlation_id,
            )
        # A turn that produced no prose at all is a *silent* failure, and it must not be one.
        #
        # There is already a guard for the harness loop hitting its cap, but that path only runs
        # with `harness_enabled` — and the case measured on 2026-08-04 had the harness off: du-03
        # made 29 tool calls (find_past_jobs ×8, load_skill ×6, find_notes ×5, …), never reached the
        # capability the question needed, and ended with an empty `AnswerEvent` after 197 s. No
        # error, no tokens, nothing to read. `evals.live` scores exactly this as `failed_loudly=
        # False` because it is the worst shape a turn can take: a user cannot retry what never said
        # it went wrong, and every prior live pass has found one (`docs/archive/vibe-test-2026-07`).
        #
        # An `ErrorEvent` rather than inventing an answer: the system genuinely has nothing to say,
        # and saying so is the honest outcome. Retryable, unlike the loop cap — a turn that spent
        # its budget circling retrieval may well succeed on a narrower question, and the message
        # says so.
        text = "".join(answer_parts)
        if not text.strip():
            METRICS.increment("chemclaw_turn_empty_answers_total")
            logger.warning(
                "turn for session %s ended with no answer text after %d tool call(s)",
                session.session_id,
                len(tool_trace.called_tools),
            )
            yield ErrorEvent(
                message=(
                    "The turn ended without producing an answer, after "
                    f"{len(tool_trace.called_tools)} tool call(s). Nothing was written, so "
                    "there is nothing below to read — this is a failure, not an empty result. "
                    "A narrower or more specific question is the useful next step "
                    f"(session {session.session_id})."
                ),
                code="empty_answer",
                retryable=True,
                correlation_id=correlation_id,
            )
        answer = await build_answer_event(text, tool_trace.outputs, tool_trace.called_tools)
        # **Before the yield, not after it.** `agent.run` has ended by now, so the history provider
        # has already committed this turn's rows and they are a complete, paired exchange — there is
        # nothing half-written left to undo. The cancellation that reaches a finished turn is
        # delivered *while suspended in the yield below*, as sse-starlette sends the answer, so a
        # flag set after it is still false exactly when the teardown clause needs it to be true.
        answered = True
        yield answer
        # The turn used its authorization, so the authorization is spent (D-167). Here rather than
        # in `finally`, which also runs on the disconnect path where an `await` would re-raise the
        # cancellation and skip every teardown step after it — see `consume_turn_approval`.
        if plan_gated:
            await consume_turn_approval(session)
    except (GeneratorExit, asyncio.CancelledError):
        # The turn is being torn down from outside — the client went away, or the front door's
        # wall-clock deadline expired. Roll the session back to its pre-turn state: a half-written
        # turn is worth less than the conversation it would otherwise poison (see the snapshot
        # above). Needs its own clause because both derive from BaseException, not Exception;
        # re-raised so the generator still closes and a timeout still surfaces as one.
        #
        # **`CancelledError` belongs here and its absence made this clause dead code on the only
        # path that matters** (D-130). sse-starlette answers `http.disconnect` by cancelling its
        # task group; it never calls `aclose()` on the body iterator, so a real disconnect
        # delivers `CancelledError` and this rollback was skipped every single time. It looked
        # covered because the suite closed the stream by hand — the one thing production does
        # not do. Measured on a live front door: the agent's stream received `CancelledError`,
        # never `GeneratorExit`. The read-time repair in `agents.session_store` is why this was a
        # silent weakness rather than an outage; it strips the unmatched tool call on the next
        # read, but only the rollback discards the rest of the abandoned turn.
        #
        # **Only a turn whose exchange is incomplete is rolled back.** Once the last `agent.run`
        # returned, the history provider committed a complete user+assistant pair and no
        # `tool_use` is left without its result — the sole failure the rollback exists to
        # prevent. Undoing it anyway deleted a finished exchange from the conversation because
        # the client dropped during the send of its answer. In a GxP system a silently vanished
        # answer is worse than a lost turn. (The spent-plan marker used to ride along in that
        # snapshot, so reverting an answered turn's state re-armed the approval it had just used
        # as well; consumption is a durable column now — `plan_approvals.consumed_at` — so the
        # committed exchange alone is the reason, which is the reason that was always sufficient.)
        #
        # The predicate is `run_complete`, not `answered`, and the gap between them is real time:
        # after the stack closes the turn still awaits loop-cap reporting, an optional job-result
        # wait plus resume, and the verifier's judge call — and `answered` only becomes true after
        # all of them. A teardown landing in any of those windows used to take the rollback branch
        # and delete an exchange `agent.run` had already committed complete and correctly paired —
        # the exact outcome this comment says must not happen. `answered` is kept beside it for
        # the cost ledger, whose question genuinely is "did the user get an answer".
        if answered or run_complete:
            logger.warning(
                "turn for session %s was torn down after its exchange completed (client "
                "disconnect or the front door's turn deadline); the committed turn is kept",
                session.session_id,
            )
            raise
        logger.warning(
            "turn for session %s was torn down before it answered (client disconnect or the "
            "front door's turn deadline); rolling session state back",
            session.session_id,
        )
        session.state.clear()
        session.state.update(state_snapshot)
        if history is not None and hasattr(history, "rollback_to") and not watermark_read:
            # The guard was disarmed at the top of the turn (the ERROR + counter already said so):
            # without the pre-turn watermark there is no boundary this turn's rows can be told
            # apart at, and the placeholder 0 would delete the *whole* conversation — a disconnect
            # would cost the chemist every turn they ever had, not the one that broke. Skip the
            # durable delete and lean on the next read's `message_pairing` repair to drop any
            # orphaned tool call, exactly as `_roll_back`'s own failure branch below already does.
            logger.warning(
                "skipping the durable-history rollback for session %s: the pre-turn watermark "
                "was unreadable, so only the next read's repair can safely drop this turn's rows",
                session.session_id,
            )
        elif history is not None and hasattr(history, "rollback_to"):

            async def _roll_back() -> None:
                """Delete this turn's committed rows, reporting its own failure.

                Shielded by the caller, so this runs as a task that outlives the cancelled turn —
                a plain `await` here would be cancelled at its first suspension point and leave
                the half-written turn committed after all. It swallows its own errors for the same
                reason the durable claim release does (D-130): once the awaiting task is
                cancelled, `shield` stops collecting the inner result, so a failure raised here
                would surface only as an unattributed `Task exception was never retrieved`.
                """
                try:
                    deleted = await history.rollback_to(session.session_id, history_watermark)
                except Exception:  # noqa: BLE001 - a cleanup aid must not replace the teardown
                    logger.warning(
                        "could not roll durable history back for session %s; the next turn's "
                        "read-time repair will drop any unmatched tool call",
                        session.session_id,
                        exc_info=True,
                    )
                    return
                if deleted:
                    logger.warning(
                        "rolled %d durable message(s) back for session %s",
                        deleted,
                        session.session_id,
                    )

            try:
                await asyncio.shield(_roll_back())
            except BaseException:  # noqa: BLE001 - see below; nothing here may escape
                # `BaseException`, not `Exception`: the teardown that brought us here cancels this
                # task, so the shielded await raises `CancelledError` as soon as it suspends —
                # while the rollback itself carries on in its own task. Letting that out would
                # replace the teardown exception we are handling and leave the generator
                # improperly closed. The bare `raise` below re-raises the original either way.
                logger.debug(
                    "the rollback for session %s outlived its turn; it completes on its own task",
                    session.session_id,
                )
        raise
    except Exception as exc:
        # One turn's failure becomes one user-safe event, never a 500 mid-stream or a leaked
        # trace. The exception detail (DB hosts, SMILES, workflow ids, driver errors) stays
        # server-side in the log; the client gets a *classified* failure plus the correlation id
        # the audit trail is keyed on, so a bug report is findable without leaking internals.
        logger.exception("turn failed for session %s", session.session_id)
        code, retryable = _classify(exc)
        yield ErrorEvent(
            message=(
                "The turn could not be completed due to an internal error "
                f"(session {session.session_id})."
            ),
            code=code,
            retryable=retryable,
            correlation_id=correlation_id,
        )
        # A turn that spent the authorization and then broke has still spent it: tools may have
        # run before it failed, and re-running under the same approval is exactly what a person
        # would want asked about again.
        if plan_gated:
            await consume_turn_approval(session)
    finally:
        # **Nothing in this block may `await`.** It runs on the disconnect path too, which
        # production reaches by cancellation rather than `aclose()` (D-130) — an `await` here
        # re-raises the cancellation on the spot and silently skips everything below it, including
        # the five context-var resets, which would leak one turn's ambient identity into the next
        # turn on this worker.
        if budget is not None:
            budget.record(session.session_id, actor, turn_usage.total)
        # Observed on every path — success, failure and disconnect — because a turn that failed
        # after 40 s is exactly the sample an operator needs, and excluding it would make the
        # histogram look best when the service is worst. The token counter is the same number the
        # budget guard meters, published as a rate rather than only used to refuse.
        METRICS.observe("chemclaw_turn_duration_seconds", time.perf_counter() - turn_started)
        # Labelled by profile (REV-10): "what is this costing" is only actionable once it can be
        # attributed, and a narrowed profile is exactly the thing a deployment adopts to spend less.
        # `default` rather than an absent label for a session on no profile, so every series carries
        # the same label set and the sum over the family is the deployment's whole spend.
        spend_labels = {"profile": profile or "default"}
        # The same numbers, booked a second time against the identity the metric cannot carry. Not a
        # duplicate: `core/metrics` refuses a counter past 64 label series (D-152) because a label
        # value is attacker-influenced, and an Entra oid is exactly such a key — so per-actor spend
        # needs a table, and the fleet-wide rate needs a counter. Booked here rather than on the
        # success path so a turn torn down by a disconnect is billed too: that is the runaway this
        # ledger exists to find, not an edge case to drop. `record_turn_cost` does not await — see
        # the block comment above and its own docstring.
        record_turn_cost(
            TurnCost(
                correlation_id=correlation_id,
                session_id=session.session_id,
                actor=actor or "",
                profile=profile or "default",
                input_tokens=turn_usage.input,
                output_tokens=turn_usage.output,
                cache_read_tokens=turn_usage.cache_read,
                cache_write_tokens=turn_usage.cache_write,
                duration_seconds=time.perf_counter() - turn_started,
                completed=answered,
            )
        )
        if turn_usage.total:
            METRICS.increment("chemclaw_tokens_total", float(turn_usage.total), spend_labels)
        # Published separately from the total because they are priced separately (REV-10). Each is
        # guarded so a provider that reports none of them leaves its counter untouched rather than
        # publishing a fabricated zero — the same rule `core.metrics` applies to gauges.
        for name, value in (
            ("chemclaw_input_tokens_total", turn_usage.input),
            ("chemclaw_output_tokens_total", turn_usage.output),
            ("chemclaw_cache_read_tokens_total", turn_usage.cache_read),
            ("chemclaw_cache_write_tokens_total", turn_usage.cache_write),
        ):
            if value:
                METRICS.increment(name, float(value), spend_labels)
        end_turn(signals_token)
        end_loop_watch(loop_token)
        reset_dry_run(dry_run_token)
        reset_current_session_id(session_token)
        reset_current_session(live_session_token)
        reset_current_correlation_id(correlation_token)
        if identity_token is not None:
            reset_current_identity(identity_token)


async def _durable_subsystem_reachable() -> bool:
    """Is Temporal answering right now? — the per-turn probe behind the durable outage announcement.

    Announced rather than discovered: every long or expensive capability in the system is a
    workflow, so an unreachable broker removes all of them at once, and the only thing that knows
    it before the turn starts is this layer. Without the probe the model met the outage as a tool
    failure mid-answer and, in the live run, read it as its own bad input.

    `check_health` rather than `connect` alone, because `connect` caches this process's client for
    its lifetime (`core.temporal_client`): once one turn has connected, every later turn would get
    the cached handle back instantly and call a broker that has since died reachable. The health
    RPC is what actually goes to the wire each turn, and `retry=False` keeps it a *probe* — the
    SDK's default retry would turn one unreachable broker into a per-turn backoff loop.

    Bounded by `connector_health_timeout_seconds`, the same budget the connector sweep uses: this
    is the same kind of thing on the same hot path — a reachability check whose cost is paid by
    every turn — and one probe budget that both honour is easier to reason about (and to raise on a
    slow network) than two knobs that can disagree. A hang here would otherwise delay every turn's
    first token by however long the broker takes to not answer.

    Never raises: a probe that fails the turn it was meant to describe is worse than no probe, so
    every failure means "not reachable" and the turn proceeds with the outage announced.
    """
    try:
        client = await asyncio.wait_for(connect(), settings.connector_health_timeout_seconds)
        return await asyncio.wait_for(
            client.service_client.check_health(retry=False),
            settings.connector_health_timeout_seconds,
        )
    except Exception:
        # DEBUG, not WARNING: `open_reachable` already logs and counts a degraded turn, and an
        # outage this probe finds is reported to the chemist on the stream — logging it at
        # attention level once per turn would bury the connector sweep's own signal under it.
        logger.debug("the durable subsystem did not answer its health probe", exc_info=True)
        return False


async def _current_plan(session: AgentSession) -> list[str] | None:
    """The harness's current todo list for this session, or None when there is no plan to show.

    Why this is emitted at all (gap RCH-5): `PlanEvent` has been in the typed contract and
    rendered by the UI since F2, but nothing ever produced one — so `plan_only` autonomy, which
    the Helm chart ships as the production default, asked a human to approve a plan the surface
    could never show them. Titles are read from the harness's own `TodoProvider` state, the same
    store `chemclaw.agent.harness_todo` mutates, so there is no second representation of the plan to
    drift.

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


async def _resume(
    agent: Any,
    session: Any,
    results: dict[str, dict[str, Any]],
    connectors: Sequence[Any],
    tool_trace: ToolCallTrace,
    turn_usage: TurnUsage,
) -> AsyncIterator[Event]:
    """Continue the turn with completed job results, streaming the continuation's events.

    The results are handed to the model as *framed data*, not as an instruction: they arrive
    from a workflow, and the same injection discipline that applies to retrieved notes applies
    here (`chemclaw.agent.framing`). Anything the continuation itself starts is surfaced too, but a
    resume is deliberately not recursive — a second wait would let one chemist turn chain
    durable jobs indefinitely inside a single request.

    The turn's connectors are passed through rather than rebuilt: the resume is part of the same
    turn, inside the same open connections, so a second set would open a second connection per
    connector
    for
    no reason.

    The turn's `tool_trace` is passed through for the same reason and one more: a tool the resume
    calls is part of this turn's evidence, so its result has to reach the answer verifier along
    with the rest. A trace of its own would have collected them into an object nobody reads.

    `turn_usage` is passed for the reason that matters most and was missed until the 2026-08-05
    review: this is a *second* `agent.run`, so it spends real tokens, and until now not one of them
    reached the budget guard, `chemclaw_tokens_total` or the `TurnCost` row. Measured on a turn
    that spent 1,000 tokens before the wait and 5,000 after it, the ledger booked 1,000 — 83 % of
    the turn unmetered. The one feature that adds an unbounded second model call was the one
    feature the runaway-cost refusal (D-144) could not see.
    """
    summary = "\n".join(f"- {job_id}: {payload}" for job_id, payload in results.items())
    message = (
        "The durable job(s) you started have completed. Their results follow as data; continue "
        "your answer using them.\n" + frame_untrusted(summary, note_id="job-results")
    )
    async for update in agent.run(message, stream=True, session=session, tools=connectors or None):
        turn_usage.add(usage_tokens(update))
        for signal in drain():
            yield _signal_event(signal)
        text = getattr(update, "text", "") or ""
        if text:
            yield TokenEvent(text=text)
        for call in tool_trace.feed(update):
            yield call
    for call in tool_trace.flush():
        yield call


def _signal_event(signal: Signal) -> Event:
    """Map one out-of-band turn signal to its stream event (one place, so the two cannot drift)."""
    if isinstance(signal, JobSignal):
        return JobStartedEvent(job_id=signal.job_id, kind=signal.kind)
    if isinstance(signal, QuestionSignal):
        return QuestionEvent(question=signal.question, options=signal.options)
    if isinstance(signal, ApprovalSignal):
        # Carries the durable hold's handle, so a surface can answer it via
        # POST /approvals/{id}/decision. The `user_input_requests` path in the turn loop emits the
        # *other* kind of approval — a plan prompt, which has no hold and is answered by the next
        # turn — and deliberately leaves `approval_id` empty to mark that difference.
        return ApprovalRequestEvent(prompt=signal.prompt, approval_id=signal.approval_id)
    if isinstance(signal, ToolFailureSignal):
        return ToolFailedEvent(tool=signal.tool, message=signal.message)
    return NoteProposedEvent(note_id=signal.note_id, reference=signal.reference)
