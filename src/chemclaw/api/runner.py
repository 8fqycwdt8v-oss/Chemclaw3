"""The per-turn run lifecycle: the caller that actually runs the agent.

`run_turn` opens the turn's MCP connectors, compiles the turn's graph over them, and streams it as
typed `chemclaw.api.events` (`api/graph_stream.py`); a failure becomes one user-safe `ErrorEvent`.
This module owns the lifecycle (exit stack, state rollback, ambient contextvars); the pure parts are
`runner_trace`, `runner_usage` and `runner_answer`.
"""

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from chemclaw.agent.audit import default_audit_sink
from chemclaw.agent.authz import (
    KNOWLEDGE_WRITE_TOOLS,
    knowledge_read_tools,
    side_effecting_tools,
)
from chemclaw.agent.checkpointer import checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.context_budget import current_context
from chemclaw.agent.exhibit_notes import exhibit_turn_note, mark_told
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.llm_provider import classify_model_failure
from chemclaw.agent.local_skills import personal_skills_available
from chemclaw.agent.loop_cap import loop_hit_cap
from chemclaw.agent.plan_gate import (
    PLAN_APPROVAL_PROMPT,
    approval_stands,
    consume_turn_approval,
    gate_applies,
    plan_author,
    plan_identity,
    spend_approval_after_teardown,
)
from chemclaw.agent.plan_state import session_plan
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.scratchpad import memory_store
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.agent.session_store import InterruptedTurn
from chemclaw.agent.skill_fingerprint import skill_fingerprint
from chemclaw.agent.spend_cap import spend_hit_cap, turn_billed_tokens
from chemclaw.agent.state import turn_config
from chemclaw.agent.stored_skill_tools import stored_skill_declarations
from chemclaw.agent.tool_result_size import (
    bounded_content,
    reset_full_result_sink,
    set_full_result_sink,
)
from chemclaw.agent.turn_ambient import reset_tolerantly, turn_caps
from chemclaw.agent.turn_cost import TurnCost, record_turn_cost
from chemclaw.agent.turn_graph import build_turn_agent
from chemclaw.agent.turn_usage import InFlightPrompts, TurnUsage
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import (
    AnswerEvent,
    ApprovalRequestEvent,
    CapabilityDegradedEvent,
    ErrorCode,
    ErrorEvent,
    Event,
    JobStartedEvent,
    TokenEvent,
    ToolCallEvent,
    ToolFailedEvent,
)
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_answer import build_answer_event
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.tool_results import full_result_sink, session_sink
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.model_prose import ModelProse
from chemclaw.core.session_context import (
    reset_current_session_id,
    set_current_session_id,
)
from chemclaw.core.temporal_client import connect
from chemclaw.core.tracing import start_span
from chemclaw.core.turn_flags import reset_dry_run, set_dry_run
from chemclaw.core.turn_signals import JobSignal, SkillLoadedSignal
from chemclaw.core.turn_text import reset_current_user_texts, set_current_user_texts
from chemclaw.durable.awaiting import AwaitRequest, open_wait
from chemclaw.exhibits.models import ExhibitRef
from chemclaw.kg.note import cited_ids

logger = logging.getLogger(__name__)

# The name announced when the whole durable layer is down. It rides in `CapabilityDegradedEvent`'s
# connector list because a surface treats it identically; the prefix keeps it from being mistaken
# for a bundle.
_DURABLE_SUBSYSTEM = "durable-jobs (Temporal)"

#: How a turn ended, as a closed set with exactly one producer (`_settle_outcome`).
#:
#: `turn_costs.completed` is derived from this and kept for existing dashboards. Turns refused
#: for budget, shed, or 409'd never reach `run_turn`, spend nothing, and have their own
#: counters, so they are not outcomes here. `INTERRUPTED` is recorded too but is not in this
#: set: its producer is whichever process next touches the session, not `_settle_outcome`.
_OUTCOMES = (
    "answered",
    "loop_capped",
    "spend_capped",
    "empty_answer",
    "errored",
    "timed_out",
    "abandoned",
)

#: A turn whose process died mid-turn, booked exactly once by whichever process next touches
#: its session (`settle_interrupted_turns`), with zero spend: what it metered died with it.
INTERRUPTED = "interrupted"


# `classify_model_failure` labels meaning the provider failed, not the request: already retried by
# the SDK, and transient as far as the chemist can tell.
_PROVIDER_FAILURES = frozenset({"timeout", "rate_limited", "transport"})


def _classify(error: BaseException) -> tuple[ErrorCode, bool]:
    """Map a turn failure onto a user-facing code and whether retrying could plausibly help.

    A short closed mapping; each arm answers "what should the person do now?", and anything
    unrecognised stays `internal`. Model failures are asked first through
    `agent/llm_provider.classify_model_failure`, so the chemist's code agrees with the model-call
    metric and log: a context-length refusal is not retryable. `ConnectionError` (database
    unreachable or saturated) is retryable; `ChemclawError` (bad data) is not.
    """
    model_failure = classify_model_failure(error)
    if model_failure == "context_length":
        return "context_length", False
    # Every provider failure (5xx, dead socket, 429, stall) means the same to the chemist: the
    # provider failed, usually transiently, ask again — which is what `llm_timeout` says.
    if model_failure in _PROVIDER_FAILURES:
        return "llm_timeout", True
    # A 401/403 from the gateway is a credential an operator must fix; retrying cannot succeed.
    if model_failure == "auth":
        return "llm_auth", False
    if isinstance(error, ConnectionError):
        return "storage_unavailable", True
    if isinstance(error, TimeoutError):
        return "llm_timeout", True
    if isinstance(error, ChemclawError):
        return "bad_tool_arguments", False
    return "internal", False


