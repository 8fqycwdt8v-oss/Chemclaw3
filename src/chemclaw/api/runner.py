"""The per-turn run lifecycle (plan step F2-T1): the missing caller that actually runs the agent.

`run_turn` owns exactly what the agent's own docstring says a caller must own: it opens the MCP
tool connectors for the turn (`connectors.registry.open_connector_specs`), compiles the turn's
graph over them, and translates the graph's stream into the typed `chemclaw.api.events` the
surfaces render (`api/graph_stream.py`). When the harness is enabled the *same* stream drives its
completion loop, so plan/execute autonomy needs no separate driver here.

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
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from langchain_core.messages import AIMessage, HumanMessage

from chemclaw.agent.checkpointer import checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.loop_cap import begin_loop_watch, end_loop_watch, loop_hit_cap
from chemclaw.agent.plan_gate import (
    PLAN_APPROVAL_PROMPT,
    approval_stands,
    consume_turn_approval,
    gate_applies,
    plan_identity,
)
from chemclaw.agent.plan_state import session_todos
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.scratchpad import memory_store
from chemclaw.agent.session import TurnSession
from chemclaw.agent.state import turn_config
from chemclaw.agent.turn_cost import TurnCost, record_turn_cost
from chemclaw.agent.turn_flags import reset_dry_run, set_dry_run
from chemclaw.agent.turn_usage import TurnUsage, reset_turn_usage, set_turn_usage
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import (
    ApprovalRequestEvent,
    CapabilityDegradedEvent,
    ErrorCode,
    ErrorEvent,
    Event,
    TokenEvent,
)
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_answer import build_answer_event
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.tool_results import session_sink
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import (
    reset_current_session_id,
    set_current_session_id,
)
from chemclaw.core.temporal_client import connect
from chemclaw.core.tracing import start_span
from chemclaw.core.turn_signals import JobSignal

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
    graph_factory: Callable[..., Any] = build_langgraph_agent,
) -> AsyncIterator[Event]:
    """Run one turn and yield its events (tokens, tool calls, approvals, then the answer).

    Args:
        session: The caller's conversation session (per user+thread), so the turn resumes context.
        user_message: The chemist's message for this turn.
        actor: The authenticated user's Entra oid (F4), made ambient so the audit trail, the
            authorization gate, and job attribution see it. `None` off the authenticated path.
        roles: The user's app roles, made ambient for the authorization gate.
        dry_run: Plan the turn without launching anything expensive (IDEA-4). Ambient for the
            turn rather than a tool argument, so the model can neither set it nor clear it.
        connectors: This turn's unopened connector specs. Defaults to every enabled connector
            (`chemclaw.agent.chemclaw_agent.connector_specs`); a caller that selected an agent
            profile passes that profile's narrowed set, and a test passes an empty list to run
            with none.
        budget: The runaway-cost meter. When set, this turn's reported token usage and its turn
            count are booked against the session/user when the turn ends (the front-door
            admission check reads those counters before the *next* turn). `None` disables
            metering (test/CLI).
        history: The session's history provider. This turn's transcript is projected into it
            (`_record_transcript`) once the answer exists, which is the only thing it is used for
            here — the graph reads its own checkpointer, never this. `None` runs the turn without
            a transcript, which is what the CLI and most tests do.
        profile: The session's agent profile, used only to label this turn's token spend
            (REV-10) — the surface it selects is chosen by the caller, which passes the matching
            agent and connectors. `None` labels the spend `default`, so every series carries the
            same label set and the family sums to the deployment's whole spend.
        graph_factory: Builds this turn's compiled graph, given the profile, the turn's identity
            and its already-open connectors. A parameter rather than a direct call so a test can
            drive a whole turn without a live model credential — the front door supplies it from
            `create_app(graph_factory=…)`. It is the *only* such seam now that the agent argument
            is gone, which is why it exists: 67 tests broke the first time the engine was flipped,
            for want of it.

    Yields:
        `chemclaw.api.events.Event` values in the order the model produced them, ending with an
        `AnswerEvent` on success or an `ErrorEvent` on failure.
    """
    ledger = _TurnLedger(correlation_id=uuid.uuid4().hex, usage=TurnUsage())
    # Whether this turn's approval is spendable, asked exactly as `build_langgraph_agent` asks
    # whether to attach the gate — one predicate, so the two cannot disagree about a profile that
    # overrides the deployment's autonomy.
    plan_gated = gate_applies(get_profile(profile))
    # Snapshot the session state before the turn so a client disconnect can roll it back
    # (ISSUE-B-10). What the snapshot is for, and why only `session.state` is in it, is in
    # `_roll_back_unfinished`.
    state_snapshot = copy.deepcopy(session.state)
    with _turn_ambient(
        session.session_id, actor, roles, dry_run, ledger.correlation_id, ledger.usage
    ):
        try:
            async with AsyncExitStack() as stack:
                # The turn span, which is the parent every other span in this request hangs from —
                # the model calls the graph emits, the tool spans `agent/audit.py` opens, and
                # (through `traceparent`) whatever a connector does on our behalf. Pushed onto the
                # stack that is already here rather than wrapping the body, so the span's lifetime
                # is exactly the turn's teardown and there is no second place that has to remember
                # to close it.
                stack.enter_context(
                    start_span(
                        "chemclaw.turn",
                        **{"session.id": session.session_id, "profile": profile or ""},
                    )
                )
                turn_tools, unreachable = await _open_turn_surface(stack, connectors)
                if unreachable:
                    yield CapabilityDegradedEvent(connectors=unreachable)
                # The sink is built here, and only here, because this is where the two things a
                # stored result has to be filed under exist: the session that owns it (which is what
                # the fetch route's ownership gate resolves against) and the turn's correlation id
                # (which is what ties a fetched result back to the audit trail). `ToolCallTrace`
                # deliberately knows neither — see its module docstring.
                tool_trace = ToolCallTrace(
                    sink=session_sink(session.session_id, ledger.correlation_id)
                )
                # This turn's compiled graph. Held in a local rather than built inline because a
                # mid-turn resume has to continue *this* graph on *this* thread — a second build
                # would bind a second set of connector sessions and start the continuation from an
                # empty conversation. Compiled *inside* the turn because it binds this turn's
                # connector tools at construction (M7).
                graph = graph_factory(
                    profile=profile,
                    actor=actor or "",
                    correlation_id=ledger.correlation_id,
                    connectors=turn_tools,
                    checkpointer=await _turn_checkpointer(),
                    store=await _turn_store(),
                )
                # `turn_config`, not a bare `configurable`: it also carries the graph's step
                # ceiling, which nothing here had ever chosen — the framework bakes 9999, and
                # reaching it raises rather than degrading. The mid-turn resume below reuses this
                # same config, so the continuation runs under the same bound as the run it
                # continues.
                graph_config = turn_config(session.session_id)
                # The graph drives itself and emits the contract directly
                # (`chemclaw.api.graph_stream`), so everything from here to the end of the stream is
                # that module's job rather than this loop's. What stays here is the whole rest of
                # the turn — the budget ledger, the rollback gate, the cancellation teardown, the
                # metrics — because none of it was ever a property of which framework produced the
                # tokens, which is what made deleting the other engine a deletion rather than a
                # rewrite.
                async for event in _stream_into(
                    graph_events(
                        graph,
                        user_message,
                        config=graph_config,
                        trace=tool_trace,
                        on_signal=ledger.note_signal,
                        usage=ledger.usage,
                        exchanges=ledger.exchanges,
                    ),
                    ledger,
                ):
                    yield event
                # The stream is exhausted, so the graph has returned and the history provider has
                # committed this turn's rows as a complete, paired exchange. From here on a teardown
                # has nothing half-written to discard — set the fact the rollback gate reads at the
                # moment it becomes true, not at the answer, which is still a verifier call and
                # possibly a job-result wait away.
                ledger.run_complete = True
                # A tool call whose arguments finished on the *final* update has nothing following
                # it to close it out, so flush the trace before the answer. (Signals used to need
                # the same treatment and no longer do: they ride the stream itself, so the last one
                # is yielded by the same loop as every other.)
                for call in tool_trace.flush():
                    yield call
                async for event in _resume_on_job_results(
                    graph,
                    config=graph_config,
                    trace=tool_trace,
                    session=session,
                    ledger=ledger,
                ):
                    yield event
            capped = _loop_cap_event(session, ledger)
            if capped is not None:
                yield capped
            silent = _empty_answer_event(session, tool_trace, ledger)
            if silent is not None:
                yield silent
                # **`return`, not fall through**, which is what this did. `events.py` names
                # `loop_cap_reached` as the *only* error that shares its turn with an answer, and
                # falling through broke that for `empty_answer` in three ways at once: the client
                # got an `AnswerEvent` whose text is `""` (the reference page renders it as an empty
                # assistant bubble), `build_answer_event` spent a judge call under
                # `verifier_enabled` grading an empty string, and `answered = True` reached
                # `record_turn_cost(completed=answered)` — so the cost ledger booked "the user got
                # an answer for the money" for precisely the silent-death turn that branch exists to
                # name. The teardown below still books the spend and the duration, which is right:
                # the turn cost what it cost.
                return
            # Before the answer, because the answer is the turn's final event: a chemist reading
            # "review the plan and approve it" in the answer text used to have nothing to act on —
            # the decision routes and the surface's approval card both existed, and no turn ever
            # emitted the event that connects them.
            if plan_gated:
                pending = await _pending_plan_approval(session.session_id)
                if pending is not None:
                    yield pending
            answer = await build_answer_event(
                ledger.answer_text,
                tool_trace.outputs,
                tool_trace.called_tools,
            )
            await _record_transcript(
                history, session, user_message, ledger.answer_text, ledger.exchanges
            )
            # **Before the yield, not after it.** The turn's rows are committed by now and they are
            # a complete, paired exchange — there is nothing half-written left to undo. The
            # cancellation that reaches a finished turn is delivered *while suspended in the yield
            # below*, as sse-starlette sends the answer, so a flag set after it is still false
            # exactly when the teardown clause needs it to be true.
            ledger.answered = True
            yield answer
            # The turn used its authorization, so the authorization is spent (D-167). Here rather
            # than in the teardown, which also runs on the disconnect path where an `await` would
            # re-raise the cancellation and skip every teardown step after it — see
            # `consume_turn_approval`.
            if plan_gated:
                await consume_turn_approval(session.session_id)
        except (GeneratorExit, asyncio.CancelledError):
            _roll_back_unfinished(session, state_snapshot, ledger)
            raise
        except Exception as exc:
            yield _failure_event(exc, session, ledger)
            # A turn that spent the authorization and then broke has still spent it: tools may have
            # run before it failed, and re-running under the same approval is exactly what a person
            # would want asked about again.
            if plan_gated:
                await consume_turn_approval(session.session_id)
        finally:
            _book_turn_spend(ledger, session=session, actor=actor, profile=profile, budget=budget)


@dataclass(slots=True)
class _TurnLedger:
    """What one turn accumulates that more than one of its stages has to read.

    Extracted from `run_turn`'s locals because the stages below are the readers: the stream
    collector appends to `answer_parts`, the resume flips `run_complete` twice, the teardown reads
    `answered` and `usage`, and the two guard events read `correlation_id`. Passing eight locals
    between them would be the same coupling written out longhand, and a mutable object is what the
    original already was — a set of names in one frame that every branch could reach.

    **`answered` and `run_complete` are two questions, not one, and conflating them was a defect.**
    `answered` is the cost ledger's question ("did the user get an answer for the money") and
    becomes true only after the verifier and any mid-turn resume have run. `run_complete` is the
    rollback's predicate: it says the last model run returned, so there is no unfinished work left
    to disown, however much of it still lies between there and the `AnswerEvent`. Gating the
    rollback on `answered` undid finished runs whose teardown merely landed in one of those windows.
    """

    correlation_id: str
    usage: TurnUsage
    # Started at construction, which is `run_turn`'s first statement, so the duration this books is
    # the whole turn rather than the part after setup.
    started: float = field(default_factory=time.perf_counter)
    answered: bool = False
    run_complete: bool = False
    answer_parts: list[str] = field(default_factory=list)
    # Durable jobs this turn launched, for the optional mid-turn resume.
    started_jobs: list[str] = field(default_factory=list)
    # The tool-bearing messages this turn produced, for the transcript projection: the events carry
    # no call id, so a projection rebuilt from them could not pair a result with its call.
    exchanges: list[Any] = field(default_factory=list)

    @property
    def answer_text(self) -> str:
        """The supervisor's own prose, which is both the answer and what the transcript stores."""
        return "".join(self.answer_parts)

    def note_signal(self, signal: Any) -> None:
        """Record a durable job launch, so the resume below knows what to wait for.

        A method rather than the lambda this was, because the resume passes a *different* callback
        deliberately (a no-op) and a named pair reads as the decision it is rather than as one
        lambda that lost its body.
        """
        if isinstance(signal, JobSignal):
            self.started_jobs.append(signal.job_id)


