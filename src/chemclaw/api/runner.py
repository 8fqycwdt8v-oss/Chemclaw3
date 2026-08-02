"""The per-turn run lifecycle (plan step F2-T1): the missing caller that actually runs the agent.

`run_turn` owns exactly what the agent's own docstring says a caller must own: it opens the MCP
tool connectors for the turn (`agent.mcp_tools`), runs the turn against the session's thread,
and translates the model's streamed updates into the typed `chemclaw.api.events` the surfaces
render.
When the harness is enabled the *same* call drives its completion loop (MAF's loop middleware
runs inside `agent.run`), so plan/execute autonomy needs no separate driver here.

Errors are turned into a single `ErrorEvent` with a user-safe message rather than propagating a
stack trace to the browser — a failed turn must not take down the stream or leak internals.
"""

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from agent_framework import AgentSession

from chemclaw.agent.chemclaw_agent import connector_tools
from chemclaw.agent.dialogue_tools import reset_dry_run, set_dry_run
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.harness_todo import todo_titles
from chemclaw.agent.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.loop_cap import begin_loop_watch, end_loop_watch, loop_hit_cap
from chemclaw.agent.plan_gate import consume_turn_approval, gate_applies
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.session_context import (
    reset_current_session,
    reset_current_session_id,
    set_current_session,
    set_current_session_id,
)
from chemclaw.agent.turn_cost import TurnCost, record_turn_cost
from chemclaw.agent.turn_signals import (
    ApprovalSignal,
    JobSignal,
    QuestionSignal,
    Signal,
    ToolFailureSignal,
    begin_turn,
    drain,
    end_turn,
)
from chemclaw.agent.verifier import ungrounded_parameter_shapes, verify_turn_answer
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import (
    AnswerEvent,
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
    ToolCallEvent,
    ToolFailedEvent,
    ToolResultEvent,
)
from chemclaw.api.metrics import METRICS
from chemclaw.connectors.registry import open_reachable
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.temporal_client import connect
from chemclaw.core.tracing import start_span

logger = logging.getLogger(__name__)