async def run_turn(
    session: TurnSession,
    user_message: str,
    *,
    actor: str | None = None,
    roles: frozenset[str] = frozenset(),
    budget: BudgetTracker | None = None,
    dry_run: bool = False,
    connectors: Sequence[Any] | None = None,
    history: Any | None = None,
    profile: str | None = None,
    graph_factory: Callable[..., Any] = build_turn_agent,
    deadline: float | None = None,
    exhibit_refs: Sequence[ExhibitRef] = (),
) -> AsyncIterator[Event]:
    """Run one turn and yield its events (tokens, tool calls, jobs, then an answer or an error).

    Args:
        session: The caller's conversation session, so the turn resumes context.
        user_message: The chemist's message for this turn.
        actor: The authenticated Entra oid, made ambient; `None` off the authenticated path.
        roles: The user's app roles, made ambient for the authorization gate.
        dry_run: Plan without launching anything expensive; ambient, so the model cannot change it.
        connectors: Unopened connector specs; default every enabled connector.
        budget: The runaway-cost meter booked at turn end; `None` disables metering.
        history: The history provider the transcript is written to; `None` writes none.
        profile: The agent profile, used only to label token spend (`None` is `default`).
        graph_factory: Builds the turn's compiled graph; the seam tests use instead of a model.
        deadline: Loop-clock time the caller's whole-turn timeout fires, to tell a timeout from a
            stop; `None` treats every cancellation as an abandonment.
        exhibit_refs: Artefacts the message points at, already resolved by the route.

    Yields:
        `chemclaw.api.events.Event` values, ending with an `AnswerEvent` or an `ErrorEvent`.
    """
    # Adopt the request's correlation id (the pump task copies the request's context), so the
    # response header, access log, audit trail and cost ledger share one id. Off the request path a
    # fresh one is minted.
    ledger = _TurnLedger(
        correlation_id=get_current_correlation_id() or uuid.uuid4().hex,
        usage=TurnUsage(),
        deadline=deadline,
    )
    # Asked exactly as `build_langgraph_agent` asks whether to attach the gate, so the two agree.
    plan_gated = gate_applies(get_profile(profile))
    # Snapshot the session state so a disconnect can roll it back; see `_roll_back_unfinished`.
    state_snapshot = copy.deepcopy(session.state)
    # Bound before the try so the teardown can read it however early the turn died.
    tool_trace: ToolCallTrace | None = None
    # Read before the `with`: `_turn_ambient` may not `await`, because its resets run under
    # cancellation and an `await` there would leak this turn's ambient identity into the next.
    earlier_said = await _earlier_user_texts(history, session)
    # The transcript row this turn's question was written ahead into, and whether it has been
    # settled; out here so the teardown can read both on every path.
    turn_row: int | None = None
    transcript_settled = False
    with (
        _turn_ambient(
            session.session_id,
            actor,
            roles,
            dry_run,
            ledger.correlation_id,
            ledger.usage,
            [*earlier_said, user_message],
        ),
        start_span(
            "chemclaw.turn",
            **{
                "session.id": session.session_id,
                "profile": profile or "",
                # The join key to `audit_events`, `turn_costs`, `session_messages` and logs. `actor`
                # is an identifier, not content.
                "correlation.id": ledger.correlation_id,
                "actor": actor or "",
            },
        ),
    ):
        # The span wraps the whole body, including the post-stream guards, the answer verifier's
        # model call, the transcript write and the final `yield`, so those calls nest under this
        # turn's trace and its duration matches the chemist's wait.
        try:
            log_event(
                logger,
                "turn.started",
                "turn started for session %s",
                session.session_id,
                session_id=session.session_id,
                actor=actor or "",
                correlation_id=ledger.correlation_id,
                profile=profile or "default",
                model=_resolved_model(),
                dry_run=dry_run,
            )
            # Write the question into the transcript before the model sees it, so a turn whose
            # process dies still shows the chemist the question the model will read next turn.
            # Inside the ambient, which stamps the row with this turn's correlation id and sender.
            turn_row = await _begin_transcript_turn(history, session, user_message)
            async with AsyncExitStack() as stack:
                turn_tools, unreachable = await _open_turn_surface(stack, connectors)
                if unreachable:
                    yield CapabilityDegradedEvent(connectors=unreachable)
                # The result sink is built here, where the owning session (for the fetch route's
                # gate) and the correlation id (for the audit join) both exist; `ToolCallTrace`
                # knows neither.
                tool_trace = ToolCallTrace(
                    sink=session_sink(session.session_id, ledger.correlation_id)
                )
                # Held in a local because a mid-turn resume must continue this graph on this thread;
                # compiled per turn because it binds this turn's connector tools. The audit sink is
                # built here (the same `default_audit_sink()` the builder would use) so the turn-end
                # flush can drain it.
                audit_sink = default_audit_sink()
                # Compile the graph in a thread: it is tens of milliseconds of pure-Python work, and
                # the single event loop serves every other stream and the probes. Under the GIL this
                # does not remove the stall, but lets the loop be scheduled during the build. Pooled
                # connections, the checkpointer and the stored skills tiers' tool declarations are
                # awaited first, on the loop. The declaration read takes no actor: it uses
                # `get_current_actor()` as the mount does, which is why it sits inside
                # `_turn_ambient`.
                checkpointer = await _turn_checkpointer()
                store = await turn_store()
                stored_skills = await stored_skill_declarations(store)
                graph = await asyncio.to_thread(
                    graph_factory,
                    profile=profile,
                    actor=actor or "",
                    correlation_id=ledger.correlation_id,
                    audit_sink=audit_sink,
                    connectors=turn_tools,
                    checkpointer=checkpointer,
                    store=store,
                    stored_skills=stored_skills,
                )
                # `turn_config` also carries the graph's step ceiling (the framework default raises
                # on reaching it). The mid-turn resume reuses this config, so it runs under the same
                # bound.
                graph_config = turn_config(session.session_id)
                # Attached to the turn's own invocation so it reaches every model call the graph
                # makes, including from tool bodies, via LangChain's callback contextvar. Attaching
                # at an inner call site would replace the inherited handlers and take that call off
                # this meter.
                graph_config["callbacks"] = [ledger.prompts]
                # The graph emits the event contract itself (`chemclaw.api.graph_stream`); what
                # stays here is the budget ledger, rollback gate, cancellation teardown and metrics.
                # Durable job results that finished meanwhile are claimed into the model's input, so
                # the model learns of them even with no tab open; the claim is atomic, so a live
                # tab's tailer and this turn never both deliver one row.
                user_input = await _with_pushed_job_results(session.session_id, user_message)
                # The artefact note — what the chemist changed and what this message points at — is
                # news for this turn, told once and framed as data. Which artefacts exist reaches
                # every model call through the instructions instead
                # (`agent/exhibit_notes.ExhibitListing`).
                note = await exhibit_turn_note(session.session_id, exhibit_refs)
                if note.text:
                    user_input = f"{user_input}\n\n{note.text}"
                # One carry for the whole turn: `model_calls` and `billed_tokens` are untracked
                # channels, so a resume would otherwise start both caps from zero. Sharing the dict
                # makes the caps per turn, not per invocation.
                cap_carry: dict[str, Any] = {}
                async for event in _stream_into(
                    graph_events(
                        graph,
                        user_input,
                        config=graph_config,
                        trace=tool_trace,
                        on_signal=ledger.note_signal,
                        usage=ledger.usage,
                        exchanges=ledger.exchanges,
                        carry=cap_carry,
                    ),
                    ledger,
                ):
                    yield event
                # The graph has returned and history holds a complete exchange, so a teardown has
                # nothing half-written to discard. Set now, not at the answer, which may still be a
                # verifier call away.
                ledger.run_complete = True
                # Mark the note's edits told only now that the graph has run over them.
                await mark_told(session.session_id, note.told)
                # No trace flush: every call is announced by the `updates` stream that carried it.
                async for event in _resume_on_job_results(
                    graph,
                    config=graph_config,
                    trace=tool_trace,
                    session=session,
                    ledger=ledger,
                    carry=cap_carry,
                ):
                    yield event
                # Everything through the revision loop stays inside the exit stack: the connector
                # sessions close when it exits, and a revision round needs its MCP tools to
                # re-ground the answer.
                for event in _cap_events(session, ledger):
                    yield event
                silent = _empty_answer_event(session, tool_trace, ledger)
                if silent is not None:
                    yield silent
                # An empty answer ships nothing, whatever the cause; whether it also needs its own
                # error event is `_empty_answer_event`'s question (no, when a cap already named it).
                # `return` rather than fall through, so the client gets no empty `AnswerEvent`, no
                # judge call grades `""`, and the cost row is not booked as answered. The teardown
                # still books the spend.
                if not ledger.answer_text.strip():
                    await _settle_transcript_turn(history, session, turn_row, "failed")
                    transcript_settled = True
                    return
                # Before the answer, which is the turn's final event, so the approval card appears
                # with it.
                if plan_gated:
                    pending = await _pending_plan_approval(session.session_id)
                    if pending is not None:
                        yield pending
                answer, review = await build_answer_event(
                    ledger.answer_text,
                    tool_trace.outputs,
                    tool_trace.called_tools,
                )
                # A flagged answer goes back for another pass, bounded by `answer_review_max_rounds`
                # (0 disables). Looped here, not in a middleware: the verdict is produced outside
                # the graph, and the bound must be a per-turn local that a chemist's follow-up
                # resets. Driven by `review.unsupported` only: the review notes are not claims the
                # model can act on, and a verdict with nothing actionable ships marked.
                rounds = 0
                while (
                    answer.review_required
                    and review.unsupported
                    and rounds < settings.answer_review_max_rounds
                ):
                    # The message the thread ends on now, which the round's additions follow. A
                    # checkpointer that cannot answer ends the loop rather than the turn: without
                    # the mark the round's prompt could not be withdrawn from the thread.
                    try:
                        retracted = await _thread_tip(graph, graph_config)
                    except Exception:
                        degraded(
                            logger,
                            "review_revision_thread",
                            "could not read session %s's thread before a review revision; the "
                            "flagged answer ships unrevised",
                            session.session_id,
                        )
                        break
                    rounds += 1
                    # Hold the current answer: `_revise_answer` clears `answer_parts`, and a round
                    # that produces nothing must ship the flagged answer rather than `""`.
                    kept = list(ledger.answer_parts)
                    try:
                        async for event in _revise_answer(
                            graph,
                            config=graph_config,
                            trace=tool_trace,
                            ledger=ledger,
                            carry=cap_carry,
                            claims=review.unsupported,
                        ):
                            yield event
                    except (GeneratorExit, asyncio.CancelledError):
                        raise
                    except Exception:
                        # A revision that raises must not cost the turn its already-graded answer:
                        # log, count as an exhausted round, and ship the held answer.
                        logger.exception(
                            "revision %d for session %s failed; the answer held from before the "
                            "round goes out unchanged",
                            rounds,
                            session.session_id,
                        )
                        ledger.answer_parts[:] = kept
                        # The run that produced the shipped answer did return; restore the flag
                        # `_revise_answer` cleared.
                        ledger.run_complete = True
                        replaced = False
                    else:
                        replaced = bool(ledger.answer_text.strip())
                        if not replaced:
                            logger.warning(
                                "revision %d for session %s produced no text; the previous answer "
                                "is restored and the loop stops",
                                rounds,
                                session.session_id,
                            )
                            ledger.answer_parts[:] = kept
                    # After the outcome is known: the thread must end on the answer that ships.
                    try:
                        await _settle_revision_thread(
                            graph, graph_config, retracted=retracted, replaced=replaced
                        )
                    except Exception:
                        # The answer still ships; only the thread keeps the round's messages, which
                        # is counted.
                        degraded(
                            logger,
                            "review_revision_thread",
                            "could not withdraw revision %d's messages from session %s's thread; "
                            "the answer ships, and the thread keeps what the round added",
                            rounds,
                            session.session_id,
                        )
                    if not replaced:
                        break
                    answer, review = await build_answer_event(
                        ledger.answer_text,
                        tool_trace.outputs,
                        tool_trace.called_tools,
                    )
                if rounds:
                    _record_review_rounds(session, answer, rounds)
                    # The only escalation site, inside `if rounds:`, so `answer_review_max_rounds =
                    # 0` is a complete no-op and a turn escalates at most once however the loop
                    # ended.
                    await _escalate_exhausted_review(
                        session, answer, actor, review.unsupported, ledger.correlation_id
                    )
            # Asked again: a cap can fire inside a revision round. `_cap_events` reads the ledger
            # flag, so each cap is announced once.
            for event in _cap_events(session, ledger):
                yield event
            await _record_transcript(
                history, session, user_message, ledger.answer_text, ledger.exchanges, turn=turn_row
            )
            transcript_settled = True
            # Drain the turn's batched audit rows before answering, so a finished turn's trail is
            # queryable. Duck-typed: only the batching sink has a `flush`.
            sink_flush = getattr(audit_sink, "flush", None)
            if sink_flush is not None:
                await sink_flush()
            # Before the yield: the cancellation reaching a finished turn is delivered while
            # suspended in it, so a flag set afterwards would be false exactly when the teardown
            # needs it.
            ledger.answered = True
            # The `AnswerEvent` is built here rather than streamed, so it is noted explicitly for
            # the cost row's `answer_confidence`, `review_required` and `notes_cited`. Before the
            # yield, for the reason `ledger.answered` is.
            ledger.note_event(answer)
            yield answer
            # The turn used its authorization, so spend it here: in the teardown an `await` would
            # re-raise the cancellation and skip later steps.
            if plan_gated:
                await consume_turn_approval(session.session_id)
        except (GeneratorExit, asyncio.CancelledError):
            # First in this clause: `_book_turn_spend` reads it, and must not depend on the rollback
            # having run.
            ledger.cancelled = True
            # Sampled now, the only instant the reading is exact (see `_TurnLedger.timed_out`).
            ledger.timed_out = _deadline_passed(ledger.deadline)
            _roll_back_unfinished(session, state_snapshot, ledger)
            # A torn-down turn that already acted has used its approval: jobs and writes are not
            # rolled back, so dropping the connection must not allow acting twice. Spent on its own
            # task, since an `await` here would skip the teardown (`spend_approval_after_teardown`).
            # A turn that only read keeps its approval.
            if plan_gated and tool_trace is not None and _turn_acted(tool_trace):
                spend_approval_after_teardown(session.session_id)
            raise
        except Exception as exc:
            yield _failure_event(exc, session, ledger)
            if not transcript_settled:
                await _settle_transcript_turn(history, session, turn_row, "failed")
                transcript_settled = True
            # A turn that failed after its tools may have run has still spent its approval.
            if plan_gated:
                await consume_turn_approval(session.session_id)
        finally:
            _book_turn_spend(ledger, session=session, actor=actor, profile=profile, budget=budget)
            # Settle the torn-down turn's question off this frame (a teardown under cancellation may
            # not `await`). A Stop is `stopped`; the clock is a failure.
            if turn_row is not None and not transcript_settled:
                _settle_after_teardown(
                    history,
                    session,
                    turn_row,
                    "stopped" if ledger.cancelled and not ledger.timed_out else "failed",
                )