@contextmanager
def _turn_ambient(
    session_id: str,
    actor: str | None,
    roles: frozenset[str],
    dry_run: bool,
    correlation_id: str,
    usage: TurnUsage,
) -> Iterator[None]:
    """Stamp the six ambients a turn runs under, and unstamp every one on the way out.

    **Synchronous on purpose, and that is the point of extracting it.** These resets used to sit at
    the bottom of `run_turn`'s `finally`, under a comment warning that nothing in that block may
    `await` — because the disconnect path reaches it by cancellation rather than `aclose()` (D-130),
    and an `await` there re-raises the cancellation on the spot and skips everything below it,
    leaking one turn's ambient identity into the next turn on this worker. A `with` block cannot
    acquire an `await` between the last statement and the reset, so the rule is now structural
    rather than a comment somebody has to keep obeying.

    Each of the five, and why it is ambient rather than an argument:

    - the session, so a job-launching tool records push-back to the right session (F3-T3) — never a
      model-supplied argument;
    - the authenticated identity (F4), so audit, authorization and attribution see the user;
    - one correlation id per *turn*, generated by the caller and stamped here. Not bound inside
      `build_langgraph_agent`: agents are cached per profile for the process's lifetime, so a
      build-time id was shared by every turn from every user on the pod, and the audit trail could
      not tell two conversations apart;
    - the tool-call counter, so the identical question asked a third time is refused rather than
      re-executed (`chemclaw.agent.repeat_guard`);
    - the loop watch, so a turn stopped by the runaway cap can say so instead of looking exactly
      like one that finished (`chemclaw.agent.loop_cap`). A no-op without the harness, which is what
      attaches the cap;
    - the token ledger, so a model call that rides no stream can still be booked against this turn.
      Every call the graph makes is metered off its `messages` stream — including the ones a tool
      body makes, which inherit the graph's callbacks — but the verifier's judge runs *after* that
      stream is exhausted, so its tokens reached neither the budget guard nor the `turn_costs` row.
      Ambient rather than threaded, because that call sits three frames below `build_answer_event`
      inside a provider's own chain (`chemclaw.agent.turn_usage.off_stream_metering`).

    `dry_run` rides here too for the reason it is ambient at all: the model can neither set it nor
    clear it (IDEA-4).

    Reset order is the reverse-ish order the original spelled out and is preserved exactly: the two
    watches, the dry-run flag, then the three identity vars. `set_current_identity` is skipped
    entirely when there is no actor, so the unauthenticated path stamps nothing to reset.
    """
    session_token = set_current_session_id(session_id)
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    correlation_token = set_current_correlation_id(correlation_id)
    calls_token = begin_call_watch()
    loop_token = begin_loop_watch()
    usage_token = set_turn_usage(usage)
    dry_run_token = set_dry_run(dry_run)
    try:
        yield
    finally:
        _unstamp(session_id, end_call_watch, calls_token)
        _unstamp(session_id, end_loop_watch, loop_token)
        _unstamp(session_id, reset_turn_usage, usage_token)
        _unstamp(session_id, reset_dry_run, dry_run_token)
        _unstamp(session_id, reset_current_session_id, session_token)
        _unstamp(session_id, reset_current_correlation_id, correlation_token)
        if identity_token is not None:
            _unstamp(session_id, reset_current_identity, identity_token)