# How many characters of a tool call's arguments the trace event carries — enough to see *what*
# was called without streaming a whole evidence payload to the UI (mirrors the audit trail
# truncation).
_ARG_PREVIEW_CHARS = 200

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
    # Whether this turn produced its answer, which is the line between a turn that finished and one
    # a disconnect or the wall-clock deadline cut short. Both are billed (the cost ledger reads it);
    # only one of them got anything for the money, and only the other one has anything to roll back.
    answered = False
    answer_parts: list[str] = []
    # Metered across the turn's updates and booked once on teardown (even on failure — a failed
    # turn still spent tokens up to the point it broke, so its cost must count toward the next
    # check).
    turn_usage = _TurnUsage()
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
    history_watermark: int | None = None
    if history is not None and hasattr(history, "latest_message_id"):
        try:
            history_watermark = await history.latest_message_id(session.session_id)
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
            tool_trace = _ToolCallTrace()
            async for update in stream:
                turn_usage.add(_usage_tokens(update))
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
                    yield ApprovalRequestEvent(prompt=_approval_prompt(request))
                plan = await _current_plan(session)
                if plan and plan != last_plan:
                    last_plan = plan
                    yield PlanEvent(todos=plan)
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
                    async for event in _resume(
                        agent, session, results, turn_connectors, tool_trace
                    ):
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
        answer = await _answer_event("".join(answer_parts), tool_trace.outputs)
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
        # **Only an *unanswered* turn is rolled back.** A turn torn down after its answer has
        # nothing half-written about it: `agent.run` returned, the history provider committed a
        # complete user+assistant pair, and no `tool_use` is left without its result — the sole
        # failure the rollback exists to prevent. Undoing it anyway deleted a finished exchange
        # from the conversation because the client dropped during the send of its answer, a window
        # of one send plus one round trip that the cost ledger simultaneously billed as
        # `completed`. In a GxP system a silently vanished answer is worse than a lost turn. (The
        # spent-plan marker used to ride along in that snapshot, so reverting an answered turn's
        # state re-armed the approval it had just used as well; consumption is a durable column now
        # — `plan_approvals.consumed_at` — so the answer alone is the reason, which is the reason
        # that was always sufficient.)
        if answered:
            logger.warning(
                "turn for session %s was torn down after it answered (client disconnect or the "
                "front door's turn deadline); the completed turn is kept",
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
        if history is not None and hasattr(history, "rollback_to"):

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
        # duplicate: `api/metrics` refuses a counter past 64 label series (D-152) because a label
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
        # publishing a fabricated zero — the same rule `api.metrics` applies to gauges.
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


async def _answer_event(answer: str, tool_outputs: Sequence[str]) -> AnswerEvent:
    """Assemble the turn's final `AnswerEvent`, scoring it against what this turn retrieved (F10-B).

    Two independent checks, each behind its own knob and each able to set `review_required` — the
    one routing signal a surface (or a future D-032 hold) reads to flag an answer rather than
    present it as authoritative. There is deliberately no second flag: a reviewer needs to know
    *that* an answer wants a look, and `unsupported_claims` carries *why*, whichever check spoke.

    - `verifier_enabled`: citation faithfulness against `tool_outputs`, stamping the aggregate
      confidence and flagging below `verifier_confidence_threshold`.
    - `answer_shape_gate_enabled`: the deterministic scan for method parameters no tool in this
      turn produced. It flags outright rather than moving a confidence, because it is not a measure
      of anything — it either found an ungrounded shape or it did not. Off by default and a
      deployment decision rather than a default-on behaviour, because it is a shape heuristic that
      both misses and over-fires (see `ungrounded_parameter_shapes`), and an answer marked for
      review that did not need it costs a chemist trust in every mark that follows.

    `tool_outputs` is what the turn's tools actually returned, untruncated, and it is the whole
    point of both checks. Verification used to re-resolve the answer's citations from the graph on
    disk, which asked whether a cited note exists rather than whether this turn saw it.

    Neither check may sink the turn: a verifier failure degrades to the unscored answer.
    """
    confidence: float | None = None
    unsupported: list[str] = []
    review = False
    if settings.verifier_enabled:
        try:
            result = await verify_turn_answer(answer, tool_outputs)
        except Exception:
            logger.exception("answer verification failed; returning the unscored answer")
        else:
            confidence = result.confidence
            unsupported = [claim.text for claim in result.unsupported]
            review = result.confidence < settings.verifier_confidence_threshold
    if settings.answer_shape_gate_enabled:
        shapes = ungrounded_parameter_shapes(answer, tool_outputs)
        if shapes:
            # WARNING because this is the signal an operator tunes the gate on — how often it
            # fires, and on what — and the matched text is in the message so a false positive is
            # diagnosable without reading the transcript. A counter would be the better home for
            # the rate, but `api/metrics` refuses an undeclared name and declaring one is a
            # cross-package edit this change does not own.
            logger.warning(
                "answer marked for review: parameter shape(s) no tool in this turn produced (%s)",
                "; ".join(shapes),
            )
            unsupported = [*unsupported, *shapes]
            review = True
    return AnswerEvent(
        text=answer,
        confidence=confidence,
        unsupported_claims=unsupported,
        review_required=review,
    )


async def _resume(
    agent: Any,
    session: Any,
    results: dict[str, dict[str, Any]],
    connectors: Sequence[Any],
    tool_trace: "_ToolCallTrace",
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
    """
    summary = "\n".join(f"- {job_id}: {payload}" for job_id, payload in results.items())
    message = (
        "The durable job(s) you started have completed. Their results follow as data; continue "
        "your answer using them.\n" + frame_untrusted(summary, note_id="job-results")
    )
    async for update in agent.run(message, stream=True, session=session, tools=connectors or None):
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


@dataclass(slots=True)
class _TurnUsage:
    """One turn's model usage, split along the dimensions it is *priced* along (REV-10).

    The runner used to accumulate a single int, and `chemclaw_tokens_total` published it. That
    number cannot answer "what is this deployment costing", which is the question AG-11 asks:
    input, output and cache-read carry different prices — a cache read is roughly an order of
    magnitude cheaper than a fresh input token — so a deployment that caches well and one that does
    not report identical totals while their bills differ several-fold.

    MAF has reported all four since the beginning (`UsageDetails` carries
    `cache_read_input_token_count` and `cache_creation_input_token_count` beside the input/output
    pair). Nothing read past the sum.

    `total` stays the sum the budget guard meters, so the runaway-cost refusal is unchanged: this
    splits what is *published*, not what is enforced.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0

    def add(self, other: "_TurnUsage") -> None:
        """Accumulate another update's usage into this turn's running total."""
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.total += other.total


def _usage_tokens(update: Any) -> _TurnUsage:
    """Best-effort usage reported in a streamed update's usage content (all zero if none).

    MAF emits usage as a content carrying a `UsageDetails` mapping. Duck-typed on the mapping so a
    provider or version that reports no usage — or the fake agent in tests — simply meters 0; the
    turn caps still bind.

    `total` falls back to input+output when the provider omits it, exactly as before. The cache
    counts are read separately rather than folded in, because a provider that reports them has
    already excluded cache reads from `input_token_count` — adding them would double-count the
    cheap tokens as expensive ones.
    """
    usage = _TurnUsage()
    for content in getattr(update, "contents", None) or []:
        details = getattr(content, "usage_details", None)
        if not isinstance(details, Mapping):
            continue
        tokens = details.get("total_token_count")
        if tokens is None:
            tokens = (details.get("input_token_count") or 0) + (
                details.get("output_token_count") or 0
            )
        usage.add(
            _TurnUsage(
                input=int(details.get("input_token_count") or 0),
                output=int(details.get("output_token_count") or 0),
                cache_read=int(details.get("cache_read_input_token_count") or 0),
                cache_write=int(details.get("cache_creation_input_token_count") or 0),
                total=int(tokens or 0),
            )
        )
    return usage


class _ToolCallTrace:
    """Reassemble a streamed function call, so `tool_call` can carry the arguments it promises.

    A streamed call does not arrive as one object. The provider sends the *name* first, on a
    content whose `arguments` is still empty, and then streams the argument JSON as fragments on
    further contents that carry only the `call_id` — no name. Reading name-and-arguments off a
    single content, as this did, therefore matched exactly the one content that never has any
    arguments, and skipped every fragment for want of a name: `ToolCallEvent.arguments` was empty
    on every call ever emitted, and could not have been anything else (D-138). The field is
    documented as "a short argument preview" and read by the UI trace, so this was a promise the
    stream never kept.

    Fragments for one call arrive contiguously, so a call is complete once an update goes by
    without adding to it — that is the flush condition, and it needs no knowledge of which
    content type terminates a call. The event therefore lands slightly later than before: after
    the arguments rather than after the name. That is the more truthful order anyway, because a
    tool cannot run before its arguments are complete.

    **That flush condition was still one step too late** (D-159). For a streamed call the next
    update to arrive is the one carrying the call's *result* — the provider finishes the argument
    stream, the framework runs the tool, and only then does anything else come down the wire. So
    "an update went by without adding to it" fired after execution, and the trace announced
    `predict_pka(...)` once the twenty seconds were already spent. From the chemist's side a
    working calculation and a hung server were the same thing.

    So a call now completes the moment its accumulated arguments **parse as JSON**, which is
    exactly when the provider has finished sending them and before the tool is invoked. No new
    provider signal is needed and D-138's promise is kept: the event still carries the whole
    argument preview, it just no longer waits for the result to prove the arguments ended. The
    update-went-by rule stays as the fallback for arguments that never parse (a provider that
    streams something other than JSON) so nothing can be stranded.

    Results are matched back by `call_id` and emitted as `ToolResultEvent`, which is why
    `_names` outlives the flush: the name is what the result event reports, and it is not on the
    result content.

    Still duck-typed: MAF's function-call content class is not a stable top-level export and its
    shape varies by version, so this matches on structure (a `call_id`/`arguments` pair) rather
    than importing a concrete type.
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._fragments: dict[str, list[str]] = {}
        # The name of every call already announced, kept so its result can be reported under the
        # same name. Bounded by the calls in one turn, which the loop cap already bounds.
        self._issued: dict[str, str] = {}
        # What this turn's tools returned, in full, in the order they came back — the evidence the
        # answer verifier and the parameter-shape gate check the answer against. Kept here rather
        # than read back off the emitted `ToolResultEvent`s because those carry a 200-character
        # *preview*: a `gather_evidence` result is ~20,000 characters over 40 chunks, so scoring
        # against the preview would call 39 of its 40 citations fabricated. The budget is right for
        # the UI and wrong for a grounding check, so the two read different things from one place.
        # Bounded by the calls in one turn, like `_issued`, and never leaves the process.
        self.outputs: list[str] = []

    def feed(self, update: Any) -> list[Event]:
        """Take one streamed update; return the calls it issued and the results it returned."""
        growing: set[str] = set()
        done: set[str] = set()
        results: list[Event] = []
        for content in getattr(update, "contents", None) or []:
            if not (hasattr(content, "arguments") or hasattr(content, "call_id")):
                continue
            name = str(getattr(content, "name", "") or "")
            key = str(getattr(content, "call_id", "") or "") or name
            if not key:
                continue
            if name:
                self._names.setdefault(key, name)
            if key not in self._names and key not in self._issued:
                continue  # a fragment for a call whose opening content we never saw
            arguments = getattr(content, "arguments", None)
            if arguments is None:
                # The call id with no arguments field at all: this is the call's *result* coming
                # back, so it must not count as the call still growing. Note the test is `is
                # None` and not falsiness — an empty string is a real fragment of the argument
                # stream, and treating it as the end flushed the call before its arguments had
                # arrived, which is how this reached a second live run still empty.
                text = _result_text(content)
                if text is not None:
                    self.outputs.append(text)
                    tool = self._issued.get(key) or self._names.get(key, key)
                    results.append(ToolResultEvent(tool=tool, preview=text[:_ARG_PREVIEW_CHARS]))
                continue
            if name and arguments:
                # The name and the complete arguments in one content: the call arrived whole
                # rather than streamed, so it is finished now, and waiting would only delay it
                # behind the next update's text. The streamed shape never looks like this — its
                # named content carries empty arguments and its fragments carry no name.
                self._fragments[key] = [
                    json.dumps(arguments) if isinstance(arguments, Mapping) else str(arguments)
                ]
                done.add(key)
            else:
                fragments = self._fragments.setdefault(key, [])
                if isinstance(arguments, str) and arguments:
                    fragments.append(arguments)
                elif isinstance(arguments, Mapping) and arguments:
                    fragments[:] = [json.dumps(arguments)]
            growing.add(key)
            if _arguments_complete(self._fragments.get(key)):
                # The provider has finished sending this call's arguments, so the tool is about
                # to run. Announcing now is the whole point of D-159: waiting for the next update
                # means waiting for the result, and the wait between them is the part worth
                # showing.
                done.add(key)
        return [*self._take((set(self._fragments) - growing) | done), *results]

    def flush(self) -> list[Event]:
        """Emit whatever is still open — the stream ended before an untouched update arrived."""
        return self._take(set(self._fragments))

    def _take(self, keys: set[str]) -> list[Event]:
        events: list[Event] = []
        for key in [k for k in self._fragments if k in keys]:
            arguments = "".join(self._fragments.pop(key))[:_ARG_PREVIEW_CHARS]
            name = self._names.pop(key, key)
            # Remembered rather than discarded: the result content carries no name, so this is
            # what lets `ToolResultEvent` report which tool answered.
            self._issued[key] = name
            events.append(ToolCallEvent(tool=name, arguments=arguments))
        return events


def _arguments_complete(fragments: list[str] | None) -> bool:
    """Whether the accumulated argument fragments are a finished JSON value.

    This is the signal that replaces "wait for the next update" as the moment a call is announced
    (D-159). It works because the provider streams a tool call's arguments as one JSON document
    and does not invoke the tool until that document is closed — so a successful parse is exactly
    the boundary between "still arriving" and "about to run", and it is knowable from the bytes
    already in hand rather than from something that follows.

    False for anything that does not parse, which keeps the old update-went-by rule as the
    fallback: a provider streaming a non-JSON argument format still gets its call announced, just
    at the previous, later moment. Nothing is stranded, and nothing regresses.
    """
    if not fragments:
        return False
    try:
        json.loads("".join(fragments))
    except (ValueError, TypeError):
        return False
    return True


def _result_text(content: Any, /) -> str | None:
    """What a function-result content returned, in full, or None when it carries nothing.

    Duck-typed over the attribute the framework version happens to use, for the same reason the
    call side is: the concrete content class is not a stable export. A result that is empty or
    unreadable yields None rather than a value the trace does not have — the trace should not claim
    one, and the verifier must not treat "nothing came back" as evidence.

    Untruncated on purpose. The caller truncates for the wire (`_ARG_PREVIEW_CHARS`, the UI's
    budget) and keeps the whole text for grounding, which are different jobs with different right
    answers; returning the preview here would silently make the second one impossible.

    Failures are deliberately not reported here. A raised call already surfaces as
    `ToolFailedEvent` through the tool middleware, which has the exception and its message; a
    second event for the same outcome would leave a consumer choosing which to believe.
    """
    for attribute in ("result", "output", "value", "text"):
        value = getattr(content, attribute, None)
        if value is None or value == "":
            continue
        return value if isinstance(value, str) else str(value)
    return None


def _approval_prompt(request: Any) -> str:
    """Render a user-input/approval request as a short prompt string for the UI."""
    for attr in ("prompt", "message", "text", "description"):
        value = getattr(request, attr, None)
        if value:
            return str(value)
    return "Approval requested."