@dataclass(slots=True)
class _TurnLedger:
    """What one turn accumulates that more than one of its stages has to read.

    `_book_turn_spend` turns the whole record into one `turn_costs` row. `answered` (an answer was
    delivered; the `completed` column), `run_complete` (the last model run returned, so there is
    nothing to roll back) and `outcome` (how it ended) are three separate questions.
    """

    correlation_id: str
    usage: TurnUsage
    # What in-flight calls have committed this turn to paying, which `usage` cannot know: a gateway
    # reports usage only on the terminal frame. `_book_turn_spend`, which runs on every path, reads
    # it.
    prompts: InFlightPrompts = field(default_factory=InFlightPrompts)
    # Started at construction (`run_turn`'s first statement), so the duration covers the whole turn.
    started: float = field(default_factory=time.perf_counter)
    answered: bool = False
    run_complete: bool = False
    answer_parts: list[str] = field(default_factory=list)
    # Durable jobs this turn launched, for the optional mid-turn resume.
    started_jobs: list[str] = field(default_factory=list)
    # The tool-bearing messages, for the transcript projection: events carry no call id to pair a
    # result with its call.
    exchanges: list[Any] = field(default_factory=list)
    # --- what the turn record is made of (`_settle_outcome`, `_book_turn_spend`) ---------------
    # Set by the failure branch from the same `_classify` that words the client's error, so a quoted
    # code has a server-side record.
    error_code: str = ""
    # Set by the runaway guard as it emits its event, so the teardown need not re-ask a torn-down
    # contextvar.
    loop_capped: bool = False
    # The same, for the spend guard. Separate from `loop_capped` because too-expensive and too-long
    # turns need different fixes and are separate outcomes.
    spend_capped: bool = False
    # Set by the cancellation clause; `deadline` tells a wall-clock kill from a stop.
    cancelled: bool = False
    # The loop-clock reading at which the caller's whole-turn `asyncio.timeout` fires, or `None` if
    # unset. The same clock as the timeout, so at delivery `loop.time() >= deadline` is true for a
    # timeout and false for a Stop; the caller's own `except TimeoutError` runs too late to tell us.
    deadline: float | None = None
    # Whether the clock had passed `deadline` at the instant the cancellation arrived, sampled in
    # the `except` clause. Re-deriving it at teardown would misbook a Stop just before the deadline
    # whose slow teardown crossed it.
    timed_out: bool = False
    # When this turn's answer first began (`perf_counter`) — the latency a chemist experiences,
    # which the whole-turn duration hides. `None` means no answer token was produced, distinct from
    # 0 seconds. Only the supervisor's tokens count (`not event.agent`, as for `answer_parts`), so a
    # turn where only a subagent spoke has no time-to-first-token.
    first_token: float | None = None
    tool_calls: int = 0
    tool_failures: int = 0
    tool_refusals: int = 0
    jobs_started: int = 0
    # The knowledge dimensions of a turn: whether it consulted the record, whether the answer cited
    # it, and whether anything was written back. Not derivable afterwards, so counted in
    # `note_event`, through which every path passes.
    retrieval_calls: int = 0
    capture_calls: int = 0
    # From the turn's own `AnswerEvent`, persisted so the answer-quality signal is retained.
    # `answer_confidence` stays `None` when the verifier did not run, which is not a low score.
    answer_confidence: float | None = None
    review_required: bool = False
    notes_cited: int = 0
    # Which skills shaped this turn, for `agent/distiller.py`'s self-confirmation guard. A set, so a
    # re-read skill counts once; sorted on the way out so rows are stable.
    skills_loaded: set[str] = field(default_factory=set)

    @property
    def answer_text(self) -> str:
        """The supervisor's own prose, which is both the answer and what the transcript stores."""
        return "".join(self.answer_parts)

    @property
    def ttft_seconds(self) -> float | None:
        """Seconds from the turn's first statement to its first streamed token, or `None`."""
        return None if self.first_token is None else self.first_token - self.started

    def note_event(self, event: Event) -> None:
        """Count one streamed event into the turn record — the one place the counts are taken.

        Off the events because every path (model run, resume, subagent) produces them. A refusal is
        a `ToolFailedEvent` whose `reason` `agent/audit.refusal_reason` already classified, so it is
        reused rather than re-decided.
        """
        if isinstance(event, TokenEvent):
            # `not event.agent`: the same filter `_stream_into` applies to `answer_parts`.
            if self.first_token is None and event.text and not event.agent:
                self.first_token = time.perf_counter()
        elif isinstance(event, ToolCallEvent):
            self.tool_calls += 1
            # Both sets are stated, not derived from authz's read-only/state-changing partition,
            # which answers a different question (what needs approval). Each has a test holding it
            # inside its partition. Retrieval is a union because a bundle's searches are the
            # bundle's own declaration.
            if event.tool in knowledge_read_tools():
                self.retrieval_calls += 1
            elif event.tool in KNOWLEDGE_WRITE_TOOLS:
                self.capture_calls += 1
        elif isinstance(event, ToolFailedEvent):
            if event.reason is None:
                self.tool_failures += 1
            else:
                self.tool_refusals += 1
            # A call that was refused or raised consulted nothing, so it is taken back out: counts
            # are taken on the attempt, and a repeat refusal in particular would double-count. A
            # failure may arrive for a call this turn never saw start (a subagent's, a resumed
            # run's), hence the `max(…, 0)` floor.
            if event.tool in knowledge_read_tools():
                self.retrieval_calls = max(self.retrieval_calls - 1, 0)
            elif event.tool in KNOWLEDGE_WRITE_TOOLS:
                self.capture_calls = max(self.capture_calls - 1, 0)
        elif isinstance(event, JobStartedEvent):
            self.jobs_started += 1
        elif isinstance(event, AnswerEvent):
            # Taken off the event so every count lives in this method, whichever path emitted the
            # answer.
            self.answer_confidence = event.confidence
            self.review_required = event.review_required
            self.notes_cited = len(cited_ids(event.text))

    def note_signal(self, signal: Any) -> None:
        """Record what a graph run announced about itself, for the readers below.

        Paired with `note_signal_without_job_chaining`, which later runs of a turn use.
        """
        if isinstance(signal, JobSignal):
            self.started_jobs.append(signal.job_id)
        else:
            self.note_signal_without_job_chaining(signal)

    def note_signal_without_job_chaining(self, signal: Any) -> None:
        """The same, for a run that must not add to what this turn will wait for.

        Only `JobSignal` is suppressed: a resume feeding its own job ids back into `started_jobs`
        could chain durable jobs indefinitely within one request. Every other signal is recorded — a
        skill read during a revision round must reach `skills_loaded`, or the distiller's
        self-confirmation guard fails open.
        """
        if isinstance(signal, SkillLoadedSignal):
            # Both tiers into one set: a personal skill shapes a turn as a reviewed one does, and is
            # the tier most likely to be self-confirming.
            self.skills_loaded.add(signal.skill)