def _unstamp(session_id: str, reset: Callable[[Any], None], token: Any) -> None:
    """Undo one ambient, tolerating a token whose `Context` is not the one closing the turn.

    A contextvar `Token` remembers the `Context` it was created in, and one teardown path closes
    the turn from somewhere else: when a client stops reading, the turn's generator is abandoned
    at a `yield` and asyncio's async-generator finalizer runs `aclose()` in a *new task with a new
    context*. Every reset then raises `ValueError` — and the first one aborted the five after it,
    including `reset_current_identity`, while surfacing as an unretrieved-task traceback naming a
    `ContextVar` and no session.

    Tolerating it loses nothing: the context those tokens belong to is being discarded either way,
    so the values are gone whether or not the reset lands. What is gained is that the *rest* of the
    teardown runs, and that the log line names the session. Only `ValueError` — anything else from
    a reset is a real defect and must not be swallowed.
    """
    try:
        reset(token)
    except ValueError:
        logger.warning(
            "the turn for session %s was torn down in a foreign context; "
            "its ambient %s could not be reset",
            session_id,
            reset.__name__,
        )


async def _open_turn_surface(
    stack: AsyncExitStack, connectors: Sequence[Any] | None
) -> tuple[list[Any], list[str]]:
    """Open this turn's out-of-process capability, and name whatever did not answer.

    This turn's own connector tools are connected for its duration and torn down after. Built per
    turn rather than held on the agent because a connector's connection must belong to exactly one
    turn — see `chemclaw.connectors.transport`. The graph binds them alongside the profile's
    in-process tools at construction, so the model sees one combined surface. An unreachable
    connector costs its tools, not the turn.

    Opening returns the tools as well as the casualties because a connector's tools do not exist
    until its session is live — `load_mcp_tools` needs an open session — which is why this is not
    "open these and reuse the list you passed in".

    **The durable subsystem is announced the same way and for the same reason.** It was not, and
    connectors were: Temporal was never probed, so a turn whose every durable launcher was going to
    fail planned exactly like a turn that could run one. Measured in the 190-probe live run: 0 of 7
    durable launchers ran, and the model repeatedly read the launch failure as bad input from the
    chemist and re-asked for parameters it already had.

    The caller announces the result before the first token rather than discarding it (REV-6): the
    model cannot tell the chemist that a tool was missing, because it never saw one missing — it
    answers from the surface it was handed. Only this layer knows the surface was short, and only
    before the first token does the model get to plan against the surface it will actually get.

    Returns:
        The turn's bound tools, and the names of every capability that did not answer.
    """
    turn_tools, unreachable = await open_connector_specs(
        stack, connectors if connectors is not None else connector_specs()
    )
    if not await _durable_subsystem_reachable():
        unreachable = [*unreachable, _DURABLE_SUBSYSTEM]
    return turn_tools, unreachable


