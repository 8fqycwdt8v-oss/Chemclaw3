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

from chemclaw.agent.audit import default_audit_sink
from chemclaw.agent.checkpointer import checkpointer
from chemclaw.agent.chemclaw_agent import connector_specs
from chemclaw.agent.context_budget import (
    begin_context_watch,
    current_context,
    end_context_watch,
)
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
    spend_approval_after_teardown,
)
from chemclaw.agent.plan_state import session_todos
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.scratchpad import memory_store
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.agent.spend_cap import (
    begin_spend_watch,
    end_spend_watch,
    spend_hit_cap,
    turn_billed_tokens,
)
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
    JobStartedEvent,
    TokenEvent,
    ToolCallEvent,
    ToolFailedEvent,
)
from chemclaw.api.graph_stream import graph_events
from chemclaw.api.runner_answer import build_answer_event
from chemclaw.api.runner_trace import ToolCallTrace
from chemclaw.api.tool_results import session_sink
from chemclaw.connectors.registry import open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import (
    get_current_correlation_id,
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.session_context import (
    reset_current_session_id,
    set_current_session_id,
)
from chemclaw.core.temporal_client import connect
from chemclaw.core.tracing import start_span
from chemclaw.core.turn_signals import JobSignal
from chemclaw.core.turn_text import reset_current_user_text, set_current_user_text

logger = logging.getLogger(__name__)

# What the durable subsystem is called when its outage is announced. `CapabilityDegradedEvent`
# carries a list of *connector* names today, and this is not a connector — it is the whole durable
# execution layer, so every connector's jobs are down with it. It rides in the same list because
# what a surface does with the name is identical (say this capability is missing this turn), and a
# second event type for one more unreachable capability would be a contract change for no
# additional meaning. The name is prefixed so it cannot be mistaken for a bundle in the registry.
_DURABLE_SUBSYSTEM = "durable-jobs (Temporal)"

#: How a turn ended, as a closed set with exactly one producer (`_settle_outcome`).
#:
#: **The record it replaces was one boolean.** `turn_costs.completed` is `answered`, so six endings
#: collapsed into two: a turn that hit the runaway cap, one that produced no prose at all, one that
#: raised, one the wall clock killed and one the client abandoned were all simply "not completed",
#: and a turn that answered partially after the cap was "completed" beside a clean one. `completed`
#: stays and is derived from this (see `TurnCost.completed` at the call site), because it is what a
#: shipped dashboard already reads.
#:
#: **Six values and not nine, and the three that are missing are missing on purpose.** A turn
#: refused for budget, shed by admission, or 409'd by a concurrent turn never reaches `run_turn` at
#: all — nothing was spent, so there is no cost row for them to be the outcome *of* — and each
#: already has its own counter (`chemclaw_turns_refused_budget_total`,
#: `chemclaw_turns_shed_total`, `chemclaw_turns_conflict_total`). Adding them here would publish a
#: second answer to a question already answered, and would break the pairing an operator reads
#: `chemclaw_turns_started_total` against, since all three happen *before* a turn is started.
_OUTCOMES = (
    "answered",
    "loop_capped",
    "spend_capped",
    "empty_answer",
    "errored",
    "timed_out",
    "abandoned",
)


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
    deadline: float | None = None,
) -> AsyncIterator[Event]:
    """Run one turn and yield its events (tokens, tool calls, jobs, then the answer).

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
        deadline: The event-loop clock reading the caller's whole-turn `asyncio.timeout` will fire
            at (`asyncio.timeout(...) as t` → `t.when()`), used for one thing: telling a turn the
            wall clock killed from one somebody stopped. Both cancellations arrive here as the same
            `CancelledError`, and the caller learns which it was only in its own
            `except TimeoutError` — which runs *after* this turn's cost row is booked, so it cannot
            tell us afterwards. Comparing against the same loop clock the timeout itself uses makes
            the answer exact rather than a tolerance. `None` off the front door, where nothing sets
            a whole-turn deadline and every cancellation is genuinely an abandonment.

    Yields:
        `chemclaw.api.events.Event` values in the order the model produced them, ending with an
        `AnswerEvent` on success or an `ErrorEvent` on failure.
    """
    # **The request's id where there is one, a fresh one where there is not.** This used to mint
    # unconditionally, so a turn ran under an id nothing outside the process had ever seen: the
    # front door now returns `X-Chemclaw-Correlation-Id` on every response, and a chemist quoting it
    # would have found no `turn_costs` row, no `audit_events` row and no matching log line — two
    # ids for one event, which is the failure a correlation id exists to prevent. The pump task the
    # turn runs on copies the request's context at creation (`api/detach.DetachableTurn`), so the
    # ambient id is the *request's*, and adopting it makes the header, the access log, the audit
    # trail and the cost ledger one join. `None` off the request path (the CLI, a test, a template
    # step) mints as before.
    ledger = _TurnLedger(
        correlation_id=get_current_correlation_id() or uuid.uuid4().hex,
        usage=TurnUsage(),
        deadline=deadline,
    )
    # Whether this turn's approval is spendable, asked exactly as `build_langgraph_agent` asks
    # whether to attach the gate — one predicate, so the two cannot disagree about a profile that
    # overrides the deployment's autonomy.
    plan_gated = gate_applies(get_profile(profile))
    # Snapshot the session state before the turn so a client disconnect can roll it back
    # (ISSUE-B-10). What the snapshot is for, and why only `session.state` is in it, is in
    # `_roll_back_unfinished`.
    state_snapshot = copy.deepcopy(session.state)
    # Bound before the try so the teardown clause below can read it whatever point the turn died
    # at — a cancellation during the connector open arrives before the real trace is built.
    tool_trace: ToolCallTrace | None = None
    with (
        _turn_ambient(
            session.session_id,
            actor,
            roles,
            dry_run,
            ledger.correlation_id,
            ledger.usage,
            user_message,
        ),
        start_span(
            "chemclaw.turn",
            **{
                "session.id": session.session_id,
                "profile": profile or "",
                # **The join key, and the one attribute that was missing.** `correlation.id`
                # ties this span to `audit_events`, `turn_costs` and `session_messages`, and to
                # every log line the turn emits; without it a trace and the rows describing one
                # turn could only be matched by timestamp. `actor` is the other half of "whose
                # turn was this" — an identifier, not content, the rule `start_span` states.
                "correlation.id": ledger.correlation_id,
                "actor": actor or "",
            },
        ),
    ):
        # **The span wraps the whole body, and it used to end before the turn did.** It was pushed
        # onto the `AsyncExitStack` below with a comment claiming "the span's lifetime is exactly
        # the turn's teardown"; the stack closes when the model stream is exhausted, and everything
        # after that ran outside it — the loop-cap and empty-answer guards, the plan-approval read,
        # `build_answer_event` (which under `verifier_enabled` makes a *second LLM call*),
        # `_record_transcript`, the audit flush, and the `yield` where a client disconnect actually
        # lands. Measured with the shipped chart's settings (`OTEL_LLM_SPANS=true` + the verifier):
        # the judge call opened its own **root span with a different trace id**, so every turn
        # emitted a second orphan trace, and the turn's traced duration understated the chemist's
        # wait by the length of the judge call. A `with` around the body rather than a stack entry,
        # because the body is what the span is measuring.
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
            async with AsyncExitStack() as stack:
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
                # Built here rather than left to `build_langgraph_agent`'s own default for one
                # reason: the durable sink batches its writes off the tool-call path, and the
                # turn-end flush below needs the object to drain. Same sink either way —
                # `default_audit_sink()` is exactly what the builder would have called.
                audit_sink = default_audit_sink()
                graph = graph_factory(
                    profile=profile,
                    actor=actor or "",
                    correlation_id=ledger.correlation_id,
                    audit_sink=audit_sink,
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
                # What finished while nobody was looking reaches the *model*, not only a browser
                # tab that may not be open. `claim_unconsumed` had exactly one consumer — the SSE
                # push-back stream — so a chemist who closed the tab lost the notification and the
                # model never learned its own job finished: the flagship "compute then reason"
                # exchange required the user to re-prompt and the model to remember the job id.
                # The claim is atomic, so a live tab's tailer and this turn cannot both deliver
                # one row; whichever asks first wins, and both audiences are told the same way.
                user_input = await _with_pushed_job_results(session.session_id, user_message)
                async for event in _stream_into(
                    graph_events(
                        graph,
                        user_input,
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
                    # Counted here as well as yielded, because this is the one path a tool call
                    # reaches the surface on without passing through `_stream_into` — the trace's
                    # tail, for a call whose arguments finished on the final update. Left out, the
                    # turn record would under-count exactly the calls that ran last.
                    ledger.note_event(call)
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
            overspent = _spend_cap_event(session, ledger)
            if overspent is not None:
                yield overspent
            silent = _empty_answer_event(session, tool_trace, ledger)
            if silent is not None:
                yield silent
                # **`return`, not fall through**, which is what this did. `events.py` names the
                # two cap errors as the ones that share their turn with an answer, and
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
            # Drain the turn's buffered audit rows before answering, so "the turn is done" also
            # means "its trail is queryable" — the off-path batching in `PostgresAuditSink` makes
            # the write eventually-consistent otherwise, and one batched write here costs
            # milliseconds where ninety inline ones cost the turn. Duck-typed because the
            # `AuditSink` protocol is `record` alone and only the batching sink has anything to
            # drain; on the disconnect path the flusher task simply finishes on its own.
            sink_flush = getattr(audit_sink, "flush", None)
            if sink_flush is not None:
                await sink_flush()
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
            # Marked before anything else in this clause, because `_book_turn_spend` in the
            # `finally` is what turns it into `timed_out` or `abandoned` and it must not depend on
            # the rollback below having run.
            ledger.cancelled = True
            # Sampled here, beside the flag it qualifies, because this is the only instant at
            # which the reading is exact — see `_TurnLedger.timed_out`. Everything below this line
            # takes time, and the deadline does not wait for it.
            ledger.timed_out = _deadline_passed(ledger.deadline)
            _roll_back_unfinished(session, state_snapshot, ledger)
            # A torn-down turn that already *acted* has used its authorization: durable jobs, note
            # proposals and calibration rows are not rolled back by the teardown, so leaving the
            # approval live made "drop the connection after the tools ran" a way to act under one
            # approval twice. Spent on a task of its own because an `await` here re-raises the
            # cancellation and skips the teardown after it (`spend_approval_after_teardown`). A
            # turn that only *read* still keeps its approval — the one-turn residual D-167 accepts.
            if plan_gated and tool_trace is not None and _turn_acted(tool_trace):
                spend_approval_after_teardown(session.session_id)
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
    collector appends to `answer_parts` and counts into `note_event`, the resume flips
    `run_complete` twice, the two guard events read `correlation_id`, and the teardown reads
    **the whole record** — `answered` and `usage`, and with them `error_code`, `cancelled`,
    `timed_out`, `loop_capped`, the four counts and `first_token`, because `_book_turn_spend`
    turns this whole object into one `turn_costs` row. Passing that many locals between them would
    be the same coupling written out longhand, and a mutable object is what the original already
    was — a set of names in one frame that every branch could reach.

    **`answered` and `run_complete` are two questions, not one, and conflating them was a defect.**
    `answered` is the question `turn_costs.completed` asks ("did the user get an answer for the
    money"), and it is still asked of this flag directly rather than derived from `outcome` — see
    the `completed=` argument in `_book_turn_spend`, where deriving it was tried and reverted
    because a loop-capped turn does deliver its partial answer. It becomes true only after the
    verifier and any mid-turn resume have run. `run_complete` is the rollback's predicate: it says
    the last model run returned, so there is no unfinished work left to disown, however much of it
    still lies between there and the `AnswerEvent`. Gating the rollback on `answered` undid
    finished runs whose teardown merely landed in one of those windows. *How* the turn ended is a
    third question again, and `outcome` is the six-valued answer to it.
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
    # --- what the turn record is made of (`_settle_outcome`, `_book_turn_spend`) ---------------
    # Set by the failure branch, from the same `_classify` that words the client's error event. It
    # was computed, sent to the user, and thrown away server-side: a chemist could quote a code the
    # deployment had no record of.
    error_code: str = ""
    # Set by the runaway guard as it emits its event, so the teardown does not have to re-ask a
    # contextvar whose watch is torn down one frame later.
    loop_capped: bool = False
    # The same, for the spend guard. Two flags rather than one "capped" with a reason, because
    # `_OUTCOMES` is what a turn record stores and an operator groups by: a deployment whose turns
    # are too *expensive* and one whose turns are too *long* need different fixes, and one value
    # covering both would need the reason as a second column to be actionable at all.
    spend_capped: bool = False
    # Set by the cancellation clause. Distinguishing the two cancellations is what `deadline` is
    # for; without it a wall-clock kill and a stop are the same ending.
    cancelled: bool = False
    # The event-loop clock reading the caller's whole-turn `asyncio.timeout` fires at, or `None`
    # when the caller set none (the CLI, a test). Not a duration and not a heuristic: the timeout
    # and this comparison read the *same* clock, so at the instant the cancellation is delivered
    # `loop.time() >= deadline` is true for a timeout and false for a Stop. The caller cannot
    # simply tell us afterwards — its own `except TimeoutError` runs after this ledger is booked.
    deadline: float | None = None
    # Whether the clock had already passed `deadline` **at the instant the cancellation arrived**,
    # recorded there rather than re-derived at teardown.
    #
    # `_settle_outcome` used to compare against the clock itself and called that "an exact test
    # rather than a tolerance". It was exact about the wrong instant: it runs in the `finally`,
    # *after* `_roll_back_unfinished` and after an approval spend, so a Stop delivered at
    # `deadline − ε` behind a slow teardown crossed the deadline while being torn down and booked
    # `timed_out`. Sampling in the `except` clause, one line after `cancelled = True`, is what
    # makes the claim true: there, the two cancellations really are separated by construction.
    timed_out: bool = False
    # When this turn's *answer* first began, as a `perf_counter` reading. **The number a chemist
    # actually experiences**, and nothing measured it: `chemclaw_turn_duration_seconds` is the whole
    # turn, so a turn that spent 40 s on tools and then streamed instantly and one that stalled for
    # 40 s before its first word were the same sample. `None` means no token of the answer was ever
    # produced, which is a different fact from "0 seconds" and is stored as one.
    #
    # **A subagent's token is not this turn's first token**, and taking any `TokenEvent` made two
    # fields of one row contradict each other: `answer_parts` collects only `not event.agent`
    # (`_stream_into`, where that filter is called load-bearing), so a turn in which only a
    # subagent ever spoke booked `outcome="empty_answer"` beside a non-null `ttft_seconds` — a
    # time-to-first-token for an answer that never had a first token. One definition of "a token
    # of this turn", used by both readers.
    first_token: float | None = None
    tool_calls: int = 0
    tool_failures: int = 0
    tool_refusals: int = 0
    jobs_started: int = 0

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

        Off the events rather than off the trace or the graph, because the events are the only
        thing every path shares: the model run, the mid-turn resume and a subagent's work all pass
        through here, and `ToolCallTrace` deliberately knows nothing about refusals. A refusal is a
        `ToolFailedEvent` carrying `reason`, which `agent/plan_gate.plan_gate_failure_reason`
        already classifies from the exception *class* — so counting it here reuses that decision
        instead of making a second one.
        """
        if isinstance(event, TokenEvent):
            # `not event.agent` — the same filter `_stream_into` applies to `answer_parts`, for
            # the same reason it gives. See `first_token`.
            if self.first_token is None and event.text and not event.agent:
                self.first_token = time.perf_counter()
        elif isinstance(event, ToolCallEvent):
            self.tool_calls += 1
        elif isinstance(event, ToolFailedEvent):
            if event.reason is None:
                self.tool_failures += 1
            else:
                self.tool_refusals += 1
        elif isinstance(event, JobStartedEvent):
            self.jobs_started += 1

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
    user_text: str,
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
    clear it (IDEA-4). `user_text` — the chemist's message for this turn — rides here for exactly
    that reason and no other: `protocols` checks a `basis="stated"` quote against it, and a haystack
    the model supplies is a haystack the model can invent (`core.turn_text`).

    Reset order is the reverse-ish order the original spelled out and is preserved exactly: the two
    watches, the dry-run flag, then the three identity vars. `set_current_identity` is skipped
    entirely when there is no actor, so the unauthenticated path stamps nothing to reset.
    """
    session_token = set_current_session_id(session_id)
    user_text_token = set_current_user_text(user_text)
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    correlation_token = set_current_correlation_id(correlation_id)
    calls_token = begin_call_watch()
    # The turn's context record, started beside the tool-call counter because it is the same kind
    # of thing: per-turn state the middleware writes and the teardown reads. Without it compaction
    # reports every model call's standing reduction as a fresh one, and `turn_costs` cannot say
    # whether the policy touched the turn at all (`agent/context_budget.py`).
    context_token = begin_context_watch()
    loop_token = begin_loop_watch()
    spend_token = begin_spend_watch()
    usage_token = set_turn_usage(usage)
    dry_run_token = set_dry_run(dry_run)
    try:
        yield
    finally:
        _unstamp(session_id, end_call_watch, calls_token)
        _unstamp(session_id, end_context_watch, context_token)
        _unstamp(session_id, end_loop_watch, loop_token)
        _unstamp(session_id, end_spend_watch, spend_token)
        _unstamp(session_id, reset_turn_usage, usage_token)
        _unstamp(session_id, reset_dry_run, dry_run_token)
        _unstamp(session_id, reset_current_user_text, user_text_token)
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
    # Gathered, not sequential: the connector open and the Temporal probe share nothing, and both
    # sit on the pre-first-token path of every turn — run one after the other they *add*, so a
    # slow broker taxed even a turn whose connectors answered instantly.
    (turn_tools, unreachable), durable_up = await asyncio.gather(
        open_connector_specs(stack, connectors if connectors is not None else connector_specs()),
        _durable_subsystem_reachable(),
    )
    if not durable_up:
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
        # The turn record's counts and its time-to-first-token, taken here because this is the one
        # point every event of both streams passes through — see `_TurnLedger.note_event`.
        ledger.note_event(event)
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
    completed. `loop_cap_reached` is one of the two errors `events.py` names as sharing its turn
    with an answer; `_spend_cap_event` is the other.
    """
    if not loop_hit_cap():
        return None
    # Marked on the ledger as well as counted, because the teardown reads it after
    # `_turn_ambient` has torn the watch down — `loop_hit_cap()` would answer False by then.
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
            "and stopped with work still open, so the answer below is partial "
            f"(session {session.session_id})."
        ),
        code="loop_cap_reached",
        # Not retryable unchanged: the same request drives the same loop into the same cap. The
        # useful next step is a narrower request, not another 25 iterations.
        retryable=False,
        correlation_id=ledger.correlation_id,
    )


def _spend_cap_event(session: TurnSession, ledger: _TurnLedger) -> ErrorEvent | None:
    """Say out loud that the turn ran out of budget mid-flight, or `None` if it did not.

    `_loop_cap_event`'s sibling in the unit that costs money, and it is a separate event rather
    than a second reason for that one because the two are different things for a chemist to do. A
    turn that hit its iteration cap was *planning* more work than a turn can close, and the useful
    next step is a narrower request. A turn that hit its spend cap may have had a perfectly small
    plan and drowned it in tool output, and the useful next step may instead be a narrower corpus,
    a smaller result, or an operator raising `agent_max_turn_billed_tokens`. Collapsing them would
    tell a surface "a guard fired" and leave it unable to say which.

    The number is in the message because "the turn stopped" and "the turn stopped after 1.2 million
    tokens against a 1 million budget" are different messages, and only the second one lets a
    chemist judge whether the request or the ceiling was wrong.

    Not retryable unchanged, for `_loop_cap_event`'s reason: the same request spends the same way.
    """
    if not spend_hit_cap():
        return None
    # Marked on the ledger as well as counted, because the teardown reads it after `_turn_ambient`
    # has torn the watch down — `spend_hit_cap()` would answer False by then.
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
            f"after billing {billed:,} and stopped with work still open, so the answer below "
            f"is partial (session {session.session_id})."
        ),
        code="spend_cap_reached",
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

    **"A narrower question" is the wrong advice when a tool failed, and the turn used to give it
    anyway.** `trace.called_tools` counts calls that were *announced*, and a call whose arguments
    the model could not write never is — so a turn in which the model asked for exactly the right
    tool and got the JSON wrong read "after 0 tool call(s) … a narrower or more specific question
    is the useful next step", directly beneath the `tool_failed` event naming that tool
    (`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` added
    the event and left this sentence alone, so the turn contradicted itself).

    **A refusal is not a failure, and the first fix said it was.**
    `D-2026-08-29-a-discarded-call-is-not-a-lost-call` replaced the advice with
    `tool_failures + tool_refusals` rendered as "N tool call(s) failed" — so a dry run the chemist
    themselves switched on reported three failures, while `TurnCost.tool_refusals` says in as many
    words that a refusal is "the control working, which must not be read as a failure". That is
    what `D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream` exists to
    stop, reintroduced one layer further out. They are counted apart here and lead to different
    next steps, because they *are* different: a fault is something to read, a refusal something to
    approve.

    **And the first count is *attempts*, which is why it does not say "ran".** `called_tools` is a
    view of the calls this turn *announced* — its own docstring says so, and `_acted` one screen
    below relies on it — so a refused call is in it. Printing that total as "ran" beside "3 refused
    by a gate" reported six intents where there were three, and told a chemist three calls had run
    that a gate had stopped before the body. The subsets are named as subsets.

    **What happened is always stated; only the advice branches, and it branches by precedence
    rather than by size.** The earlier form replaced the narrower-question line entirely, so one
    failure among twenty-nine calls deleted the only useful next step on the exact du-03 shape this
    docstring is about. The counts are their own clause now, and a fault takes the remedy whenever
    there is one — deliberately, because one fault among twenty-nine refusals is still the thing to
    read first. (This paragraph said the remedy "follows from what dominates", which describes a
    comparison the code does not make.)
    """
    if ledger.answer_text.strip():
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
    # No trailing stop on any of these: the session id closes the sentence, and a period before it
    # leaves the `(session …)` reading as a fragment — which is what the first version shipped.
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
    """`failure_event` for a turn that is already running, with this turn's log line beside it.

    The classification is kept on the ledger as well as sent, which it was not: `_classify` ran,
    its code went to the chemist, and the server-side record of the turn had no idea the turn had
    failed at all — let alone how. So a chemist quoting `storage_unavailable` named something the
    deployment could not look up.
    """
    logger.exception("turn failed for session %s", session.session_id)
    event = failure_event(exc, session.session_id, ledger.correlation_id)
    ledger.error_code = event.code
    return event


def _turn_acted(trace: ToolCallTrace) -> bool:
    """Whether this turn issued any state-changing call — what decides if a teardown spends.

    `called_tools` counts attempts, refused ones included, and that is the right set here: the
    conservative direction for an authorization is to spend it, and the cost of over-spending is
    one extra approval click where the cost of under-spending is a free second turn under a
    decision a person made once.
    """
    from chemclaw.agent.authz import side_effecting_tools

    acting = side_effecting_tools()
    return any(name in acting for name in trace.called_tools)


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


def _settle_outcome(ledger: _TurnLedger) -> str:
    """How this turn ended, as one value of `_OUTCOMES` — the one producer of that enum.

    **The order is a precedence, and each step of it is an argument.**

    `errored` first: a turn that raised has an `error_code`, and nothing after that is a better
    description of what happened to it.

    Then the wall-clock kill, told apart from the other cancellation by the caller's deadline (see
    `_TurnLedger.deadline`). It comes before the answer tests because a turn the clock killed may
    well have produced prose, and "the wall clock killed it" is what an operator needs to read.

    **A client disconnect does not, and that asymmetry is a billing rule rather than a taxonomy
    preference.** A turn that produced its answer and then lost its reader is `answered`: the model
    ran, the tokens were spent, and the answer exists — `completed` is derived from this outcome, so
    ranking the disconnect first billed such a turn as incomplete. That is the same under-reporting
    that got `stream_events(version="v3")` declined, where a turn abandoned mid-message booked 0
    tokens and made "drop the connection just before the answer" a free bypass of the token budget.
    So `abandoned` keeps its meaning — the turn reached no answer *and* no named ending — and a
    cancellation is only what distinguishes it from `empty_answer`, which is the same absence with
    a reader still attached.

    `loop_capped` before `empty_answer`, because the cap is the *cause* and an empty answer is one
    of its symptoms. That ordering is what makes `empty_answer` mean something: it names the silent
    death nothing explained — the shape `_empty_answer_event`'s docstring is about, measured with
    the harness off, 29 tool calls, no cap fired and 197 seconds of nothing.

    Both caps also come before `answered`, even though a capped turn does deliver its partial
    answer and `events.py` names the two cap errors as the ones that share their turn with one.
    Ranking `answered` first would make them unreachable, which is the same collapse
    `turn_costs.completed` already performed.

    `empty_answer` is the floor, not `abandoned`: every route to `abandoned` passes through the
    cancellation flag, so a teardown *outside* that clause — which is what an ordinary silent death
    is — lands on `empty_answer`. The docstring said the opposite for as long as the two were
    decided by the prose rather than by the flag.
    """
    if ledger.error_code:
        return "errored"
    if ledger.cancelled and ledger.timed_out:
        # **Read off the ledger, not off the clock.** `>=` against the same event-loop clock
        # `asyncio.timeout` schedules itself on is an exact test — *at the instant the cancellation
        # is delivered*. This function runs later, in the `finally`, behind the rollback and the
        # approval spend, so evaluating it here made a Stop at `deadline − ε` with a slow teardown
        # book `timed_out`. `_deadline_passed` is now called in the `except` clause that sets
        # `cancelled`, and this reads what it found.
        return "timed_out"
    if ledger.loop_capped:
        return "loop_capped"
    # After `loop_capped` and for a structural reason rather than a preference: both guards are
    # `before_model` hooks and the iteration cap is attached first, so when a turn is over both
    # ceilings the iteration cap is the one that jumps and the spend cap never runs. Ranking them
    # the other way would name an ending that cannot happen while the other is reachable.
    if ledger.spend_capped:
        return "spend_capped"
    # **`ledger.answered`, not "some prose was emitted", and the difference is a billing fact.**
    # The flag is set at exactly one place — immediately before `yield answer` — so it means an
    # `AnswerEvent` was built and delivered. Testing `answer_text` instead is strictly weaker: a
    # turn Stopped one token into its answer has prose and no `AnswerEvent`, and booked
    # `outcome="answered"` and therefore `completed=True`, billed as a delivered answer, while the
    # same teardown logged "torn down before it answered" one line away. `_empty_answer_event`
    # returns *before* the flag for the same reason, and its comment records the previous time this
    # exact substitution was made ("the cost ledger booked 'the user got an answer for the money'
    # for precisely the silent-death turn that branch exists to name").
    #
    # The disconnect-after-answer case this ordering was written for still lands on `answered`:
    # the flag is set before the yield, and the cancellation that reaches a finished turn is
    # delivered *while suspended in that yield*.
    if ledger.answered:
        return "answered"
    # The same absence, told apart by whether anyone was still reading: a turn cut short before it
    # could answer is `abandoned`, while one that ran to its own end and said nothing is the silent
    # death `empty_answer` names.
    return "abandoned" if ledger.cancelled else "empty_answer"


def _deadline_passed(deadline: float | None) -> bool:
    """Whether the event loop has reached `deadline` — `False` when there is none, or no loop.

    `>=`, against the same event-loop clock `asyncio.timeout` schedules itself on, so this is an
    exact test rather than a tolerance *at the instant it is taken* — which is why its one caller
    is the `except` clause that receives the cancellation and not the teardown that books the row.

    `get_running_loop`, not `get_event_loop`, and the `None` arm is not defensive padding: a turn
    settled off the loop is a caller that set no deadline anyway (a test, a synchronous teardown),
    and raising `RuntimeError` on the path that records what a turn cost would lose the row to save
    a comparison.
    """
    if deadline is None:
        return False
    try:
        return asyncio.get_running_loop().time() >= deadline
    except RuntimeError:
        return False


def _resolved_model() -> str:
    """The model id this deployment's *agent* route resolves to, for the turn record.

    **A documented attribution that had no producer.** `core/metrics.py` and
    `docs/guides/runbook.md` both say `turn_costs` carries model attribution — it is the stated
    reason the metric label set deliberately omits `model` (D-2026-08-01-spend-is-a-ledger-not-a-
    label) — and the table had no such column and no writer. So the one place model attribution was
    said to live was the one place it did not.

    Read from config rather than off the built model, and derived in one expression rather than by
    re-walking `build_chat_model`'s provider branch: `model_routes["agent"]` wins where a deployment
    routes per task, `llm_model` is required and non-empty under `openai_compatible` and empty
    otherwise, and `agent_model` is the Anthropic default. So this resolves exactly what that
    function would build, for both providers, without a second copy of the branch.

    **One turn can span models and this column names the agent's**, deliberately: the verifier's
    judge runs on the `verifier` route (F10-E), which may be a different, cheaper model, and its
    tokens are metered into the same turn. A column per route would be a schema that grows with the
    route table; the agent route is the one that produced the answer, and this is a comment saying
    so rather than a claim that the turn used exactly one model.
    """
    return settings.model_routes.get("agent") or settings.llm_model or settings.agent_model


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
    carry. Not a duplicate: `core/metrics` caps the label series one counter may hold (D-152,
    `_MAX_SERIES_PER_COUNTER` — read it there rather than repeating the number here, which is how
    three copies of this sentence came to say 64 after it was raised to 128) because a label value
    is attacker-influenced, and an Entra oid is exactly such a key — so per-actor spend
    needs a table, and the fleet-wide rate needs a counter. Booked here rather than on the success
    path so a turn torn down by a disconnect is billed too: that is the runaway this ledger exists
    to find, not an edge case to drop. It does not await — see its own docstring.

    **It is the one producer of a chat turn's record** (`_settle_outcome`, `_OUTCOMES`): the
    outcome, the classified error code, the resolved model, the tool/job counts and the
    time-to-first-token all land here, in the one function that runs on every path a turn can take.
    Anywhere else *inside a turn* would be a second place that has to remember, and the disconnect
    path is exactly the one such a place would forget. The table itself has a second writer, which
    is a different thing: `durable/template_activities._book_step_spend` books a template agent
    step, which is not a turn and never passes through here — it settles its own outcome, in this
    vocabulary, and says so.
    """
    elapsed = time.perf_counter() - ledger.started
    # **The budget first, and everything that can fail after it.** The three derivations below were
    # computed ahead of this line, so anything raising in any — `_settle_outcome` reading a
    # ledger, `_resolved_model` reading config, `current_context` reading a contextvar — lost
    # *both* the budget record and the `turn_costs` row, and replaced the `CancelledError` this
    # frame usually runs under with its own exception. `budget.record` is a dict write and the
    # thing a runaway is metered by; it goes first, and the record is then settled where a failure
    # costs one row's precision instead of the row.
    if budget is not None:
        budget.record(session.session_id, actor, ledger.usage.total)
    outcome = "unknown"
    model = ""
    context = None
    try:
        outcome = _settle_outcome(ledger)
        model = _resolved_model()
        # Last of the three, and the cheapest to lose: the two fields it feeds already read
        # `context is not None`, so a failure here books them False rather than losing the row.
        context = current_context()
    except Exception:
        # Assigned progressively above, so a failure in the second derivation keeps the first.
        # `unknown` is the `turn_costs` column default and means "written before `outcome`
        # existed"; a row reaching it *through this arm* is the only other way it can be written,
        # which is why the arm is loud rather than silent.
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
            duration_seconds=elapsed,
            # **`ledger.answered`, which is what it has always been, not `outcome == "answered"`.**
            # The two agree on most rows and disagree on the ones that matter: a loop-capped turn
            # *does* deliver its partial answer (`events.py` names the two cap errors as the
            # ones that share their turn with one), and a turn that raised after answering has an
            # answer too. Deriving the boolean from the enum booked both as `completed=False`, so
            # the field every existing dashboard and eval reads would have quietly changed meaning
            # under them — while this migration's own header claimed it "stays exactly where it
            # was". It stays exactly where it was.
            #
            # The two fields are not redundant and neither is the other's summary: `completed`
            # answers "did the chemist get an answer for the money", which is a billing question,
            # and `outcome` answers "how did the turn end", which is six-valued and is what a new
            # reader should ask.
            completed=ledger.answered,
            outcome=outcome,
            error_code=ledger.error_code,
            model=model,
            tool_calls=ledger.tool_calls,
            tool_failures=ledger.tool_failures,
            tool_refusals=ledger.tool_refusals,
            jobs_started=ledger.jobs_started,
            ttft_seconds=ledger.ttft_seconds,
            # Read off the turn's context record rather than the ledger, because the producer is a
            # middleware three layers down and the ledger is this module's. Still live here: this
            # runs inside `_turn_ambient`'s `with`, which is what makes the read the turn's own
            # rather than the next turn's or nobody's.
            compacted=context.compacted if context is not None else False,
            context_unreducible=context.unreducible if context is not None else False,
        )
    )
    # **The same record as a log line, because a deployment may have no ledger to read.** The cost
    # row needs Postgres (`session_store="postgres"`); the log stack is always there. Measured
    # before this existed: `grep -c logger.info api/runner.py` was **0** — a healthy turn produced
    # no log record of any kind, so "what happened in this turn" was answerable only from the
    # chemist's screen. `turn.started` above and this pair the way `job.started`/`job.finished` do.
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
        tool_calls=ledger.tool_calls,
        tool_failures=ledger.tool_failures,
        tool_refusals=ledger.tool_refusals,
        jobs_started=ledger.jobs_started,
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


async def _with_pushed_job_results(session_id: str, user_message: str) -> str:
    """The turn's input, with any waiting job push-back appended as framed data.

    The mailbox half of "compute then reason": a job that outlived its turn writes a
    `session_events` row, and until this existed the row's only consumer was the browser's SSE
    stream — so with the tab closed the completion reached nobody, and the model started its next
    turn not knowing work it launched had finished. Claimed with the same atomic claim the stream
    uses, scoped to the same two kinds, so the two consumers cannot double-deliver one row and
    neither can starve the other of kinds it does not handle.

    The chemist's words lead and the push-back follows, framed
    (`chemclaw.agent.framing.frame_untrusted`) because a job summary is workflow output, not an
    instruction. Best-effort in both directions: a mailbox that cannot be read must not fail the
    turn, and a memory-backed deployment has no mailbox to read.
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
    return (
        f"{user_message}\n\n"
        "Since your previous turn, durable job(s) this session started have finished. Some may "
        "have failed: report any entry whose kind is 'job_failed' to the chemist rather than "
        "describing that work as done. Their outcomes follow as data; use "
        "get_durable_job_status for full results where needed.\n"
        + frame_untrusted(summary, note_id="job-results")
    )


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