async def _earlier_user_texts(history: Any | None, session: TurnSession) -> list[str]:
    """The chemist's own earlier messages in this thread, bounded, for the `stated` ambient.

    Lets a `basis="stated"` quote cite words from earlier turns. Bounded at the query by
    `agent_stated_quote_turns`. Best-effort: an unreachable store yields `[]`, which refuses rather
    than accepts a quote; other errors are defects and propagate. A provider without
    `recent_user_texts` contributes nothing.
    """
    reader = getattr(history, "recent_user_texts", None)
    if reader is None or settings.agent_stated_quote_turns <= 0:
        return []
    try:
        return list(
            await reader(
                session.session_id,
                limit=settings.agent_stated_quote_turns,
                state=session.state,
            )
        )
    except (ConnectionError, psycopg.Error) as exc:
        degraded(
            logger,
            "stated_quote_history",
            "could not read the chemist's earlier messages for session %s (%s); a "
            '`basis="stated"` quote from an earlier turn will be refused this turn',
            session.session_id,
            exc,
        )
        return []


@contextmanager
def _turn_ambient(
    session_id: str,
    actor: str | None,
    roles: frozenset[str],
    dry_run: bool,
    correlation_id: str,
    usage: TurnUsage,
    user_texts: Sequence[str],
) -> Iterator[None]:
    """Stamp the ambients only a request can supply, and unstamp every one on the way out.

    Synchronous on purpose: the resets run under cancellation, where an `await` would re-raise and
    leak this turn's identity into the next. Ambient rather than arguments: the session (for job
    push-back), the identity, the turn's correlation id, `full_result_sink`, `dry_run` and
    `user_texts` — values the model must not set and that per-profile cached agents and middleware
    cannot be handed at build time. The cap watches come from `agent.turn_ambient.turn_caps`. Resets
    run in reverse order.
    """
    session_token = set_current_session_id(session_id)
    user_texts_token = set_current_user_texts(user_texts)
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    correlation_token = set_current_correlation_id(correlation_id)
    dry_run_token = set_dry_run(dry_run)
    full_results_token = set_full_result_sink(full_result_sink(session_id, correlation_id))
    try:
        # The cap ambients come from `turn_caps`, shared by every turn driver, so no driver can open
        # only some of them.
        with turn_caps(usage, closing=f"session {session_id}"):
            yield
    finally:
        _unstamp(session_id, reset_full_result_sink, full_results_token)
        _unstamp(session_id, reset_dry_run, dry_run_token)
        _unstamp(session_id, reset_current_user_texts, user_texts_token)
        _unstamp(session_id, reset_current_session_id, session_token)
        _unstamp(session_id, reset_current_correlation_id, correlation_token)
        if identity_token is not None:
            _unstamp(session_id, reset_current_identity, identity_token)


def _unstamp(session_id: str, reset: Callable[[Any], None], token: Any) -> None:
    """Undo one of this front door's ambients, naming the session in the log line.

    The tolerance lives in `agent.turn_ambient.reset_tolerantly` (the layering forbids `agent ->
    api`); this adds the session id to the teardown log line.
    """
    reset_tolerantly(reset, token, closing=f"session {session_id}")


async def _open_turn_surface(
    stack: AsyncExitStack, connectors: Sequence[Any] | None
) -> tuple[list[Any], list[str]]:
    """Open this turn's out-of-process capability, and name whatever did not answer.

    Connector sessions belong to one turn (`chemclaw.connectors.transport`) and their tools exist
    only once live. An unreachable connector costs its tools, not the turn; Temporal is probed too.
    The caller announces what is missing before the first token, since only this layer knows.
    Returns the bound tools and the unreachable names.
    """
    # Gathered: both sit on every turn's pre-first-token path and share nothing.
    (turn_tools, unreachable), durable_up = await asyncio.gather(
        open_connector_specs(stack, connectors if connectors is not None else connector_specs()),
        _durable_subsystem_reachable(),
    )
    if not durable_up:
        unreachable = [*unreachable, _DURABLE_SUBSYSTEM]
    return turn_tools, unreachable


async def _stream_into(events: AsyncIterator[Event], ledger: _TurnLedger) -> AsyncIterator[Event]:
    """Re-yield a graph stream unchanged, collecting the supervisor's own tokens as the answer.

    Shared by the model run and the mid-turn resume, which both build `answer_parts`. `not
    event.agent` is load-bearing: a specialist's tokens stream for the trace but are not the answer.
    The supervisor's own tool call ends the paragraph: prose written before a tool ran stays a
    streamed `token` event and its own transcript row, and the answer is the last model call's text
    (which is also what the verifier grades). A helper's tool calls run inside the supervisor's
    `task` call and do not cut the answer.
    """
    async for event in events:
        if isinstance(event, TokenEvent) and not event.agent:
            ledger.answer_parts.append(event.text)
        elif isinstance(event, ToolCallEvent) and not event.agent:
            ledger.answer_parts.clear()
        # Every event of both streams passes here; see `_TurnLedger.note_event`.
        ledger.note_event(event)
        yield event


async def _resume_on_job_results(
    graph: Any,
    *,
    config: dict[str, Any],
    trace: ToolCallTrace,
    session: TurnSession,
    ledger: _TurnLedger,
    carry: dict[str, Any],
) -> AsyncIterator[Event]:
    """Continue this same turn with the results of the durable jobs it launched.

    If enabled, wait (bounded) for this turn's jobs and continue the same graph and thread with
    their results; yields nothing otherwise. Job signals are dropped so a turn cannot chain jobs,
    `run_complete` is cleared while the second run may half-write, and `carry` keeps the in-graph
    caps per turn.
    """
    if not (ledger.started_jobs and settings.mid_turn_resume_enabled):
        return
    results = await await_job_results(
        session.session_id,
        ledger.started_jobs,
        timeout_seconds=settings.mid_turn_resume_timeout_seconds,
    )
    if not results:
        return
    ledger.run_complete = False
    async for event in _stream_into(
        graph_events(
            graph,
            _job_results_message(results),
            config=config,
            trace=trace,
            on_signal=ledger.note_signal_without_job_chaining,
            usage=ledger.usage,
            exchanges=ledger.exchanges,
            carry=carry,
        ),
        ledger,
    ):
        yield event
    ledger.run_complete = True


async def _revise_answer(
    graph: Any,
    *,
    config: dict[str, Any],
    trace: ToolCallTrace,
    ledger: _TurnLedger,
    carry: dict[str, Any],
    claims: Sequence[str],
) -> AsyncIterator[Event]:
    """Run one revision pass over an answer the verifier flagged, in the same turn.

    Like `_resume_on_job_results` — same graph and thread, `run_complete` cleared, same `carry` so
    revisions count against the caps — but it clears `answer_parts`, because a revision replaces the
    answer. `claims` (`TurnReview.unsupported`) are framed as data, not instruction. The caller
    settles the thread afterwards (`_settle_revision_thread`).
    """
    ledger.run_complete = False
    ledger.answer_parts.clear()
    METRICS.increment("chemclaw_answer_revisions_total")
    async for event in _stream_into(
        graph_events(
            graph,
            _revision_message(claims),
            config=config,
            trace=trace,
            # No job chaining, for the reason the resume gives.
            on_signal=ledger.note_signal_without_job_chaining,
            usage=ledger.usage,
            exchanges=ledger.exchanges,
            carry=carry,
        ),
        ledger,
    ):
        yield event
    ledger.run_complete = True


async def _thread_tip(graph: Any, config: dict[str, Any]) -> str | None:
    """The id of the message this thread currently ends on, or `None` off a durable thread.

    `None` without a checkpointer: there is no persisted thread to withdraw from.
    """
    if getattr(graph, "checkpointer", None) is None:
        return None
    state = await graph.aget_state(config)
    messages = state.values.get("messages", []) if state.values else []
    return str(messages[-1].id) if messages else None


async def _settle_revision_thread(
    graph: Any, config: dict[str, Any], *, retracted: str | None, replaced: bool
) -> None:
    """Leave the checkpointed thread ending on the answer that ships, and on nothing fabricated.

    The persisted revision prompt is a `user` message the chemist never wrote. If the round's answer
    ships (`replaced`), the `retracted` answer and the prompt are withdrawn; otherwise everything
    the round added is, leaving the thread as before. Removed as one contiguous run, so no
    `tool_calls` loses its `ToolMessage`. `retracted=None` means no durable thread.
    """
    if retracted is None:
        return
    state = await graph.aget_state(config)
    messages = list(state.values.get("messages", []) if state.values else [])
    ids = [str(message.id) for message in messages]
    if retracted not in ids:
        # The tip moved (a compaction, or a thread not ours); withdrawing by position could take
        # somebody else's message.
        logger.warning("the revised thread no longer carries %s; nothing is withdrawn", retracted)
        return
    added = messages[ids.index(retracted) + 1 :]
    if replaced:
        # The round's one `human` message is the revision prompt; no real user message arrives
        # mid-turn.
        prompt = [str(message.id) for message in added if message.type == "human"]
        drop = [retracted, *prompt]
    else:
        drop = [str(message.id) for message in added]
    if not drop:
        return
    await graph.aupdate_state(config, {"messages": [RemoveMessage(id=dropped) for dropped in drop]})