async def _stream_into(events: AsyncIterator[Event], ledger: _TurnLedger) -> AsyncIterator[Event]:
    """Re-yield a graph stream unchanged, collecting the supervisor's own tokens as the answer.

    One definition because the turn streams twice — the model run and the mid-turn resume — and
    both halves of the answer are built from `answer_parts`. Written out twice, the resume could
    silently stop collecting and the turn would answer with only its first half.

    **`not event.agent` is the whole filter, and it is load-bearing.** A specialist's tokens stream
    to the surface for the trace and are *not* the answer. Concatenating them would interleave one
    agent's working prose with the supervisor's, in the durable transcript as well as on screen.
    """
    async for event in events:
        if isinstance(event, TokenEvent) and not event.agent:
            ledger.answer_parts.append(event.text)
        yield event


async def _resume_on_job_results(
    graph: Any,
    *,
    config: dict[str, Any],
    trace: ToolCallTrace,
    session: TurnSession,
    ledger: _TurnLedger,
) -> AsyncIterator[Event]:
    """Continue this same turn with the results of the durable jobs it launched (gap AGT-2).

    If this turn launched durable jobs, optionally wait for them and continue the *same* turn with
    their results, so "compute this, then reason about the result" is one exchange rather than two.
    Off by default; bounded by config and, above it, by the front door's whole-turn deadline. Yields
    nothing at all when the feature is off, no job was launched, or none finished in time — which is
    why the caller can loop over it unconditionally.

    A second `graph_events` over the *same* graph and the same `thread_id`, because the continuation
    has to see the conversation the first half produced.

    **`on_signal` is a no-op here rather than the ledger's appender, deliberately.** A resume that
    fed its own job ids back into `started_jobs` would be the recursion this feature is without, so
    that one chemist turn cannot chain durable jobs indefinitely inside a single request.

    `run_complete` is cleared for the duration and set again after: the resume drives a *second*
    model run, which can half-write exactly like the first — so the exchange is incomplete again
    until it returns, and a teardown landing inside it must roll the turn back after all.
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
            on_signal=lambda _signal: None,
            usage=ledger.usage,
            exchanges=ledger.exchanges,
        ),
        ledger,
    ):
        yield event
    ledger.run_complete = True


def _loop_cap_event(session: TurnSession, ledger: _TurnLedger) -> ErrorEvent | None:
    """Say out loud that the runaway guard fired, or `None` if it did not.

    The harness loop still had work it wanted to do and its iteration cap stopped it
    (`chemclaw.agent.loop_cap`). Said before the answer, for the same reason
    `CapabilityDegradedEvent` precedes the tokens — the answer that follows is whatever the last
    iteration managed, and a surface must be able to mark it partial rather than present it as the
    finished work.

    The turn is not failed by this: the answer still goes out, and the ledger still bills it as
    completed. `loop_cap_reached` is the one error `events.py` names as sharing its turn with an
    answer.
    """
    if not loop_hit_cap():
        return None
    METRICS.increment("chemclaw_turn_loop_caps_total")
    logger.warning(
        "the harness loop for session %s hit its %d-iteration cap with work still open",
        session.session_id,
        settings.harness_max_loop_iterations,
    )
    return ErrorEvent(
        message=(
            f"The turn reached its {settings.harness_max_loop_iterations}-iteration limit "
            "and stopped with work still open, so the answer below is partial "
            f"(session {session.session_id})."
        ),
        code="loop_cap_reached",
        # Not retryable unchanged: the same request drives the same loop into the same cap. The
        # useful next step is a narrower request, not another 25 iterations.
        retryable=False,
        correlation_id=ledger.correlation_id,
    )


def _empty_answer_event(
    session: TurnSession, trace: ToolCallTrace, ledger: _TurnLedger
) -> ErrorEvent | None:
    """Name a turn that produced no prose at all, or `None` if it produced some.

    A turn that produced no prose is a *silent* failure, and it must not be one.

    There is already a guard for the harness loop hitting its cap, but that path only runs with
    `harness_enabled` — and the case measured on 2026-08-04 had the harness off: du-03 made 29 tool
    calls (find_past_jobs ×8, load_skill ×6, find_notes ×5, …), never reached the capability the
    question needed, and ended with an empty `AnswerEvent` after 197 s. No error, no tokens, nothing
    to read. `evals.live` scores exactly this as `failed_loudly=False` because it is the worst shape
    a turn can take: a user cannot retry what never said it went wrong, and every prior live pass
    has found one (`docs/archive/vibe-test-2026-07`).

    An `ErrorEvent` rather than inventing an answer: the system genuinely has nothing to say, and
    saying so is the honest outcome. Retryable, unlike the loop cap — a turn that spent its budget
    circling retrieval may well succeed on a narrower question, and the message says so.
    """
    if ledger.answer_text.strip():
        return None
    METRICS.increment("chemclaw_turn_empty_answers_total")
    logger.warning(
        "turn for session %s ended with no answer text after %d tool call(s)",
        session.session_id,
        len(trace.called_tools),
    )
    return ErrorEvent(
        message=(
            "The turn ended without producing an answer, after "
            f"{len(trace.called_tools)} tool call(s). Nothing was written, so "
            "there is nothing below to read — this is a failure, not an empty result. "
            "A narrower or more specific question is the useful next step "
            f"(session {session.session_id})."
        ),
        code="empty_answer",
        retryable=True,
        correlation_id=ledger.correlation_id,
    )


async def _pending_plan_approval(session_id: str) -> ApprovalRequestEvent | None:
    """The approval this session is waiting on, or `None` when it is not waiting on one.

    Emitted at the end of a plan-gated turn — the moment the plan is settled and committed —
    whenever the session's current plan is non-empty and holds no live approval. That is the exact
    condition under which the gate will refuse every state-changing call of the next turn, so it is
    the moment a surface owes the chemist the decision card: `ApprovalRequestEvent` documented an
    empty `approval_id` as the plan-approval shape and the reference surface mounts its card on it,
    but nothing ever produced the event, so under `plan_only` the chemist saw a plan and a refusal
    and no way to act on either.

    Reads the same sources the gate and `consume_turn_approval` read — `session_todos` for the
    plan, `plan_identity` for its hash, `approval_stands` for the decision — so the prompt cannot
    disagree with the enforcement about whether the session is actually blocked. An *approved*
    plan whose turn just executed does not prompt: the check runs before the turn's approval is
    consumed, and the next turn re-prompts if the chemist asks for more work under the now-spent
    decision.

    Never raises, mirroring `consume_turn_approval`: an unreadable plan must not fail a turn that
    already has its answer. The gate still refuses on the next call regardless, so the cost of
    staying silent here is one missing card, not one missing control.
    """
    try:
        todos = await session_todos(session_id)
        if todos is None:
            return None
        plan_hash = plan_identity(todos)
        if plan_hash is None:
            return None
        if await approval_stands(session_id, plan_hash):
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


def failure_event(exc: Exception, session_id: str, correlation_id: str) -> ErrorEvent:
    """One failed turn as one user-safe, classified event — never a leaked trace.

    Public, and takes ids rather than a session and a ledger, because the *route* is the second
    caller: everything `chemclaw.api.routes.turns` evaluates to call `run_turn` (the connector
    factory, the history provider, the graph factory) runs one frame above every handler this
    module owns, and a failure there used to end the stream with no event at all — the shape
    `empty_answer` exists to eliminate, reproduced one layer up. There is exactly one way a turn
    stream reports a failure, so the two sites cannot disagree about a code or about what is
    disclosed.

    The exception detail (DB hosts, SMILES, workflow ids, driver errors) stays server-side in the
    caller's log; the client gets the classification plus the correlation id the audit trail is
    keyed on, so a bug report is findable without leaking internals.
    """
    code, retryable = _classify(exc)
    return ErrorEvent(
        message=(
            f"The turn could not be completed due to an internal error (session {session_id})."
        ),
        code=code,
        retryable=retryable,
        correlation_id=correlation_id,
    )


def _failure_event(exc: Exception, session: TurnSession, ledger: _TurnLedger) -> ErrorEvent:
    """`failure_event` for a turn that is already running, with this turn's log line beside it."""
    logger.exception("turn failed for session %s", session.session_id)
    return failure_event(exc, session.session_id, ledger.correlation_id)


def _roll_back_unfinished(
    session: TurnSession, snapshot: dict[str, Any], ledger: _TurnLedger
) -> None:
    """Undo the bookkeeping of a turn torn down before its exchange completed.

    The turn is being torn down from outside — the client went away, or the front door's wall-clock
    deadline expired. `session.state` is the harness's own bookkeeping — the todo list, the plan
    hash, the approval marks — and a turn torn down half-way through has advanced it for work that
    never finished, so the next turn would read a plan claiming steps it never took. A half-written
    turn is worth less than the conversation it would otherwise poison.

    **`CancelledError` belongs in the caller's clause beside `GeneratorExit`, and its absence made
    that clause dead code on the only path that matters** (D-130). sse-starlette answers
    `http.disconnect` by cancelling its task group; it never calls `aclose()` on the body iterator,
    so a real disconnect delivers `CancelledError` and this rollback was skipped every single time.
    It looked covered because the suite closed the stream by hand — the one thing production does
    not do. Measured on a live front door: the agent's stream received `CancelledError`, never
    `GeneratorExit`.

    **Only a turn whose exchange is incomplete is rolled back.** Once the last model run returned,
    the history provider committed a complete user+assistant pair and no `tool_use` is left without
    its result — the sole failure the rollback exists to prevent. Undoing it anyway deleted a
    finished exchange from the conversation because the client dropped during the send of its
    answer. A silently vanished answer is worse than a lost turn. (The spent-plan marker used to
    ride along in that snapshot, so reverting an answered turn's state re-armed the approval it had
    just used as well; consumption is a durable column now — `plan_approvals.consumed_at` — so the
    committed exchange alone is the reason, which is the reason that was always sufficient.)

    The predicate is `run_complete`, not `answered`, and the gap between them is real time: after
    the stack closes the turn still awaits loop-cap reporting, an optional job-result wait plus
    resume, and the verifier's judge call — and `answered` only becomes true after all of them. A
    teardown landing in any of those windows used to take the rollback branch and delete an exchange
    the model run had already committed complete and correctly paired — the exact outcome this
    docstring says must not happen. `answered` is kept beside it for the cost ledger, whose question
    genuinely is "did the user get an answer".

    **Only `session.state` is rolled back, and that is the whole rollback now.** It used to have a
    durable half: a pre-turn watermark over `session_messages`, because the previous engine wrote
    the stored thread incrementally and fed it back to the model, so a disconnect mid-tool-call
    committed a `tool_use` with no matching `tool_result` and every later turn replayed it — the
    model rejected the thread outright ("tool_use ids found without tool_result blocks") and one
    dropped connection permanently bricked the conversation. The graph reads its own checkpointer
    instead, and `_record_transcript` writes the user message and the answer together in one call
    once the answer exists, so the transcript is written once, after the answer: a teardown either
    lands before it and leaves nothing behind, or lands after it and finds a complete exchange.
    There is no third outcome (D-2026-08-10 §2).
    """
    if ledger.answered or ledger.run_complete:
        logger.warning(
            "turn for session %s was torn down after its exchange completed (client "
            "disconnect or the front door's turn deadline); the committed turn is kept",
            session.session_id,
        )
        return
    logger.warning(
        "turn for session %s was torn down before it answered (client disconnect or the "
        "front door's turn deadline); rolling session state back",
        session.session_id,
    )
    session.state.clear()
    session.state.update(snapshot)