def _revision_message(claims: Sequence[str]) -> str:
    """What the model is told about its own flagged answer, worded and framed.

    Names the claims rather than saying "try again", so the model re-grounds rather than rewords.
    Takes `TurnReview.unsupported`, never the merged wire list, and is only called with a non-empty
    one.
    """
    named = "\n".join(f"- {claim}" for claim in claims)
    return _REVISION_NOTE + "\n" + frame_untrusted(named, note_id="unsupported-claims")


#: The revision round's instruction. It arrives in the `user` position (a provider needs a user
#: turn after an assistant one, and `_settle_revision_thread` withdraws exactly this message),
#: so it says who is speaking and that the reply is for the chemist, who never sees this note;
#: otherwise the model answers as if the chemist had pushed back.
_REVISION_NOTE = ModelProse(
    "[System note, not from the chemist — the chemist never sees it, so do not reply to it, thank "
    "anyone for it or mention it.] An automated check compared your previous answer with the "
    "evidence this turn actually retrieved, and the claims below are not supported by it. Write "
    "the answer to the chemist's question again, from the beginning and addressed to the chemist, "
    "as if it were your first reply: drop or correct each claim, cite the evidence for what you "
    "keep, and say plainly what the evidence does not settle rather than filling the gap. Do not "
    "open by agreeing with, acknowledging or apologising for anything, and do not call the answer "
    "corrected or revised — to the chemist there is no earlier answer to correct."
)


#: The five things that can become of a request to have a person read a flagged answer. A set,
#: so tests can assert each is reachable and a typo cannot mint a silent extra series.
ESCALATION_OUTCOMES = frozenset({"opened", "joined", "no_claims", "no_actor", "unavailable"})


def _escalation_outcome(outcome: str) -> None:
    """Book what became of one escalation attempt.

    Called beside each return, because the outcomes other than `opened` would otherwise be only log
    lines. Raises rather than asserts (asserts vanish under `python -O`): the registry validates
    label names, not values.
    """
    if outcome not in ESCALATION_OUTCOMES:
        raise ValueError(f"undeclared escalation outcome {outcome!r}")
    METRICS.increment("chemclaw_answer_review_escalations_total", labels={"outcome": outcome})


def _record_review_rounds(session: TurnSession, answer: AnswerEvent, rounds: int) -> None:
    """Book what the revision loop did, including the case where it ran out of rounds.

    Exhaustion is counted: the answer still ships with `review_required`, as it would with the loop
    off, and the difference is visible. Runs exactly once per turn that entered the loop (including
    rounds that raised or produced nothing), so the exhaustion counter and its per-turn denominator
    cannot drift apart.
    """
    METRICS.increment("chemclaw_answer_review_turns_total")
    if answer.review_required:
        METRICS.increment("chemclaw_answer_review_exhausted_total")
        logger.warning(
            "the answer for session %s is still unsupported after %d revision(s); it goes out "
            "marked for review",
            session.session_id,
            rounds,
        )
    else:
        logger.info(
            "the answer for session %s was grounded after %d revision(s)",
            session.session_id,
            rounds,
        )


async def _escalate_exhausted_review(
    session: TurnSession,
    answer: AnswerEvent,
    actor: str | None,
    claims: Sequence[str],
    correlation_id: str,
) -> None:
    """Ask a person to look at an answer the revision loop could not ground.

    Opens a wait (`durable/awaiting.py`) routed to the requester, who can open the thread it names,
    and deduplicated per session so one conversation's exhausted turns share one review. Runs only
    as the turn's authenticated principal — never the `dev-user` stand-in, hence the
    `entra_required` check — so nobody is attributed work they did not ask for. Best-effort: the
    answer is already built, so failures are counted through `degraded` and swallowed.
    """
    if not answer.review_required or not settings.answer_review_escalation_enabled:
        return
    if not claims:
        # A contentless verdict (a judge outage, or a low-confidence verdict with every claim
        # supported) is not escalated, as it is not revised: during a fleet-wide judge outage this
        # would otherwise file an empty review request per conversation.
        _escalation_outcome("no_claims")
        logger.info(
            "the answer for session %s stays marked for review and no person was asked: the "
            "verdict named no unsupported claim for a reviewer to start from",
            session.session_id,
        )
        return
    if not actor or not settings.entra_required:
        _escalation_outcome("no_actor")
        logger.info(
            "the answer for session %s stays marked for review and no person was asked: the turn "
            "has no authenticated actor to raise the request as",
            session.session_id,
        )
        return
    # Built directly rather than via `request_external_input`: its trigger authorization decides
    # against the model's standing for a tool the model did not call, and its premise pre-check
    # would refuse exactly the case a review is for. `requested_by` still comes only from the
    # authenticated identity.
    request = AwaitRequest(
        kind="review",
        subject=(
            "Review a ChemClaw answer that could not be grounded "
            f"(conversation {session.session_id})"
        ),
        rationale=(
            "The automated checks flagged this answer and the revision rounds did not clear the "
            f"flag, so a person is being asked to read it. Turn {correlation_id}. What the checks "
            "could not ground: " + "; ".join(claims)
        ),
        # Routed to the requester, a visibility decision: an unrouted request is listed to the whole
        # tenant, and this one carries model-authored text from an owner-scoped conversation. The
        # requester is the one principal who can open the thread to check it.
        asked_of=actor,
        requested_by=actor,
        session_id=session.session_id,
        correlation_id=correlation_id,
    )
    try:
        # Bounded: the answer is waiting behind this call, and a broker that has died is discovered
        # here. The same budget as the turn's durable-reachability probe; a timeout degrades below.
        request_id, opened = await asyncio.wait_for(
            open_wait(request), settings.connector_health_timeout_seconds
        )
    except Exception:
        _escalation_outcome("unavailable")
        degraded(
            logger,
            "answer_review_escalation",
            "the answer for session %s stays marked for review and the request to have a person "
            "read it could not be opened; the answer still ships",
            session.session_id,
        )
        return
    _escalation_outcome("opened" if opened else "joined")
    if opened:
        logger.warning(
            "a person has been asked to review the answer for session %s, which stayed flagged "
            "after every revision round (%s)",
            session.session_id,
            request_id,
        )
    else:
        logger.info(
            "the answer for session %s stays marked for review; the review request already open "
            "for this conversation covers it (%s)",
            session.session_id,
            request_id,
        )


def _cap_events(session: TurnSession, ledger: _TurnLedger) -> Iterator[ErrorEvent]:
    """Whichever of the turn's two guards has fired and not already been announced.

    Asked when the graph run returns and again after the revision loop, since a cap can trip in
    either; the helpers return `None` once they have spoken, so each cap is announced once.
    """
    for event in (_loop_cap_event(session, ledger), _spend_cap_event(session, ledger)):
        if event is not None:
            yield event


def _partial_answer_clause(ledger: _TurnLedger) -> str:
    """How a cap event ends, which depends on whether the turn wrote anything before it fired.

    A capped turn may have no prose at all, in which case it must not point at an answer below.
    Shared by both cap events.
    """
    if ledger.answer_text.strip():
        return "so the answer below is partial"
    return "and nothing had been written, so there is nothing below to read"


def _loop_cap_event(session: TurnSession, ledger: _TurnLedger) -> ErrorEvent | None:
    """Say out loud that the runaway guard fired, or `None` if it did not.

    The iteration cap (`chemclaw.agent.loop_cap`) stopped work the loop still wanted to do. Said
    before the answer so a surface can mark it partial. The turn is not failed: the answer ships and
    is billed as completed (`loop_cap_reached` shares its turn with an answer, as does
    `_spend_cap_event`'s).
    """
    # Already announced by an earlier ask: one firing is one event.
    if ledger.loop_capped or not loop_hit_cap():
        return None
    # Marked on the ledger: by teardown the watch is gone and `loop_hit_cap()` answers False.
    ledger.loop_capped = True
    METRICS.increment("chemclaw_turn_loop_caps_total")
    logger.warning(
        "the harness loop for session %s hit its %d-iteration cap with work still open",
        session.session_id,
        settings.harness_max_loop_iterations,
    )
    return ErrorEvent(
        message=(
            f"The turn reached its {settings.harness_max_loop_iterations}-iteration limit "
            f"and stopped with work still open, {_partial_answer_clause(ledger)} "
            f"(session {session.session_id})."
        ),
        code="loop_cap_reached",
        # Not retryable unchanged: the same request hits the same cap; ask a narrower one.
        retryable=False,
        correlation_id=ledger.correlation_id,
    )


def _spend_cap_event(session: TurnSession, ledger: _TurnLedger) -> ErrorEvent | None:
    """Say out loud that the turn ran out of budget mid-flight, or `None` if it did not.

    Separate from the loop cap because the remedy differs: a spend-capped turn may have had a small
    plan drowned in tool output, so the next step may be a narrower corpus or a higher
    `agent_max_turn_billed_tokens`. The message carries the spend and the budget so the chemist can
    judge which was wrong. Not retryable unchanged.
    """
    # Already announced by an earlier ask.
    if ledger.spend_capped or not spend_hit_cap():
        return None
    # Marked on the ledger: by teardown the watch is gone and `spend_hit_cap()` answers False.
    ledger.spend_capped = True
    billed = turn_billed_tokens()
    METRICS.increment("chemclaw_turn_spend_caps_total")
    logger.warning(
        "the turn for session %s hit its %d billed-token cap after %d tokens",
        session.session_id,
        settings.agent_max_turn_billed_tokens,
        billed,
    )
    return ErrorEvent(
        message=(
            f"The turn reached its {settings.agent_max_turn_billed_tokens:,}-token budget "
            f"after billing {billed:,} and stopped with work still open, "
            f"{_partial_answer_clause(ledger)} (session {session.session_id})."
        ),
        code="spend_cap_reached",
        retryable=False,
        correlation_id=ledger.correlation_id,
    )