def _book_turn_spend(
    ledger: _TurnLedger,
    *,
    session: TurnSession,
    actor: str | None,
    profile: str | None,
    budget: BudgetTracker | None,
) -> None:
    """Book what the turn cost, on every path — success, failure and disconnect alike.

    **Nothing in here may `await`, and it is synchronous so that it cannot.** This runs on the
    disconnect path too, which production reaches by cancellation rather than `aclose()` (D-130) —
    an `await` here re-raises the cancellation on the spot and silently skips everything below it.

    The duration is observed on every path because a turn that failed after 40 s is exactly the
    sample an operator needs, and excluding it would make the histogram look best when the service
    is worst. The token counter is the same number the budget guard meters, published as a rate
    rather than only used to refuse.

    Labelled by profile (REV-10): "what is this costing" is only actionable once it can be
    attributed, and a narrowed profile is exactly the thing a deployment adopts to spend less.
    `default` rather than an absent label for a session on no profile, so every series carries the
    same label set and the sum over the family is the deployment's whole spend.

    `record_turn_cost` books the same numbers a second time against the identity the metric cannot
    carry. Not a duplicate: `core/metrics` refuses a counter past 64 label series (D-152) because a
    label value is attacker-influenced, and an Entra oid is exactly such a key — so per-actor spend
    needs a table, and the fleet-wide rate needs a counter. Booked here rather than on the success
    path so a turn torn down by a disconnect is billed too: that is the runaway this ledger exists
    to find, not an edge case to drop. It does not await — see its own docstring.
    """
    elapsed = time.perf_counter() - ledger.started
    if budget is not None:
        budget.record(session.session_id, actor, ledger.usage.total)
    METRICS.observe("chemclaw_turn_duration_seconds", elapsed)
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
            duration_seconds=elapsed,
            completed=ledger.answered,
        )
    )
    if ledger.usage.unreadable:
        # The provider reported usage and we could not read it, which is not the same as a provider
        # that reports none: this turn was metered at zero against a budget that is enabled by
        # default in the chart, so the cost guard is not binding. ERROR because the remedy is a code
        # change, and counted because a per-turn log line during an outage is noise that nobody
        # aggregates.
        logger.error(
            "usage_unreadable: %d usage content(s) carried no token count; this turn metered "
            "zero and the budget guard did not bind",
            ledger.usage.unreadable,
        )
        METRICS.increment("chemclaw_usage_unreadable_total", float(ledger.usage.unreadable))
    if ledger.usage.total:
        METRICS.increment("chemclaw_tokens_total", float(ledger.usage.total), spend_labels)
    # Published separately from the total because they are priced separately (REV-10). Each is
    # guarded so a provider that reports none of them leaves its counter untouched rather than
    # publishing a fabricated zero — the same rule `core.metrics` applies to gauges.
    for name, value in (
        ("chemclaw_input_tokens_total", ledger.usage.input),
        ("chemclaw_output_tokens_total", ledger.usage.output),
        ("chemclaw_cache_read_tokens_total", ledger.usage.cache_read),
        ("chemclaw_cache_write_tokens_total", ledger.usage.cache_write),
    ):
        if value:
            METRICS.increment(name, float(value), spend_labels)