def _empty_answer_event(
    session: TurnSession, trace: ToolCallTrace, ledger: _TurnLedger
) -> ErrorEvent | None:
    """Name a turn that produced no prose at all, or `None` if it produced some.

    A silent turn must say it failed. Retryable. The message states attempted, failed and refused
    calls (a refusal is a gate working, not a failure), and the advice follows the first that
    applies: read the failure, approve the plan, or ask a narrower question.
    """
    if ledger.answer_text.strip():
        return None
    if ledger.loop_capped or ledger.spend_capped:
        # A capped turn is not a silent one: the cap has its own event, counter and outcome, and
        # naming it twice would contradict its `retryable` flag and fire the empty-answer alert,
        # which is meant only for silences nothing explains. The caller still stops on an empty
        # answer.
        return None
    METRICS.increment("chemclaw_turn_empty_answers_total")
    attempted = len(trace.called_tools)
    failed, refused = ledger.tool_failures, ledger.tool_refusals
    logger.warning(
        "turn for session %s ended with no answer text: %d tool call(s) attempted, %d failed, "
        "%d refused",
        session.session_id,
        attempted,
        failed,
        refused,
    )
    counts = f"{attempted} tool call(s) attempted"
    if failed:
        counts += f", {failed} failed"
    if refused:
        counts += f", {refused} refused by a gate"
    # No trailing period: the session id closes the sentence.
    if failed:
        remedy = "The failure(s) reported above are the place to start"
    elif refused:
        remedy = (
            "Nothing failed — the call(s) above were held by a gate, so approving the plan or "
            "leaving dry-run mode is what unblocks them"
        )
    else:
        remedy = "A narrower or more specific question is the useful next step"
    return ErrorEvent(
        message=(
            f"The turn ended without producing an answer: {counts}. Nothing was written, so "
            "there is nothing below to read — this is a failure, not an empty result. "
            f"{remedy} (session {session.session_id})."
        ),
        code="empty_answer",
        retryable=True,
        correlation_id=ledger.correlation_id,
    )


async def _pending_plan_approval(session_id: str) -> ApprovalRequestEvent | None:
    """The approval this session is waiting on, or `None` when it is not waiting on one.

    Emitted at the end of a plan-gated turn whenever the current plan is non-empty and holds no live
    approval — exactly when the gate will refuse the next turn's state-changing calls — so the
    surface shows the decision card. Reads the same sources as the gate (`session_plan`,
    `plan_identity`, `approval_stands`) so the prompt cannot disagree with enforcement; it runs
    before the turn's approval is consumed. Never raises: the gate still refuses regardless, so
    silence costs a card, not a control.
    """
    try:
        steps = await session_plan(session_id)
        if steps is None:
            return None
        plan_hash = plan_identity(steps)
        if plan_hash is None:
            return None
        if await approval_stands(session_id, plan_hash):
            return None
        # A plan another participant's turn wrote is theirs to decide; no card the route would 403.
        # An unrecorded author is left to the route's owner fallback.
        author = await plan_author(session_id, plan_hash)
        if author and author != get_current_actor():
            return None
        return ApprovalRequestEvent(prompt=PLAN_APPROVAL_PROMPT, approval_id="")
    except Exception:
        logger.warning(
            "could not determine whether session %s's plan awaits approval; the decision card is "
            "not shown this turn and the gate still refuses unapproved work",
            session_id,
            exc_info=True,
        )
        return None


_INTERNAL_REASON = "The turn could not be completed due to an internal error"

# What the chemist reads for each code `_classify` can return. Only a code whose cause is unknown
# says "internal".
_FAILURE_REASONS: dict[ErrorCode, str] = {
    # The one remedy that is the chemist's rather than an operator's, so it says what to do.
    "context_length": (
        "The conversation has grown too long for the model to read in one request; start a new "
        "session or ask a narrower question"
    ),
    # Every provider-side failure; the sentence names the provider without claiming which kind,
    # since the remedy is the same.
    "llm_timeout": (
        "The model provider failed to answer before the turn finished (it stopped responding, was "
        "overloaded or returned an error); this is usually temporary, so try again in a moment"
    ),
    # Not the chemist's to fix and not temporary, so it says both — and names who can fix it.
    "llm_auth": (
        "The model provider refused this deployment's credentials, so no turn can run until an "
        "operator fixes them; asking again will not help, so please report it"
    ),
    "storage_unavailable": (
        "The turn could not be completed because its database was unavailable; try again in a "
        "moment"
    ),
    "bad_tool_arguments": (
        "The turn could not be completed because a tool was given input it cannot use; rephrasing "
        "the request may help"
    ),
}


def failure_event(exc: Exception, session_id: str, correlation_id: str) -> ErrorEvent:
    """One failed turn as one user-safe, classified event — never a leaked trace.

    Public and id-based because `chemclaw.api.routes.turns` is the second caller, for failures one
    frame above `run_turn`; one function means both sites report a code identically. Exception
    detail stays in the server log; the client gets the classification and the correlation id.
    """
    code, retryable = _classify(exc)
    reason = _FAILURE_REASONS.get(code, _INTERNAL_REASON)
    return ErrorEvent(
        message=f"{reason} (session {session_id}).",
        code=code,
        retryable=retryable,
        correlation_id=correlation_id,
    )


def _failure_event(exc: Exception, session: TurnSession, ledger: _TurnLedger) -> ErrorEvent:
    """`failure_event` for a turn that is already running, with this turn's log line beside it.

    Also keeps the code on the ledger, so the turn record shows how it failed.
    """
    logger.exception("turn failed for session %s", session.session_id)
    event = failure_event(exc, session.session_id, ledger.correlation_id)
    ledger.error_code = event.code
    return event


def _turn_acted(trace: ToolCallTrace) -> bool:
    """Whether this turn issued any state-changing call — what decides if a teardown spends.

    Counts attempts, refused ones included: over-spending costs an extra approval click,
    under-spending a free second turn under one decision.
    """
    acting = side_effecting_tools()
    return any(name in acting for name in trace.called_tools)


def _roll_back_unfinished(
    session: TurnSession, snapshot: dict[str, Any], ledger: _TurnLedger
) -> None:
    """Undo the bookkeeping of a turn torn down before its exchange completed.

    Restores `session.state` (todo list, plan hash, marks) when a disconnect or deadline tears down
    a turn whose last model run had not returned (`run_complete`), so the next turn does not read
    steps never taken. sse-starlette delivers a disconnect as `CancelledError`. The graph's
    checkpoint is not undone, so a teardown after the graph run but before `_record_transcript`
    leaves the two records diverged; that branch is counted.
    """
    if ledger.answered or ledger.run_complete:
        if not ledger.answered:
            # The graph committed the exchange but `_record_transcript` never ran: the records
            # differ by one turn.
            METRICS.increment("chemclaw_transcript_thread_divergence_total")
        logger.warning(
            "turn for session %s was torn down after its exchange completed (client "
            "disconnect or the front door's turn deadline); the committed turn is kept%s",
            session.session_id,
            (
                ""
                if ledger.answered
                else " — it is in the checkpointer and not in the transcript, so the chemist's "
                "view of this session is one turn behind the model's"
            ),
        )
        return
    logger.warning(
        "turn for session %s was torn down before it answered (client disconnect or the "
        "front door's turn deadline); rolling session state back",
        session.session_id,
    )
    session.state.clear()
    session.state.update(snapshot)


def _settle_outcome(ledger: _TurnLedger) -> str:
    """How this turn ended, as one value of `_OUTCOMES` — the one producer of that enum.

    Precedence: `errored`; `timed_out`; the caps (a cause, not their empty-answer symptom, and
    hidden if `answered` came first); `answered` (a late disconnect after delivery still bills as
    answered); then `abandoned` if cancelled, else `empty_answer`.
    """
    if ledger.error_code:
        return "errored"
    if ledger.cancelled and ledger.timed_out:
        # Read off the ledger: the deadline was sampled when the cancellation arrived, since by now
        # a slow teardown could have crossed it.
        return "timed_out"
    if ledger.loop_capped:
        return "loop_capped"
    # After `loop_capped` because the iteration cap's `before_model` hook runs first, so over both
    # ceilings the spend cap never fires.
    if ledger.spend_capped:
        return "spend_capped"
    # `ledger.answered`, not "some prose was emitted": it is set only immediately before `yield
    # answer`, so it means an `AnswerEvent` was delivered. A turn stopped one token into its answer
    # must not be billed as answered. A disconnect after the answer still lands here, since the
    # cancellation arrives while suspended in that yield.
    if ledger.answered:
        return "answered"
    # The same absence, told apart by whether the turn was cut short (`abandoned`) or ran to its own
    # end and said nothing (`empty_answer`).
    return "abandoned" if ledger.cancelled else "empty_answer"


def _deadline_passed(deadline: float | None) -> bool:
    """Whether the event loop has reached `deadline` — `False` when there is none, or no loop.

    `>=` on the clock `asyncio.timeout` uses is exact at the instant it is taken, which is why its
    caller is the `except` clause receiving the cancellation. With no running loop there was no
    deadline either, and raising would lose the cost row.
    """
    if deadline is None:
        return False
    try:
        return asyncio.get_running_loop().time() >= deadline
    except RuntimeError:
        return False


def _resolved_model() -> str:
    """The model id this deployment's *agent* route resolves to, for the turn record.

    `turn_costs` carries model attribution because the metric label set omits `model`.
    `model_routes["agent"]` wins where configured, else `llm_model` (validated non-empty). A turn
    can span models — the verifier's judge may run on another route and its tokens are metered into
    the turn — and this column names the agent route, which produced the answer.
    """
    return settings.model_routes.get("agent") or settings.llm_model


def _book_turn_spend(
    ledger: _TurnLedger,
    *,
    session: TurnSession,
    actor: str | None,
    profile: str | None,
    budget: BudgetTracker | None,
) -> None:
    """Book what the turn cost, on every path — success, failure and disconnect alike.

    Synchronous so it cannot `await` on the cancellation path. Observes duration on failures too and
    publishes tokens labelled by profile; `record_turn_cost` books the same per actor in a table,
    since an oid must not be a metric label. The one producer of a chat turn's `turn_costs` row
    (template steps book their own).
    """
    elapsed = time.perf_counter() - ledger.started
    # The budget is booked first, so a failure in any later derivation costs record precision, not
    # the budget record or the row. Tokens the provider never reported (usage arrives only on the
    # terminal frame, so a cancelled turn meters zero) are estimated from in-flight prompts rather
    # than dropped, so a late disconnect costs what it costs; zero on ordinary turns. The estimate
    # is an input to the booking and cannot fail, so it may precede it.
    estimated = ledger.prompts.unbilled_tokens
    if budget is not None:
        budget.record(session.session_id, actor, ledger.usage.total + estimated)
    outcome = "unknown"
    model = ""
    context = None
    try:
        outcome = _settle_outcome(ledger)
        model = _resolved_model()
        # Last and cheapest to lose: its consumers already handle `None`.
        context = current_context()
    except Exception:
        # Assigned progressively, so an earlier derivation survives a later failure. `unknown` is
        # the column default; reaching it through this arm is why the arm logs loudly.
        logger.exception(
            "settling the turn record for session %s failed; booking it as %r",
            session.session_id,
            outcome,
        )
    METRICS.observe("chemclaw_turn_duration_seconds", elapsed)
    METRICS.increment("chemclaw_turns_finished_total", labels={"outcome": outcome})
    spend_labels = {"profile": profile or "default"}
    record_turn_cost(
        TurnCost(
            correlation_id=ledger.correlation_id,
            session_id=session.session_id,
            actor=actor or "",
            profile=profile or "default",
            input_tokens=ledger.usage.input,
            output_tokens=ledger.usage.output,
            cache_read_tokens=ledger.usage.cache_read,
            cache_write_tokens=ledger.usage.cache_write,
            estimated_tokens=estimated,
            duration_seconds=elapsed,
            # `ledger.answered`, not `outcome == "answered"`: a loop-capped turn and a turn that
            # raised after answering both delivered an answer. `completed` is the billing question;
            # `outcome` says how the turn ended.
            completed=ledger.answered,
            outcome=outcome,
            error_code=ledger.error_code,
            model=model,
            tool_calls=ledger.tool_calls,
            tool_failures=ledger.tool_failures,
            tool_refusals=ledger.tool_refusals,
            jobs_started=ledger.jobs_started,
            ttft_seconds=ledger.ttft_seconds,
            # Read off the turn's context record (its producer is a middleware); still inside
            # `_turn_ambient`, so it is this turn's.
            compacted=context.compacted if context is not None else False,
            context_unreducible=context.unreducible if context is not None else False,
            retrieval_calls=ledger.retrieval_calls,
            capture_calls=ledger.capture_calls,
            answer_confidence=ledger.answer_confidence,
            review_required=ledger.review_required,
            notes_cited=ledger.notes_cited,
            # Digested: `turn_costs` is retained after a person leaves, and a skill name is the
            # chemist's own words. The distiller's guard only asks equality, which a digest answers.
            skills_loaded=sorted(skill_fingerprint(name) for name in ledger.skills_loaded),
        )
    )
    # The same record as a log line, since the cost row needs Postgres and the log stack is always
    # there; pairs with `turn.started`.
    log_event(
        logger,
        "turn.finished",
        "turn %s for session %s in %.1fs",
        outcome,
        session.session_id,
        elapsed,
        session_id=session.session_id,
        actor=actor or "",
        correlation_id=ledger.correlation_id,
        outcome=outcome,
        error_code=ledger.error_code,
        profile=profile or "default",
        model=model,
        duration_seconds=round(elapsed, 3),
        ttft_seconds=None if ledger.ttft_seconds is None else round(ledger.ttft_seconds, 3),
        input_tokens=ledger.usage.input,
        output_tokens=ledger.usage.output,
        # Beside the measured counts, never summed into them, so an inferred number cannot pass for
        # a provider's. The budget meters the sum; the estimate is published on the row and on
        # `chemclaw_estimated_tokens_total`.
        estimated_tokens=estimated,
        tool_calls=ledger.tool_calls,
        tool_failures=ledger.tool_failures,
        tool_refusals=ledger.tool_refusals,
        jobs_started=ledger.jobs_started,
    )
    if ledger.usage.unreadable:
        # Usage was reported but unreadable, so the turn metered zero and the cost guard is not
        # binding. ERROR because the fix is a code change; counted because per-turn log lines are
        # not aggregated.
        logger.error(
            "usage_unreadable: %d usage content(s) carried no token count; this turn metered "
            "zero and the budget guard did not bind",
            ledger.usage.unreadable,
        )
        METRICS.increment("chemclaw_usage_unreadable_total", float(ledger.usage.unreadable))
    if ledger.usage.total:
        METRICS.increment("chemclaw_tokens_total", float(ledger.usage.total), spend_labels)
    # Published separately because priced separately. Each is skipped when the provider reports
    # none, rather than publishing a fabricated zero.
    for name, value in (
        ("chemclaw_input_tokens_total", ledger.usage.input),
        ("chemclaw_output_tokens_total", ledger.usage.output),
        ("chemclaw_cache_read_tokens_total", ledger.usage.cache_read),
        ("chemclaw_cache_write_tokens_total", ledger.usage.cache_write),
        # The inferred estimate is its own series rather than a label on the measured one, so
        # existing panels keep their meaning and abandoned turns' spend still shows in the
        # fleet-wide rate.
        ("chemclaw_estimated_tokens_total", estimated),
    ):
        if value:
            METRICS.increment(name, float(value), spend_labels)


async def _turn_checkpointer() -> Any:
    """The graph engine's checkpointer, or `None` where this deployment stores nothing durably.

    Gated on the same `session_store` setting `history_provider` reads, so both agree on durability,
    and an in-memory deployment never needs Postgres.

    Returns:
        A ready `AsyncPostgresSaver`, or `None` to keep turn state in the invocation.
    """
    if settings.session_store != "postgres":
        return None
    return await checkpointer()


async def turn_store() -> Any:
    """This turn's durable memory store, or `None` where the deployment keeps none.

    Gated by `local_skills.personal_skills_available`; built here so `build_langgraph_agent` stays
    synchronous; public because `api/routes/skills.py` reads the same store. Whether the turn has an
    actor is `scratchpad_backend`'s decision. Returns an `AsyncPostgresStore` or `None`.
    """
    if not personal_skills_available():
        return None
    return await memory_store()


async def _durable_subsystem_reachable() -> bool:
    """Is Temporal answering right now? — the per-turn probe behind the durable outage announcement.

    An unreachable broker removes every durable capability at once, and only this layer knows before
    the turn starts. `check_health` rather than `connect`, which caches the client for the process;
    `retry=False` keeps it a probe. Bounded by `connector_health_timeout_seconds`, the connector
    sweep's budget. Never raises: any failure means "not reachable".
    """
    try:
        client = await asyncio.wait_for(connect(), settings.connector_health_timeout_seconds)
        return await asyncio.wait_for(
            client.service_client.check_health(retry=False),
            settings.connector_health_timeout_seconds,
        )
    except Exception:
        # DEBUG, because the chemist is told on the stream and a per-turn log line is noise; the
        # counter aggregates it into an alertable rate. The connector-unreachable counter does not
        # cover Temporal.
        logger.debug("the durable subsystem did not answer its health probe", exc_info=True)
        METRICS.increment("chemclaw_durable_unreachable_total")
        return False