async def _turn_checkpointer() -> Any:
    """The graph engine's checkpointer, or `None` where this deployment stores nothing durably.

    Gated on `session_store` rather than built unconditionally, and gated on the *same* setting
    `history_provider` reads — so the two engines agree about whether a conversation survives a pod
    restart instead of one of them deciding separately. A dev process or a test running on the
    in-memory store would otherwise have to reach Postgres to take a single turn, which is both a
    dependency it does not have and a claim about durability it cannot keep.

    Returns:
        A ready `AsyncPostgresSaver`, or `None` to keep turn state in the invocation.
    """
    if settings.session_store != "postgres":
        return None
    return await checkpointer()


async def _turn_store() -> Any:
    """This turn's durable memory store, or `None` where the deployment keeps none.

    Two gates, both necessary and neither redundant. `agent_memory_enabled` is the deployment's
    decision that agent-authored files may outlive a session at all; `session_store` is the same
    condition `_turn_checkpointer` reads, because the store shares the checkpointer's pool and a
    process on the in-memory store has no Postgres to put one in. Building it here rather than in
    `build_langgraph_agent` is what keeps that builder synchronous — the same seam the checkpointer
    already uses.

    The *third* gate is not here and that is deliberate: whether the turn has an actor is decided by
    `scratchpad_backend`, because that is where the namespace is computed and an actorless memory is
    one nobody could erase.

    Returns:
        A ready `AsyncPostgresStore`, or `None` for a turn with a scratchpad but no memory.
    """
    if not settings.agent_memory_enabled or settings.session_store != "postgres":
        return None
    return await memory_store()


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
        # DEBUG stays: an outage this probe finds is reported to the chemist on the stream, and
        # logging it at attention level once per turn would bury the connector sweep's own signal.
        #
        # The counter is new, and the comment that used to stand alone here was checkably false.
        # It said `open_reachable` "already logs and counts a degraded turn" — that counter is
        # `chemclaw_connectors_unreachable_total`, which reads `tool.is_connected` over *connector*
        # tools and never names Temporal. Measured at the shipped `log_level=INFO` with the broker
        # pointed at a dead port: the probe returned False, zero log lines were emitted, and
        # `METRICS.render()` was unchanged. So every chemist was being told durable jobs were
        # unavailable while nothing server-side said so — the dashboard read healthy until someone
        # opened a ticket. A counter is the right instrument precisely because the log line must
        # stay quiet: it aggregates per-turn noise into one alertable rate.
        logger.debug("the durable subsystem did not answer its health probe", exc_info=True)
        METRICS.increment("chemclaw_durable_unreachable_total")
        return False