async def _with_pushed_job_results(session_id: str, user_message: str) -> str:
    """The turn's input, with any waiting job push-back appended as framed data.

    Lets the model learn a job it launched finished even with no tab open, via the same atomic
    kind-scoped claim as the SSE stream. Push-back is framed (`frame_untrusted`) after the chemist's
    words, each summary cut to `agent_max_tool_result_chars` inside the frame. Best-effort.
    """
    if settings.session_store != "postgres":
        return user_message
    try:
        pushed = await claim_unconsumed(session_id, kinds=("job_completed", "job_failed"))
    except Exception:
        logger.debug("could not read session %s's job push-back mailbox", session_id, exc_info=True)
        return user_message
    if not pushed:
        return user_message
    summary = "\n".join(
        f"- {event.kind}: {json.dumps(event.payload, sort_keys=True, default=str)}"
        for event in pushed
    )
    bounded, removed = bounded_content(
        summary,
        "the job push-back mailbox",
        settings.agent_max_tool_result_chars,
        remedy="call get_durable_job_status for the jobs whose outcomes were cut",
    )
    if removed:
        logger.info(
            "session %s's job push-back was cut by %d characters to stay inside the turn's "
            "message bound; %d event(s) were waiting",
            session_id,
            removed,
            len(pushed),
        )
    return (
        f"{user_message}\n\n"
        "Since your previous turn, durable job(s) this session started have finished. Some may "
        "have failed: report any entry whose kind is 'job_failed' to the chemist rather than "
        "describing that work as done. Their outcomes follow as data; use "
        "get_durable_job_status for full results where needed.\n"
        + frame_untrusted(bounded, note_id="job-results")
    )


def _job_results_message(results: dict[str, dict[str, Any]]) -> str:
    """The completed jobs, worded and framed as the message that continues the turn.

    Handed to the model as framed data, not instruction (`chemclaw.agent.framing`). Rendered as JSON
    (`default=str`, `sort_keys`) rather than a Python repr the model would have to guess at.
    """
    summary = "\n".join(
        f"- {job_id}: {json.dumps(payload, sort_keys=True, default=str)}"
        for job_id, payload in results.items()
    )
    return (
        # "finished", not "completed", with an explicit failure instruction: a sentence asserting
        # success would outrank a `status: failed` field.
        "The durable job(s) you started have finished. Some may have failed: report any result "
        "whose status is 'failed' to the chemist, with its summary, rather than describing the "
        "work as done. Their results follow as data; continue your answer using them.\n"
        + frame_untrusted(summary, note_id="job-results")
    )


async def _record_transcript(
    history: Any | None,
    session: Any,
    user_message: str,
    answer: str,
    exchanges: list[Any] | None = None,
    *,
    turn: int | None = None,
) -> None:
    """Write this turn's exchange to the session transcript, best-effort.

    `session_messages` backs `GET /sessions/{id}/messages`; the graph keeps its thread only in the
    checkpointer. Usually appends to the written-ahead question (`turn`) and settles it `done`;
    otherwise writes the exchange whole. Tool exchanges are included, since only messages carry call
    ids. An empty answer is not written.
    """
    if history is None or not answer.strip():
        return
    if not hasattr(history, "save_messages"):
        # Duck-typed: `history` is whatever the caller injected, and a provider that does not store
        # is tolerated.
        return
    session_id = session.session_id
    try:
        if turn is not None and hasattr(history, "finish_turn"):
            await history.finish_turn(
                session_id,
                turn,
                [*(exchanges or []), AIMessage(content=answer)],
                "done",
                state=session.state,
            )
            return
        await history.save_messages(
            session_id,
            [
                HumanMessage(content=user_message),
                *(exchanges or []),
                AIMessage(content=answer),
            ],
            # `state` holds the in-memory provider's thread, so one call is correct under both
            # stores.
            state=session.state,
        )
    except (ConnectionError, psycopg.Error) as exc:
        degraded(
            logger,
            "transcript_projection",
            "could not record the transcript for session %s (%s); the turn answered and the "
            "conversation is intact in the checkpointer, but this exchange will be missing from "
            "the transcript route",
            session_id,
            exc,
        )


async def _begin_transcript_turn(
    history: Any | None, session: TurnSession, user_message: str
) -> int | None:
    """Write this turn's question ahead of it, `running`; the row to settle, or `None`.

    First settles whatever the previous turn left (`settle_interrupted_turns`): this turn holds the
    claim, so an unsettled question has no live owner. Done before the write so the new turn is
    never a candidate. `None` with no write-ahead provider or a refused write; the turn runs anyway
    and `_record_transcript` writes the exchange whole.
    """
    begin = getattr(history, "begin_turn", None)
    if begin is None:
        return None
    await settle_interrupted_turns(history, session.session_id, state=session.state)
    try:
        turn: int | None = await begin(
            session.session_id, HumanMessage(content=user_message), state=session.state
        )
    except (ConnectionError, psycopg.Error) as exc:
        degraded(
            logger,
            "transcript_projection",
            "could not write session %s's question ahead of its turn (%s); the turn runs and its "
            "exchange is written whole once it answers",
            session.session_id,
            exc,
        )
        return None
    return turn


async def _settle_transcript_turn(
    history: Any | None, session: TurnSession, turn: int | None, status: str
) -> None:
    """Settle a written-ahead question that ended without an answer, best-effort.

    `failed` or `stopped`, never `done`. Only a question still `running` moves, so this cannot undo
    an answer or another process's mark.
    """
    finish = getattr(history, "finish_turn", None)
    if turn is None or finish is None:
        return
    try:
        await finish(session.session_id, turn, [], status, state=session.state)
    except (ConnectionError, psycopg.Error) as exc:
        degraded(
            logger,
            "transcript_projection",
            "could not settle session %s's question as %s (%s); it stays `running` until the "
            "session is next touched, which then reads it as interrupted",
            session.session_id,
            status,
            exc,
        )


#: Teardown settlements still in flight, per session, held so a task cannot be collected mid-write
#: and so the turn's own route can wait for them (`transcript_settled`).
_PENDING_SETTLES: dict[str, set["asyncio.Task[None]"]] = {}


def _settle_after_teardown(
    history: Any | None, session: TurnSession, turn: int, status: str
) -> None:
    """`_settle_transcript_turn` from a teardown, where an `await` would re-raise the cancellation.

    Synchronous, the write on its own task that degrades rather than raises, and held per session so
    the route owning the turn's claim can wait for it (`transcript_settled`).
    """
    session_id = session.session_id
    try:
        task = asyncio.get_running_loop().create_task(
            _settle_transcript_turn(history, session, turn, status)
        )
    except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
        logger.warning("no event loop to settle session %s's question", session_id)
        return
    _PENDING_SETTLES.setdefault(session_id, set()).add(task)

    def _done(finished: "asyncio.Task[None]") -> None:
        pending = _PENDING_SETTLES.get(session_id)
        if pending is not None:
            pending.discard(finished)
            if not pending:
                del _PENDING_SETTLES[session_id]

    task.add_done_callback(_done)


async def transcript_settled(session_id: str) -> None:
    """Wait until every torn-down turn of this session has settled its question.

    The route calls this before releasing the turn's claim: otherwise the next message could take
    the claim, find the stopped turn's question still `running`, and book it `interrupted` beside
    its own outcome. Shielded, because the caller is a `finally` running under cancellation.
    """
    pending = list(_PENDING_SETTLES.get(session_id, ()))
    if pending:
        await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))


async def settle_interrupted_turns(
    history: Any | None, session_id: str, *, state: dict[str, Any] | None = None
) -> int:
    """Mark the session's turns whose owner died `interrupted`, and book each one's outcome once.

    A killed pod runs no teardown, so the next to touch the session (its next turn, a reattach, a
    transcript read) asks; the provider marks only questions whose claim lapsed, exactly once across
    processes, so the zero-spend booking is once too. Best-effort. Returns how many turns this call
    marked.
    """
    mark = getattr(history, "mark_interrupted", None)
    if mark is None:
        return 0
    try:
        interrupted: list[InterruptedTurn] = await mark(session_id, state=state)
    except (ConnectionError, psycopg.Error) as exc:
        degraded(
            logger,
            "transcript_projection",
            "could not check session %s for an interrupted turn (%s); the next reader will",
            session_id,
            exc,
        )
        return 0
    for turn in interrupted:
        _book_interrupted(session_id, turn)
    return len(interrupted)


def _book_interrupted(session_id: str, turn: InterruptedTurn) -> None:
    """The record of one interrupted turn: its counter, its log record and its `turn_costs` row.

    Skipped for a turn already booked by its own process (which then failed to settle its question),
    so no turn is counted twice.
    """
    correlation_id, actor = turn.correlation_id, turn.actor
    if turn.booked:
        logger.info(
            "session %s's turn %s had no settled question and is marked interrupted; its outcome "
            "was already booked by its own process, so none is booked here",
            session_id,
            correlation_id,
        )
        return
    METRICS.increment("chemclaw_turns_finished_total", labels={"outcome": INTERRUPTED})
    log_event(
        logger,
        "turn.interrupted",
        "turn interrupted for session %s: its process ended mid-turn and its claim lapsed",
        session_id,
        session_id=session_id,
        actor=actor or "",
        correlation_id=correlation_id,
        outcome=INTERRUPTED,
    )
    if correlation_id:
        # Without a correlation id there is no turn to name, and `TurnCost` refuses an empty one.
        record_turn_cost(
            TurnCost(
                correlation_id=correlation_id,
                session_id=session_id,
                actor=actor or "",
                completed=False,
                outcome=INTERRUPTED,
            )
        )