def _job_results_message(results: dict[str, dict[str, Any]]) -> str:
    """The completed jobs, worded and framed as the message that continues the turn.

    A function of its own rather than a string inlined at the resume site, because it is the
    *decision* the resume carries: a chemist meets this text as the reason their turn continued,
    and it belongs beside the framing rule it depends on rather than buried in the turn loop.

    The results are handed to the model as *framed data*, not as an instruction: they arrive from
    a workflow, and the same injection discipline that applies to retrieved notes applies here
    (`chemclaw.agent.framing`).

    **Rendered as JSON, and it used to be a Python `repr`.** `f"- {job_id}: {payload}"` over a
    `dict` produced single-quoted keys, `None` and `True` — the exact form
    `chemclaw.templates.resolve._text` states it exists to avoid, for the same reason ("a Python
    repr with single quotes that a model has to guess at"), on the *higher*-traffic of the two
    paths. `default=str` keeps a stray datetime from failing a turn over formatting, and
    `sort_keys` makes one turn's rendering comparable with the next's.
    """
    summary = "\n".join(
        f"- {job_id}: {json.dumps(payload, sort_keys=True, default=str)}"
        for job_id, payload in results.items()
    )
    return (
        # "finished", not "completed", and the failure instruction is explicit. A row carrying
        # `status: failed` used to arrive under a sentence asserting the jobs had completed, and a
        # direct assertion of success outranks an unexplained status word — so the model narrated
        # the calculation as done, which is the outcome reporting failed jobs at all exists to
        # prevent.
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
) -> None:
    """Write this turn's exchange to the session transcript, best-effort.

    **The read model, and the reason it is written here rather than derived.** `session_messages`
    backs `GET /sessions/{id}/messages` — what a chemist sees after a reload — and it used to be
    filled as a side effect of the previous engine's history provider, which it called on every
    run. The graph keeps its thread in the checkpointer and calls no such hook, so when that engine
    went the table stopped being written at all: measured, a complete turn left **0 rows** while the
    same session accumulated 8 checkpoint rows. The conversation was never lost — the checkpointer
    is what the next turn reads — but the transcript route returned `[]` for every session.

    **Written from the turn's own text, which is the trade this being "the light option" names.**
    The alternative is projecting from the checkpoint stream, which survives a process that dies
    mid-turn because the checkpoint is already committed. This runs after the answer is assembled,
    so a turn killed before it answers leaves no transcript row — and that is the same exchange the
    teardown path deliberately rolls back anyway, so the two agree about what a half-turn is worth.

    **The tool exchanges are stored too, and leaving them out was a silent regression.** The route
    projects `tool_calls` and each call's `result_ref` out of these rows
    (`api/schemas._transcript`), so a transcript of only the question and the answer made both
    permanently empty: everything the agent *did* vanished on reload, and a stored result whose
    bytes were sitting in `tool_result_blobs` had no handle to fetch it by. The pairing needs the
    call ids, which exist only on the messages — hence `exchanges` rather than a rebuild from the
    events, which carry no id.

    **Best-effort, like every other write on this path.** A transcript is a rendering; no rendering
    is worth failing an answered turn over, which is the rule `chemclaw.api.tool_results` already
    states for stored tool results. An empty answer is not written at all: the turn yielded an
    `ErrorEvent` saying nothing was produced, and a blank assistant row would contradict it.
    """
    if history is None or not answer.strip():
        return
    if not hasattr(history, "save_messages"):
        # Duck-typed rather than isinstance-checked: `history` is whatever the caller injected —
        # the two real providers, a test's recorder, a fake that only reads — and a provider that
        # does not store is a configuration this path tolerates, not a fault to raise on.
        return
    session_id = session.session_id
    try:
        await history.save_messages(
            session_id,
            [
                HumanMessage(content=user_message),
                *(exchanges or []),
                AIMessage(content=answer),
            ],
            # `state` is where the in-memory provider keeps its thread, and the durable one
            # deliberately keeps nothing there. Passing it is what makes this one call correct
            # under both stores, which is the same reason the transcript route passes it on read.
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
