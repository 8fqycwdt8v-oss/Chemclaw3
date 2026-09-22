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
passing an object in and comparing what comes back: `api/runner_trace.py` (the events a tool call
and its result become), `api/runner_usage.py` (the turn's token arithmetic) and
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
from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.job_results import await_job_results
from chemclaw.agent.local_skills import personal_skills_available
from chemclaw.agent.loop_cap import loop_hit_cap
from chemclaw.agent.plan_gate import (
    PLAN_APPROVAL_PROMPT,
    approval_stands,
    consume_turn_approval,
    gate_applies,
    plan_identity,
    spend_approval_after_teardown,
)
from chemclaw.agent.plan_state import session_plan
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.scratchpad import memory_store
from chemclaw.agent.session import TurnSession
from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.agent.skill_fingerprint import skill_fingerprint
from chemclaw.agent.spend_cap import spend_hit_cap, turn_billed_tokens
from chemclaw.agent.state import turn_config
from chemclaw.agent.stored_skill_tools import stored_skill_declarations
from chemclaw.agent.tool_result_size import bounded_content
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
from chemclaw.core.turn_flags import reset_dry_run, set_dry_run
from chemclaw.core.turn_signals import JobSignal, SkillLoadedSignal
from chemclaw.core.turn_text import reset_current_user_texts, set_current_user_texts
from chemclaw.durable.awaiting import AwaitRequest, open_wait
from chemclaw.kg.note import cited_ids

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
    graph_factory: Callable[..., Any] = build_turn_agent,
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
    # **Read before the `with`, because `_turn_ambient` may not `await`.** That block reaches its
    # resets by cancellation rather than by `aclose()` (D-130), so an `await` inside it re-raises
    # the cancellation on the spot and leaks one turn's ambient identity into the next turn on this
    # worker — the reason it is synchronous at all. The history read is therefore the caller's, and
    # its result is passed in.
    earlier_said = await _earlier_user_texts(history, session)
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
                # **Built off the loop.** Compiling the graph is ~42.5 ms of pure-Python work —
                # every bound tool's schema, the middleware chain, and the helper roster, which is
                # 18.8 ms of it on its own — and it ran on the one event loop this process has.
                # There are no workers to absorb it (`service_uvicorn_workers > 1` is refused), so
                # at the shipped admission cap that was a per-turn stall of every other chemist's
                # stream and both probes.
                #
                # **It does not take all of that off the loop, and the honest number is the point.**
                # A thread buys no parallelism — the GIL is held between switch intervals — so what
                # this buys is the loop being *scheduled* during the build rather than after it.
                # Measured on build-shaped work, medians of five runs with a 2 ms heartbeat:
                # worst-case loop stall **52.2 ms -> 16.6 ms**, a 3.1x reduction rather than an
                # elimination, and the build's own wall clock is unchanged (43.9 -> 44.3 ms). A
                # single run showed the build 38% slower and that was noise; the residual 16.6 ms
                # is the switch interval and is not removable here.
                #
                # The awaits are hoisted rather than moved into the thread, because acquiring a
                # pooled connection is the loop's to do.
                #
                # The third one reads what the two stored skills tiers declare about tools, so
                # `ToolScopedSkills` narrows them as it narrows a filed tree — it is `await` for the
                # same reason the other two are, and it is here because the builder that needs it is
                # synchronous. It is one paged search per mounted tier over a capped namespace, on
                # the same store `turn_store()` just returned.
                #
                # It takes no actor: it reads the same `get_current_actor()` the mount resolves
                # its namespace from, which is why this call sits inside `_turn_ambient`. Passing
                # the request's raw value made the two spell one actor two ways for a padded oid —
                # see `stored_skill_declarations`, which carries the measurement.
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
                # `turn_config`, not a bare `configurable`: it also carries the graph's step
                # ceiling, which nothing here had ever chosen — the framework bakes 9999, and
                # reaching it raises rather than degrading. The mid-turn resume below reuses this
                # same config, so the continuation runs under the same bound as the run it
                # continues.
                graph_config = turn_config(session.session_id)
                # **The meter for a call that is never reported on.** Attached to the *turn's* own
                # invocation, which is what makes it reach every model call the graph makes — a
                # node's call inherits the run's callbacks through LangChain's contextvar, and so
                # does one a tool body starts (`agent/turn_usage`'s module docstring measures both).
                # Attaching it at an inner call site instead would *replace* the inherited handlers
                # and take that call off the stream this turn meters, which is the same trap
                # `off_stream_metering` documents.
                graph_config["callbacks"] = [ledger.prompts]
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
                # **One carry for the whole turn, because a turn can be two graph invocations.**
                # `model_calls` and `billed_tokens` are untracked channels, so a resume on the same
                # thread starts them at zero and gets a second full allowance of both caps —
                # measured, and exactly contrary to `agent/spend_cap.py`'s stated unit ("one budget
                # spans a turn that delegates"). Passing the same dict to both runs is what makes
                # the caps the turn's rather than each invocation's.
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
                # The stream is exhausted, so the graph has returned and the history provider has
                # committed this turn's rows as a complete, paired exchange. From here on a teardown
                # has nothing half-written to discard — set the fact the rollback gate reads at the
                # moment it becomes true, not at the answer, which is still a verifier call and
                # possibly a job-result wait away.
                ledger.run_complete = True
                # No trace flush here: every call the graph makes is announced by the `updates`
                # stream that carried it, so there is nothing left open when that stream ends.
                # (This used to iterate `tool_trace.flush()` for a call whose arguments finished on
                # the final update — the streamed shape the previous engine had. The reassembler it
                # drained went with that engine, and the loop returned `[]` on every turn.)
                async for event in _resume_on_job_results(
                    graph,
                    config=graph_config,
                    trace=tool_trace,
                    session=session,
                    ledger=ledger,
                    carry=cap_carry,
                ):
                    yield event
                # **Everything from here to the end of the revision loop is inside the stack**, and
                # that is a fix rather than a layout choice. `_open_turn_surface` entered one
                # `HeldConnectorSession` per bundle *on this stack*, so the block's end is where
                # every MCP tool dies — and the revision loop sat below it. Measured on a turn with
                # one connector bound: `session OPENED -> tokens -> session CLOSED -> [revision] ->
                # ToolFailedEvent`. In-process tools kept working, which is what made a pass whose
                # entire purpose is to *re-ground* an answer fail silently at exactly the tools that
                # hold the evidence.
                for event in _cap_events(session, ledger):
                    yield event
                silent = _empty_answer_event(session, tool_trace, ledger)
                if silent is not None:
                    yield silent
                # **The stop is a different question from the naming, and writing them as one
                # condition made one of them dead code.** "Does this silence need an error event of
                # its own?" is `_empty_answer_event`'s, and it answers *no* for a turn a cap has
                # already named — see its own comment for the drive. "Is there anything to ship?" is
                # this one, and it is true of a capped silent turn exactly as much as of an
                # unexplained one, because the three consequences below do not care why the text is
                # empty. The first version of this fix asked the cap test here as well, which made
                # the test inside `_empty_answer_event` unreachable: driven, three mutations of it
                # stayed green.
                #
                # **`return`, not fall through**, which is what this did. `events.py` names
                # the two cap errors as the ones that share their turn with an answer, and
                # falling through broke that for `empty_answer` in three ways at once: the
                # client got an `AnswerEvent` whose text is `""` (the reference page renders it
                # as an empty assistant bubble), `build_answer_event` spent a judge call under
                # `verifier_enabled` grading an empty string, and `answered = True` reached
                # `record_turn_cost(completed=answered)` — so the cost ledger booked "the user
                # got an answer for the money" for precisely the silent-death turn that branch
                # exists to name. The teardown below still books the spend and the duration,
                # which is right: the turn cost what it cost.
                if not ledger.answer_text.strip():
                    return
                # Before the answer, because the answer is the turn's final event: a chemist reading
                # "review the plan and approve it" in the answer text used to have nothing to act on
                # — the decision routes and the surface's approval card both existed, and no turn
                # ever emitted the event that connects them.
                if plan_gated:
                    pending = await _pending_plan_approval(session.session_id)
                    if pending is not None:
                        yield pending
                answer, review = await build_answer_event(
                    ledger.answer_text,
                    tool_trace.outputs,
                    tool_trace.called_tools,
                )
                # **The flagged answer goes back for another pass, and nothing used to do
                # that.** `agent/verifier.py` marks an answer `review_required` and
                # `D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`
                # concedes what happened next: "Nothing routes a flagged answer back for another
                # pass." Looped here rather than in a middleware because the verdict is produced
                # *outside* the graph — the run has returned by this line, even though its
                # connector sessions are deliberately still open — and because the rounds must be
                # bounded by something a chemist's own follow-up resets, which a per-turn local is
                # and a state channel is not. Off at `answer_review_max_rounds = 0`.
                #
                # **`review.unsupported`, not `answer.unsupported_claims`.** The wire's list also
                # carries the notes saying which check spoke, and a round driven by one of those is
                # a round driven by nothing the model can act on: "verification did not run" quoted
                # back as a claim to drop, every round, until the allowance is gone — so a judge
                # outage multiplied every flagged turn's model spend by `max_rounds + 1` fleet-wide.
                # The other shape is a low-confidence answer whose every claim *is* supported, which
                # framed an empty block: the "just try again" prompt `_revision_message` exists to
                # avoid. A verdict with nothing actionable in it ships marked, as it did before this
                # loop existed.
                rounds = 0
                while (
                    answer.review_required
                    and review.unsupported
                    and rounds < settings.answer_review_max_rounds
                ):
                    rounds += 1
                    # **What the turn already has in hand, held across the round.** A revision
                    # *replaces* an answer, so `_revise_answer` clears `answer_parts` before it
                    # runs — and a round that then produces nothing (the model returns no text, or
                    # the spend cap jumps the graph `to end`) used to leave the turn shipping `""`
                    # booked as `outcome='answered', completed=True`: a blank bubble where the
                    # un-looped turn shipped a usable flagged answer, with
                    # `chemclaw_turn_empty_answers_total` flat because the emptiness guard had
                    # already run against the *flagged* text one screen above.
                    kept = list(ledger.answer_parts)
                    # The message the thread ends on right now: the answer this round is out to
                    # replace, and the mark everything the round adds sits after.
                    retracted = await _thread_tip(graph, graph_config)
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
                        # **A revision that raises must not cost the turn the answer it had.** The
                        # round was unwrapped, so a gateway 503 on the *second* call propagated to
                        # the handler below and the chemist got a generic internal error instead of
                        # the complete, already-graded answer sitting in `answer` — and
                        # `_record_review_rounds` never ran, so the exhaustion counter was blind to
                        # the whole class. Logged with the traceback, counted as an exhausted round,
                        # and the held answer ships.
                        logger.exception(
                            "revision %d for session %s failed; the answer held from before the "
                            "round goes out unchanged",
                            rounds,
                            session.session_id,
                        )
                        ledger.answer_parts[:] = kept
                        # The run that produced the answer being shipped *did* return, so the
                        # rollback gate must not read this turn as half-written on a later
                        # teardown; `_revise_answer` cleared the flag and never reached its reset.
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
                    # After the outcome is known, because what the thread must end on is the answer
                    # that ships — which is this round's only when the round produced one.
                    await _settle_revision_thread(
                        graph, graph_config, retracted=retracted, replaced=replaced
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
                    # **The one place a person is asked, and it is inside `if rounds:`.** That
                    # guard is what keeps `answer_review_max_rounds = 0` a complete no-op — not a
                    # second reading of the setting, which could disagree with the loop's own —
                    # and it is the only call site, so one turn cannot escalate twice however the
                    # loop left the building (exhausted, broken out of by an empty round, or by a
                    # round that raised). All three spent their allowance and all three end on an
                    # answer the flag is still on; `_escalate_exhausted_review` asks whether it is.
                    await _escalate_exhausted_review(
                        session, answer, actor, review.unsupported, ledger.correlation_id
                    )
            # **Asked again, because a cap can fire inside a revision.** Both guards were evaluated
            # once, above the loop, so a turn whose second model call tripped the loop or spend cap
            # emitted no `ErrorEvent` at all and booked `outcome='answered', completed=True`. The
            # caps always *enforced* through the shared `cap_carry`; what they could not do is
            # report. Each event is announced once — `_cap_events` reads the ledger flag the first
            # ask set, so the pre-loop ask and this one cannot both speak.
            for event in _cap_events(session, ledger):
                yield event
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
            # **The one event that does not arrive through `_stream_into`.** `note_event` takes its
            # counts off the graph's event stream, and the `AnswerEvent` is built here rather than
            # streamed — so the three columns read off it (`answer_confidence`, `review_required`,
            # `notes_cited`) were `NULL/false/0` on every row until this line existed. That is the
            # `record_kept_chunks` shape: a reader with no caller, its own tests calling it directly
            # and passing. `tests/test_turn_knowledge.py` now drives the booked row instead.
            #
            # Before the yield for the same reason `ledger.answered` is: the cancellation that
            # reaches a finished turn is delivered while suspended in that yield, and the spend is
            # booked in the teardown, so a count taken after it would be lost exactly when the turn
            # completed normally enough to have one.
            ledger.note_event(answer)
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
    # **What the calls still in flight have committed this turn to paying**, which `usage` cannot
    # know: a gateway reports usage on the terminal frame only, so a turn torn down mid-message
    # metered zero against a request it had already been billed for (`InFlightPrompts`). Held on
    # the ledger because it is per turn and because `_book_turn_spend` — the one function that runs
    # on every path a turn can take — is the reader.
    prompts: InFlightPrompts = field(default_factory=InFlightPrompts)
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
    # **The knowledge dimensions of a turn, which this row had every other dimension of.**
    # `turn_costs` already recorded what a turn *spent* and how it *ended*; what it could not say
    # was whether the turn consulted the record at all, whether the answer cited it, and whether
    # anything was written back. Those are the three questions the retrieval and capture reviews
    # each had to answer with a bespoke script, and none of them is derivable afterwards: the
    # events are gone, and `session_messages` holds prose rather than which tool ran.
    #
    # Counted here because `note_event` is already "the one place the counts are taken", and every
    # path — the model run, the mid-turn resume, a subagent's work — passes through it.
    retrieval_calls: int = 0
    capture_calls: int = 0
    # From the turn's own `AnswerEvent`: `score_answer` computes all three on every production
    # turn and they were **streamed and discarded** — `api/schemas.py` records that they are not
    # persisted either, so the richest answer-quality signal this system produces was retained
    # nowhere. `answer_confidence` stays `None` when the verifier did not run, which is not the
    # same as a low score and must not be stored as one.
    answer_confidence: float | None = None
    review_required: bool = False
    notes_cited: int = 0
    # **Which skills actually shaped this turn**, for the guard `agent/distiller.py` reads. A set
    # rather than a list: a turn that re-read one skill's body loaded it once as far as any
    # consumer is concerned, and a duplicate would make "how many turns loaded this" wrong in the
    # direction that admits self-confirming evidence. Ordered on the way out so two turns that
    # loaded the same skills produce the same row.
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
            # **Both sides are stated subsets, and each has a test holding it inside the authz
            # partition it belongs to.** Neither question authz answers is this one: it partitions
            # by *what a tool may do without approval*, so deriving retrieval from the read-only
            # set counts a turn that asked the chemist a question as a turn that searched the
            # record, and deriving capture from the state-changing set counted a turn that
            # computed one xTB energy as a turn that wrote knowledge back — 49 tools, six of them
            # writes. A derived set is only better than a stated one when the derivation answers
            # the question being asked.
            #
            # The retrieval half is a *union* rather than a bare set, because a bundle's searches
            # are the bundle's own fact: `rxnfp`'s seven searches over the reaction corpus are as
            # much "did this turn look at the record" as `gather_evidence` is, and counting them
            # by naming them in core would be the copy D-118 exists to prevent.
            if event.tool in knowledge_read_tools():
                self.retrieval_calls += 1
            elif event.tool in KNOWLEDGE_WRITE_TOOLS:
                self.capture_calls += 1
        elif isinstance(event, ToolFailedEvent):
            if event.reason is None:
                self.tool_failures += 1
            else:
                self.tool_refusals += 1
            # **A call that was refused or raised consulted nothing, so it is taken back out.**
            # Both counts are taken on the `ToolCallEvent`, which is the *attempt*; measured
            # 2026-09-06, a turn making one successful `find_notes`, one repeat-refused
            # `find_notes` and one `expand_note` that raised booked `retrieval_calls = 3` while the
            # record was consulted once. For the field's own 0-vs-nonzero question that is
            # harmless, and for any rate it is a threefold overstatement of the one behaviour the
            # retrieval obligation exists to move. A repeat refusal is the clearest case: it is
            # refused *because* an identical call already happened, and that one is already
            # counted here.
            #
            # Counted forward and reversed, rather than deferred to the outcome, because the
            # `ToolCallEvent` is where the tool's *name* is classified and a failure event may
            # arrive for a call this turn never saw start (a subagent's, a resumed run's) — so
            # `max(…, 0)` is the floor rather than an assertion that the pairing is total.
            if event.tool in knowledge_read_tools():
                self.retrieval_calls = max(self.retrieval_calls - 1, 0)
            elif event.tool in KNOWLEDGE_WRITE_TOOLS:
                self.capture_calls = max(self.capture_calls - 1, 0)
        elif isinstance(event, JobStartedEvent):
            self.jobs_started += 1
        elif isinstance(event, AnswerEvent):
            # The grade this turn already computed, on its way past. Taking it off the event rather
            # than from `score_answer`'s caller keeps every count in this one method, and means a
            # path that emits an answer without going through the runner's own assembly is counted
            # the same way.
            self.answer_confidence = event.confidence
            self.review_required = event.review_required
            self.notes_cited = len(cited_ids(event.text))

    def note_signal(self, signal: Any) -> None:
        """Record what a graph run announced about itself, for the readers below.

        A method rather than the lambda this was, because the second and third runs of a turn pass
        `note_signal_without_job_chaining` and a named pair reads as the decision it is rather than
        as one lambda that lost its body.
        """
        if isinstance(signal, JobSignal):
            self.started_jobs.append(signal.job_id)
        else:
            self.note_signal_without_job_chaining(signal)

    def note_signal_without_job_chaining(self, signal: Any) -> None:
        """The same, for a run that must not add to what this turn will wait for.

        **Exactly one signal type is suppressed, and the blanket that stood here suppressed the
        union.** The rule this callback exists for is `JobSignal`'s alone: a resume that fed its own
        job ids back into `started_jobs` would let one chemist turn chain durable jobs indefinitely
        inside a single request. Nothing about a revision round or a resumed half makes the *other*
        signals untrue, and dropping them was measurably unsafe in one direction that matters —
        `answer_review_max_rounds` ships at 2, so a skill read only during a revision round left
        `turn_costs.skills_loaded` empty, and `agent/distiller.py::independent_sessions` counted
        that session as *independent* evidence for proposing the very skill that was acting in it.
        The self-confirmation guard failed **open**, which is the direction it exists to close.
        """
        if isinstance(signal, SkillLoadedSignal):
            # Both tiers into one set. A personal skill shapes a turn exactly as a reviewed one
            # does, and the guard that reads this would otherwise be blind to the tier most likely
            # to be self-confirming — the one the agent can propose into.
            self.skills_loaded.add(signal.skill)


async def _earlier_user_texts(history: Any | None, session: TurnSession) -> list[str]:
    """The chemist's own earlier messages in this thread, bounded, for the `stated` ambient.

    The producer half of `core/turn_text`: `require_quotes_are_verbatim` grades a `basis="stated"`
    slot against the chemist's own words, and until this existed it could only see the message that
    started the turn in flight — so a constraint stated on turn 1 and acted on at turn 3 was
    unrepresentable as `stated` at all.

    **Bounded at the query**, by `agent_stated_quote_turns`, which is what makes it affordable to
    run once per turn: the provider filters to the chemist's own rows in SQL and returns at most
    that many, rather than reading a whole conversation and discarding the assistant half of it.
    The character half of the bound is applied where the ambient is bound, because it is a property
    of the ambient rather than of the read.

    **Best-effort, and its failure mode is the strict one.** A transcript is a rendering and no
    rendering is worth failing an answered turn over — the rule `api.tool_results` and
    `_record_transcript` already state — so an unreachable store degrades to no earlier words,
    which refuses a `stated` quote the turn could otherwise have accepted. That is the direction a
    check may fail in; the other one accepts a fabrication.

    **The catch stays narrow, and that is a choice about who is at fault.** Those two are what the
    session store raises for an unreachable or refusing database. Anything else means a provider
    whose `recent_user_texts` does not have this signature, which is a defect in this repository
    rather than in a deployment's database — and this call sits *outside* `run_turn`'s own
    `try`, so it would fail the request rather than degrade it. That is the right noise for a
    two-provider seam, and the reason the duck-type test above is a `getattr` rather than a catch.

    Args:
        history: The session's history provider, or `None` off the durable path. A provider
            without the method (a test double that only stores) contributes nothing, duck-typed
            for the reason `_record_transcript` states.
        session: The turn's session — its id addresses the durable rows and its `state` is where
            the in-memory provider keeps its thread, so one call is correct under both.

    Returns:
        Their earlier messages, oldest first. Empty when there is no provider, no thread, or the
        store could not be read.
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

    **Synchronous on purpose, and that is the point of extracting it.** These resets used to sit at
    the bottom of `run_turn`'s `finally`, under a comment warning that nothing in that block may
    `await` — because the disconnect path reaches it by cancellation rather than `aclose()` (D-130),
    and an `await` there re-raises the cancellation on the spot and skips everything below it,
    leaking one turn's ambient identity into the next turn on this worker. A `with` block cannot
    acquire an `await` between the last statement and the reset, so the rule is now structural
    rather than a comment somebody has to keep obeying.

    **The four cap watches and the token ledger are not here any more** — they are
    `agent.turn_ambient.turn_caps`', entered below, because two other drivers of a turn need the
    same five and were opening two and none of them. What stays is what only a *request* has an
    argument for.

    Each of the ones that stay, and why it is ambient rather than an argument:

    - the session, so a job-launching tool records push-back to the right session (F3-T3) — never a
      model-supplied argument;
    - the authenticated identity (F4), so audit, authorization and attribution see the user;
    - one correlation id per *turn*, generated by the caller and stamped here. Not bound inside
      `build_langgraph_agent`: agents are cached per profile for the process's lifetime, so a
      build-time id was shared by every turn from every user on the pod, and the audit trail could
      not tell two conversations apart;

    `dry_run` rides here too for the reason it is ambient at all: the model can neither set it nor
    clear it (IDEA-4). `user_texts` — the chemist's own words in this thread, this turn's message
    last — rides here for exactly that reason and no other: `protocols` checks a `basis="stated"`
    quote against them, and a haystack the model supplies is a haystack the model can invent
    (`core.turn_text`). It is the *thread's* user turns rather than this turn's message because
    `structure_experiment_request` is meant to be called iteratively, so a chemist's real
    constraint is usually two turns behind the "ok go ahead" that triggers the intake; the bound on
    how far back it reaches is `core.turn_text`'s, and the read that fills it is the caller's,
    because nothing in this function may `await`.

    Reset order is unchanged by the extraction and was checked rather than assumed: the nested
    `with` exits while the exception propagates out of the `yield`, so the five cap ambients still
    tear down first and in their old order, then the dry-run flag, then the three identity vars.
    `set_current_identity` is skipped entirely when there is no actor, so the unauthenticated path
    stamps nothing to reset.
    """
    session_token = set_current_session_id(session_id)
    user_texts_token = set_current_user_texts(user_texts)
    identity_token = set_current_identity(actor, roles) if actor is not None else None
    correlation_token = set_current_correlation_id(correlation_id)
    dry_run_token = set_dry_run(dry_run)
    try:
        # **The four cap ambients are `agent.turn_ambient.turn_caps`', not this function's, and
        # that is the whole of the change.** This front door opened all four; the Temporal
        # template step opened two of them and the CLI opened none, so on those two paths a
        # fan-out was
        # bounded by the per-branch channel snapshot rather than by the turn and an off-stream
        # model call was counted by nothing. Four zero-argument watches opened by hand in three
        # drivers is a thing three callers get wrong differently; a context manager is a thing a
        # driver either enters or does not. What stays here is what only a *request* can supply.
        with turn_caps(usage, closing=f"session {session_id}"):
            yield
    finally:
        _unstamp(session_id, reset_dry_run, dry_run_token)
        _unstamp(session_id, reset_current_user_texts, user_texts_token)
        _unstamp(session_id, reset_current_session_id, session_token)
        _unstamp(session_id, reset_current_correlation_id, correlation_token)
        if identity_token is not None:
            _unstamp(session_id, reset_current_identity, identity_token)


def _unstamp(session_id: str, reset: Callable[[Any], None], token: Any) -> None:
    """Undo one of this front door's ambients, naming the session in the log line.

    The tolerance and the argument for it live in `agent.turn_ambient.reset_tolerantly`, which the
    cap ambients need for the same reason and which `tests/test_layering.py`'s `agent -> api` ban
    puts on that side. This is the front-door spelling of `closing=`: every caller here has a
    session id, and a teardown line that cannot name one was the original defect's worst part.
    """
    reset_tolerantly(reset, token, closing=f"session {session_id}")


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

    **The supervisor's own tool call ends the paragraph, because prose written before a tool ran is
    not an answer to anything.** The parts used to be joined across every model call of the turn,
    with no separator: measured end to end on 2026-09-06 against a gateway that narrates and then
    calls a tool, the `answer` event read `'Let me look that up.THE FINAL ANSWER IS 42.'` and
    `GET /sessions/{id}/messages` carried the preamble twice — once as its own assistant row, once
    inside the answer row. Joining on a blank line would have fixed the run-on and neither of the
    other two, and it would have left the preamble in the string
    `runner_answer.build_answer_event` grades: `unsupported_claims` was being computed over "Let me
    look that up", which nothing grounds and nothing could. So the earlier prose stays exactly what
    it already is — a streamed `token` event and its own transcript row — and the answer is the
    last model call's.

    A *helper's* call is not the supervisor's, and the same `not event.agent` filter says so: a
    helper runs its tools inside the `task` call the supervisor is still waiting on, so cutting on
    those would delete the answer of every turn that delegated.
    """
    async for event in events:
        if isinstance(event, TokenEvent) and not event.agent:
            ledger.answer_parts.append(event.text)
        elif isinstance(event, ToolCallEvent) and not event.agent:
            ledger.answer_parts.clear()
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
    carry: dict[str, Any],
) -> AsyncIterator[Event]:
    """Continue this same turn with the results of the durable jobs it launched (gap AGT-2).

    If this turn launched durable jobs, optionally wait for them and continue the *same* turn with
    their results, so "compute this, then reason about the result" is one exchange rather than two.
    Off by default; bounded by config and, above it, by the front door's whole-turn deadline. Yields
    nothing at all when the feature is off, no job was launched, or none finished in time — which is
    why the caller can loop over it unconditionally.

    A second `graph_events` over the *same* graph and the same `thread_id`, because the continuation
    has to see the conversation the first half produced.

    **`on_signal` drops this run's job launches and keeps everything else.** A resume that fed its
    own job ids back into `started_jobs` would be the recursion this feature is without, so that one
    chemist turn cannot chain durable jobs indefinitely inside a single request — and that argument
    reaches `JobSignal` and nothing beside it, which is why the callback is named rather than a
    blanket `lambda _signal: None`. See `_TurnLedger.note_signal_without_job_chaining`.

    `run_complete` is cleared for the duration and set again after: the resume drives a *second*
    model run, which can half-write exactly like the first — so the exchange is incomplete again
    until it returns, and a teardown landing inside it must roll the turn back after all.

    **`carry` is what makes the turn's caps span both runs**, and its absence was the one place
    where "a turn" and "a graph invocation" came apart. `ChemclawState.model_calls` and
    `billed_tokens` are `UntrackedValue` channels, so this second invocation used to start both at
    zero — a fresh 25-iteration loop cap and a fresh `agent_max_turn_billed_tokens` for a run that
    describes itself, one line above, as the same turn. (`ledger.usage` was threaded through both
    all along, so `turn_costs` and `api/budget.py` always saw the total; only the two *in-graph*
    caps doubled.) Both are off in the shipped configuration, which is why this was found by
    reading rather than by an incident.
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

    Built on `_resume_on_job_results`'s shape — a second `graph_events` over the same graph and
    `thread_id`, with `run_complete` cleared for its duration and the same `carry`, so a revision is
    counted by the loop cap and the spend cap rather than buying a fresh allowance of either. That
    is the conclusion `D-2026-08-16` reached about `RubricMiddleware`'s revisions and it holds for
    these: a revision is a model call, and a cap it could skip would be a bypass.

    **`answer_parts` is cleared, which is the one place this is *not* the resume.** A resume
    continues an answer, so appending is right there; a revision *replaces* one, and
    `ledger.answer_text` joins the parts — so without this the transcript and the `AnswerEvent`
    would both carry the flagged prose with the corrected prose stapled to its end, which is worse
    than either alone. The client has already been streamed the first attempt's tokens and cannot
    un-see them; `AnswerEvent.text` is the authoritative answer and carries only this pass, which
    is what the event contract already says it is.

    **Framed as data, not as an instruction.** The unsupported claims are this system's own
    verdict, but they quote the model's prose back at it, and prose that reaches a model inside an
    instruction is prose that can instruct — the discipline `_job_results_message` follows for the
    same reason one line over.

    **What this round leaves on the checkpointed thread is the caller's to settle**, because only
    the caller knows whether the round was worth anything — see `_settle_revision_thread`. This
    function deliberately does not clean up after itself: a round that raises leaves its prompt
    behind exactly as one that succeeds does, and the two want opposite withdrawals.

    Args:
        graph: This turn's compiled graph — the *same* one, so the revision sees the conversation
            it is revising.
        config: The turn's graph config, carrying the thread id and the step ceiling.
        trace: The turn's tool-call trace, so a tool the revision runs is announced and scored
            like any other.
        ledger: The turn's ledger; its `answer_parts` are replaced by this pass.
        carry: The caps' per-turn carry, so a revision spends the turn's allowance rather than a
            fresh one.
        claims: The unsupported claims to name — `TurnReview.unsupported`, never the wire's merged
            `unsupported_claims`, which also carries notes about which check spoke.
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
            # A no-op for the reason the resume gives: a revision that fed its own job ids back into
            # `started_jobs` would let one chemist turn chain durable work indefinitely.
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

    `None` covers the deployment that keeps no checkpointer (`_turn_checkpointer` returns one only
    on the Postgres store): there is no persisted thread to leave anything on, so there is nothing
    for `_forget_revision_prompt` to withdraw either.
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

    A revision is this system talking to itself. `turn_input` makes `_revision_message` a
    `("user", …)` message and the checkpointer persists it, so without this the chemist's *next*
    turn opened on the retracted `ai` claim, then a `human` message they never wrote, then their
    real question — while `ledger.exchanges` collects only tool-bearing messages, so
    `session_messages` had neither. The transcript and the model's own record of one conversation
    disagreed, uncounted, and the claim this system had just rejected stayed restatable for the
    rest of the conversation.

    **Two withdrawals, because a round that bought nothing is the opposite case.** When the round
    produced an answer, that answer is what ships and the retracted one plus the prompt come off.
    When it did not — the model returned no text, the spend cap jumped the graph `to end`, or the
    call raised — the *retracted* answer is what ships, so everything the round added comes off
    instead and the thread is exactly what it was before the round. Either way the invariant is the
    same: the thread ends on the answer the chemist was given, and carries no message they did not
    write. Removing the round's additions as a whole contiguous run is also what keeps the thread
    legal — an `AIMessage` carrying `tool_calls` whose `ToolMessage` had been dropped is a thread
    no provider accepts.

    Removal rather than counting the divergence: `chemclaw_transcript_thread_divergence_total`
    exists for a teardown landing between two writes, which is an accident nobody can undo. This is
    a divergence this code creates on purpose and can therefore simply not create.

    Args:
        graph: The turn's compiled graph, whose checkpointer holds the thread.
        config: The turn's graph config, naming the thread to withdraw from.
        retracted: The message the thread ended on before the round, from `_thread_tip`. `None`
            means there is no durable thread and nothing to do.
        replaced: Whether the round produced the answer that is about to ship.
    """
    if retracted is None:
        return
    state = await graph.aget_state(config)
    messages = list(state.values.get("messages", []) if state.values else [])
    ids = [str(message.id) for message in messages]
    if retracted not in ids:
        # The tip moved out from under us — a compaction, or a thread this turn does not own.
        # Withdrawing by position from here would take somebody else's message, so nothing is.
        logger.warning("the revised thread no longer carries %s; nothing is withdrawn", retracted)
        return
    added = messages[ids.index(retracted) + 1 :]
    if replaced:
        # The one `human` message the round adds is the prompt `_revision_message` wrote: a real
        # user message cannot arrive mid-turn on this thread.
        prompt = [str(message.id) for message in added if message.type == "human"]
        drop = [retracted, *prompt]
    else:
        drop = [str(message.id) for message in added]
    if not drop:
        return
    await graph.aupdate_state(config, {"messages": [RemoveMessage(id=dropped) for dropped in drop]})


def _revision_message(claims: Sequence[str]) -> str:
    """What the model is told about its own flagged answer, worded and framed.

    A function of its own for the reason `_job_results_message` is one: this text is the decision
    the revision carries. It names the claims rather than saying "try again", because a revision
    prompt with no specifics measures nothing and licenses the model to reword instead of reground.

    Takes the claims rather than the `AnswerEvent` they came from, because that event's
    `unsupported_claims` is the *merged* list a reviewer reads — findings plus notes saying which
    check spoke — and a note about the check is not a claim the model can drop. The caller passes
    `TurnReview.unsupported` and enters the loop only when it is non-empty, so this is never handed
    an empty block.
    """
    named = "\n".join(f"- {claim}" for claim in claims)
    return (
        "Your previous answer was checked against the evidence this turn actually retrieved, and "
        "the claims below are not supported by it. Answer again: drop or correct each one, cite "
        "the evidence for what you keep, and say plainly what the evidence does not settle rather "
        "than filling the gap. Do not restate the previous answer.\n"
        + frame_untrusted(named, note_id="unsupported-claims")
    )


#: The five things that can become of a request to have a person read a flagged answer. A frozen
#: set rather than a comment, so `tests/test_answer_revision.py` can assert every one is reachable
#: and `_escalation_outcome` cannot book a sixth by typo — the failure mode of a label typo is a
#: silent second series, which is why `core/metrics.py` declares label *names* the same way.
ESCALATION_OUTCOMES = frozenset({"opened", "joined", "no_claims", "no_actor", "unavailable"})


def _escalation_outcome(outcome: str) -> None:
    """Book what became of one escalation attempt.

    Beside each of the five returns rather than once at the top, because the interesting outcomes
    are the four that are *not* `opened`: each was a log line and nothing else, and
    `chemclaw_answer_review_exhausted_total` counts turns that went out flagged, which is the same
    number whether a person was asked, an existing wait absorbed the ask, or the broker was down.

    **Raises rather than asserts**, per
    `D-2026-09-16-an-assert-is-a-control-with-an-off-switch-in-this-repository-too`: `python -O`
    deletes an assert, and what this checks is a *label value*, which the registry cannot check for
    itself — it validates label **names** and would take a typo'd outcome as a silent sixth series
    that no panel queries.
    """
    if outcome not in ESCALATION_OUTCOMES:
        raise ValueError(f"undeclared escalation outcome {outcome!r}")
    METRICS.increment("chemclaw_answer_review_escalations_total", labels={"outcome": outcome})


def _record_review_rounds(session: TurnSession, answer: AnswerEvent, rounds: int) -> None:
    """Book what the revision loop did, including the case where it ran out of rounds.

    **Exhaustion is counted separately and is not silent.** The answer still goes out and still
    carries `review_required`, which is exactly what it carried before this loop existed — so a
    deployment that runs out of rounds is no worse off than one with the loop off, and the
    difference is visible rather than inferred. That is the property
    `D-2026-08-16` found `RubricMiddleware` lacking: its `_finalize_evaluation` rewrites the result
    to `max_iterations_reached` and mutates no message, so a grader outage ships every answer
    ungraded with a log line nothing reads.

    **The denominator lives here, not at the call site**, so it cannot drift from the numerator it
    is divided by: this function runs exactly once per turn that entered the loop — including the
    turns that broke out because a round raised or produced nothing, which still tried and still
    spent — and `chemclaw_answer_review_exhausted_total` is incremented from inside it. Dividing
    exhaustions by `chemclaw_answer_revisions_total` compared a per-turn count with a per-*pass*
    one, which went silent at total failure for every `answer_review_max_rounds` above 1.
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

    **The half of the bound that was missing.** `_record_review_rounds` counts an exhausted loop
    and the answer ships still marked, which is where Paperclip's own `maxReviewRounds` does the
    thing this did not: it escalates the stage to a person, and only that person can advance it.
    Bounded rounds that end in a counter increment are a verdict nobody acted on twice over —
    the turn ends, the chemist holds a flagged answer, and nothing in the system is waiting on
    anybody to read it. This opens that wait, on the machinery that already exists for exactly
    this shape of question (`durable/awaiting.py`: a question, a deadline, an escalation, and an
    answer that may never come).

    **It runs as the turn's own authenticated principal, or it does not run.** `actor` is the
    front door's `oid`, the same value `require_actor` would read off the ambient identity this
    turn stamped. Where there is none — the CLI, a test, an unauthenticated dev posture — the
    escalation is skipped and says so: synthesizing a requester would be this system granting
    itself a chemist's identity to file work that chemist did not ask for, which is the shape
    `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late` declined a whole scheduled
    agent over, and which `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
    is the consequence of. A `requested_by` nobody can produce is worse than an absent wait,
    because the inbox would name a person who never asked.

    **The dedup subject is the conversation, deliberately.** `request_id_for` keys on
    `(kind, subject, asked_of)`, so what goes in `subject` decides what joins what: the answer's
    own text would open a fresh wait on every exhausted turn — a notification storm, and with the
    shape gate on a busy chemist could file a dozen in an afternoon — while a constant subject
    would collapse the whole deployment into one wait whose rationale belongs to whichever turn
    exhausted first. Naming the session makes the unit of review a *conversation*, which is what a
    person actually opens and reads: two turns of one thread that exhaust on related answers join
    one wait, and the reviewer sees one open question per conversation rather than one per turn.
    The consequence to know is that the joined wait keeps the **first** turn's rationale — so the
    claims named below are the ones that first went unsupported, and the reviewer is pointed at
    the thread rather than at a single answer. That is the right trade for a review (the thread is
    the evidence) and would be the wrong one for an approval, where each act needs its own
    decision — which is why `durable/connector_job.py` keys its approval on the job id instead.

    **Routed to the requester, and this paragraph said the opposite until the routing changed.**
    It read "unrouted (`asked_of=""`), which means whoever is entitled" — an argument about
    *authorization* (a review is not an authorization, so the requester answering it is fine) that
    never touched *visibility*. Unrouted does not mean "whoever is entitled" to a reader:
    `_may_answer` returns `True` for any authenticated caller and `pending_store`'s list predicate
    carries `OR asked_of = ''`, so the request was listed to the whole tenant carrying
    model-authored claim text lifted out of a thread those readers cannot open. The argument the
    old wording made is still sound and is now the reason the *requester* is a legitimate
    answerer rather than the reason nobody is named. `connector_job.py` still fails closed on an
    unrouted approval, for its own separate reason. Nothing is released by answering.

    **Best-effort, on the `deliver_best_effort`/`notify_session_best_effort` precedent.** The
    answer has already been built and is about to be yielded; a chemist must not lose it because
    the broker is down, so every failure is counted through `degraded` and swallowed. The turn is
    no worse off than it was before this function existed, which is exactly the property
    `_record_review_rounds` claims for the exhausted answer itself.

    Args:
        session: The turn's session — its id is both the dedup key and where the wait's own
            push-back notice lands, so the chemist learns a review was raised without this
            function emitting an event of its own.
        answer: The answer as it will ship. Only `review_required` is read: a loop that broke out
            after *fixing* the answer has nothing to escalate.
        actor: The turn's authenticated principal. **Checked against `entra_required` as well as
            for emptiness**, because off the authenticated path it is not empty: `api/auth.py`
            manufactures a stand-in principal whose `oid` is the literal `dev-user`, and
            `Principal.oid` is `min_length=1`, so `not actor` is false in exactly the posture this
            guard was written for. Raising a durable request as `dev-user` — and addressing its
            notices to `dev-user` — is the attribution-nothing-can-write shape
            `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deletes on sight,
            arriving through the branch meant to prevent it.
        claims: What the last verdict found unsupported, named in the rationale so the reviewer
            starts where the checks stopped.
        correlation_id: The turn's id, in the rationale because it is the join key to
            `turn_costs`, `audit_events` and every log line the turn wrote.
    """
    if not answer.review_required or not settings.answer_review_escalation_enabled:
        return
    if not claims:
        # **The loop refuses to revise on a contentless verdict and this must refuse to escalate on
        # one, for the same reason.** The re-grade at the loop's bottom is a *fresh* verdict, so a
        # turn can exit with rounds spent and `unsupported` empty — a judge outage
        # (`verifier.py` sets `review_notes` and leaves the claims empty) or a low-confidence
        # verdict whose every claim is supported. Both produced a rationale ending "What the checks
        # could not ground: " with nothing after it. A judge outage is fleet-wide, so without this
        # it files one contentless review request per active conversation — a verdict nobody can
        # act on, which is the failure this whole escalation exists to end.
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
    # Built here rather than through `request_external_input`, whose two extra acts are both wrong
    # for this caller: `authorize_trigger` decides against the *model's* standing for a tool the
    # model did not call, and the premise pre-check refuses to open a wait whose citations have
    # since moved — which for a review is the strongest reason to open one. What the model-facing
    # path does that this copies is the whole construction below, including `requested_by` coming
    # from the authenticated identity and nothing else.
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
        # **Routed to the requester, and that is a visibility decision rather than a routing one.**
        # An empty `asked_of` does not mean "whoever is entitled" to a reader — `_may_answer`
        # returns `True` for any authenticated caller and `pending_store`'s list predicate carries
        # `OR asked_of = ''`, so an unrouted request is listed to the whole tenant. This one's
        # `rationale` is model-authored claim text lifted out of the answer, and its `subject`
        # names the conversation — while that conversation is owner-scoped and 404s a non-owner
        # with no existence leak. Unrouted, it published a fragment of a private thread fleet-wide
        # to people who cannot open the thread to check it.
        #
        # So it goes to the one principal who can actually read what it points at. That makes the
        # ask an honest self-review rather than a leaky appeal to nobody: a real reviewer
        # population is a configured entitlement this deployment does not have, and inventing one
        # here would be a control nobody asked for. `connectors/bo/workflows.py` leaves `asked_of`
        # empty for a wait a *person* launched about work they chose to share; this one fires
        # automatically, per conversation, carrying conversation content.
        asked_of=actor,
        requested_by=actor,
        session_id=session.session_id,
        correlation_id=correlation_id,
    )
    try:
        # **Bounded, because the answer is already built and waiting behind this call.** `connect()`
        # caches a client for the process, so a broker that has since died is discovered *here*, on
        # the path between the last token and the `AnswerEvent` — an unbounded open would hold a
        # finished answer for as long as the broker takes to not answer. The same budget the turn
        # already pays once for its durable-reachability probe, for the same reason it exists
        # there: a check that delays every turn is worse than the outage it reports. A timeout
        # lands in the degrade below, which is where it belongs.
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

    Asked twice per turn — once when the graph run returns, once after the revision loop — because
    a cap can trip in either, and the second ask is the one a revision needs: the guards were
    evaluated only before the loop, so a cap tripped by the *second* model call emitted nothing and
    the turn booked `outcome='answered', completed=True`. Enforcement was never the gap (both caps
    run in-graph off the shared `cap_carry`); reporting was.

    The two helpers below return `None` once they have spoken, so "asked twice" cannot become "said
    twice" — which is what a surface reading two `loop_cap_reached` events for one turn would have
    to reconcile.
    """
    for event in (_loop_cap_event(session, ledger), _spend_cap_event(session, ledger)):
        if event is not None:
            yield event


def _partial_answer_clause(ledger: _TurnLedger) -> str:
    """How a cap event ends, which depends on whether the turn wrote anything before it fired.

    Both cap events ended `"so the answer below is partial"` unconditionally, and a capped turn does
    not always have an answer below: driven 2026-09-19 at `agent_max_turn_billed_tokens=1`, the cap
    fired after 1,020 billed tokens with no prose at all, so the chemist read "the answer below is
    partial" with nothing following it — and then, one event later, `empty_answer` saying "Nothing
    was written, so there is nothing below to read" with the opposite `retryable` flag.

    One function because the two caps are one sentence with one number swapped, and a clause fixed
    in one of them is the shape `tasks/lessons.md` calls a rule written twice.
    """
    if ledger.answer_text.strip():
        return "so the answer below is partial"
    return "and nothing had been written, so there is nothing below to read"


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
    # Already announced by an earlier ask: `_cap_events` runs before the revision loop and again
    # after it, and one firing is one event.
    if ledger.loop_capped or not loop_hit_cap():
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
            f"and stopped with work still open, {_partial_answer_clause(ledger)} "
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
    # `_loop_cap_event`'s guard, for its reason: asked once before the revision loop and once
    # after, and a cap that has already spoken says nothing more.
    if ledger.spend_capped or not spend_hit_cap():
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
    view of the calls this turn *announced* — its own docstring says so, and `_turn_acted` one
    screen below relies on it — so a refused call is in it. Printing that total as "ran" beside
    "3 refused by a gate" reported six intents where there were three, and told a chemist three
    calls had run that a gate had stopped before the body. The subsets are named as subsets.

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
    if ledger.loop_capped or ledger.spend_capped:
        # **A capped turn is not a silent one, and saying so twice contradicted itself.** Driven
        # 2026-09-19 at `agent_max_turn_billed_tokens=1`: the chemist got `spend_cap_reached`
        # (`retryable=False`) immediately followed by `empty_answer` (`retryable=True`) about the
        # same silence, which a surface cannot reconcile — and `chemclaw_turn_empty_answers_total`
        # moved too, firing `ChemclawTurnsAnsweringEmpty`, whose own description and runbook entry
        # both said "No error counter moves" and sent the operator after "a model that emitted only
        # tool calls" while naming neither the cap nor the counter that identifies it. The cap has
        # its own event, its own counter and its own turn outcome; this series is for the case
        # nothing explains, which is what makes it worth alerting on at `for: 0m`.
        #
        # `_cap_events` runs before this, so the flags are set by the time it is asked. The caller
        # still stops here — see its own comment — because a capped turn with no prose must not
        # reach `build_answer_event`.
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

    Reads the same sources the gate and `consume_turn_approval` read — `session_plan` for the
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
        steps = await session_plan(session_id)
        if steps is None:
            return None
        plan_hash = plan_identity(steps)
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

    **Only `session.state` is rolled back, and there is nothing here that rolls back a
    checkpoint.** It used to have a durable half: a pre-turn watermark over `session_messages`,
    because the previous engine wrote the stored thread incrementally and fed it back to the model,
    so a disconnect mid-tool-call committed a `tool_use` with no matching `tool_result` and every
    later turn replayed it — the model rejected the thread outright ("tool_use ids found without
    tool_result blocks") and one dropped connection permanently bricked the conversation. The graph
    reads its own checkpointer instead, and `_record_transcript` writes the user message and the
    answer together in one call once the answer exists.

    **So the transcript is all-or-nothing across a teardown and the thread is not, and this
    docstring used to claim there was "no third outcome".** There is, it is this branch, and it was
    measured: a real `run_turn` cancelled between the graph run and `_record_transcript` left
    `checkpoints: 8, session_messages: 0` — the model sees the question and the answer, the chemist
    sees neither, and the next turn answers out of a context the chemist cannot read ("as I said
    above", about something that is not on their screen). The checkpoint is committed by the graph
    the instant it is written, on the checkpointer's autocommit pool, and nothing in this process
    owns it by then. Keeping the exchange is still the right call — the alternative is deleting a
    *complete, correctly paired* exchange because a client dropped — so what changes is that the
    divergence is counted rather than denied.

    **The counter names one branch and not the whole class**, deliberately. The same divergence
    arrives on the ordinary failure path (a gateway that refuses one model call leaves the
    chemist's question in the checkpoint and no transcript row) and after a turn cancelled
    mid-tool. This branch is the one place the runner *knowingly* keeps a turn it knows the
    transcript will not get, so it is the one place that can say so honestly; the others are not
    counted here and are named so that a zero is not read as "this cannot happen". Closing the
    class rather than counting it means projecting the transcript from the checkpoint stream, which
    `_record_transcript` already names as the alternative it declined — on cost, before divergence
    was the reason.
    """
    if ledger.answered or ledger.run_complete:
        if not ledger.answered:
            # `run_complete and not answered`: the graph finished and committed the exchange to the
            # checkpointer, and `_record_transcript` — which runs after `run_complete` is set —
            # never got there. The two records of one conversation now differ by exactly one turn.
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

    Read from config rather than off the built model, and in one expression:
    `model_routes["agent"]` wins where a deployment routes per task, and `llm_model` is what
    `build_chat_model` falls back to — validated non-empty, so there is nothing to default behind
    it. An `or settings.agent_model` tail stood here while that field existed; it was a vendor
    model id in git whose only other reader was the deleted Anthropic branch
    (`D-2026-09-04-a-gateway-is-the-only-provider`).

    **One turn can span models and this column names the agent's**, deliberately: the verifier's
    judge runs on the `verifier` route (F10-E), which may be a different, cheaper model, and its
    tokens are metered into the same turn. A column per route would be a schema that grows with the
    route table; the agent route is the one that produced the answer, and this is a comment saying
    so rather than a claim that the turn used exactly one model.
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
    # **What the provider never got to report, estimated rather than dropped.** A gateway puts
    # usage on the terminal frame only, so a turn cancelled mid-message — a disconnect under
    # `survives_disconnect=false`, the Stop button, the wall-clock deadline — metered exactly 0
    # against a prompt it had already been billed for (measured: 0/0 beside an identical completed
    # turn's 900/120). Booking it makes "drop the connection just before the answer" cost what it
    # costs. Zero on every ordinary turn, because a call that finishes is metered rather than
    # estimated; `InFlightPrompts` carries the arithmetic and its limits.
    #
    # **Ahead of `budget.record` because it is an *input* to it**, which is the one thing the rule
    # below permits in front of the booking: it is a sum of ints times a clamped float, with no
    # derivation in it that can fail, unlike the three that were moved out from under it.
    estimated = ledger.prompts.unbilled_tokens
    if budget is not None:
        budget.record(session.session_id, actor, ledger.usage.total + estimated)
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
            estimated_tokens=estimated,
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
            # Straight off the ledger, which counted them from the same event stream every other
            # count on this row comes from.
            retrieval_calls=ledger.retrieval_calls,
            capture_calls=ledger.capture_calls,
            answer_confidence=ledger.answer_confidence,
            review_required=ledger.review_required,
            notes_cited=ledger.notes_cited,
            # **Digested, because this row outlives the person.** The column's only consumer is
            # the distiller's self-confirmation guard, which asks whether *this* skill was acting —
            # an equality question a digest answers exactly as well as the name. The name itself is
            # a chemist's own words, and `turn_costs` is in `leaver._RETAINED` and refused by
            # `durable/retention.py`: measured, a skill called `project-nightingale-workup` was
            # still in the table after `erase_actor(apply=True)` reported success. The code claimed
            # the opposite ("erased with that person by `agent/leaver.py`"), which is the kind of
            # false statement about a control this repository exists to stop making.
            skills_loaded=sorted(skill_fingerprint(name) for name in ledger.skills_loaded),
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
        # **Beside the measured pair, never summed into it.** The budget is metered on the sum,
        # because a cost guard has to bind on the whole bill; what is *published* keeps the two
        # apart, so an inferred number can never pass for a provider's. It lands here, on the
        # turn's `turn_costs` row (migration 087) **and** on `chemclaw_estimated_tokens_total` —
        # three places, each answering a different question, and the third was missing until
        # 2026-09-06. This comment used to say a counter was the wrong instrument, citing
        # `agent/compaction._announce` on a distinction "a declared counter's label set cannot
        # carry". That is an argument against a *label* on the measured series, and it stands; a
        # separate series carries the distinction without touching what the four measured ones
        # mean, and a rate is what a deployment reads when nobody is looking at a row.
        estimated_tokens=estimated,
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
        # **The fifth is inferred, and it is a fifth series rather than part of the first.** The
        # estimate already reached the budget and the ledger row and stopped there, so the
        # fleet-wide rate under-reported by the whole prompt of every abandoned turn — measured
        # 2026-09-06, two identical turns against a gateway billing 42,448 each moved
        # `chemclaw_tokens_total` by 42,481 and **0**, which is exactly the population wave 4
        # identified as an attack ("drop the connection just before the answer"). The paragraph
        # below argued against carrying it and was right about a *label*: a declared counter's
        # label set cannot hold measured-versus-inferred without changing what every existing panel
        # means. It is a distinction a second series carries exactly, which is what the dashboard
        # panel beside `chemclaw_tokens_total` now shows.
        ("chemclaw_estimated_tokens_total", estimated),
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


async def turn_store() -> Any:
    """This turn's durable memory store, or `None` where the deployment keeps none.

    The two gates are `local_skills.personal_skills_available`'s, asked rather than restated:
    a third surface (`propose_skill`) read them by not reading them at all, which is the argument
    that function now carries. Building the store here rather than in `build_langgraph_agent` is
    what keeps that builder synchronous — the same seam the checkpointer already uses.

    The *third* gate is not here and that is deliberate: whether the turn has an actor is decided by
    `scratchpad_backend`, because that is where the namespace is computed and an actorless memory is
    one nobody could erase.

    **Public since `api/routes/skills.py` became the second caller**, and public rather than
    copied: the chemist's own skills ride this same store
    (`D-2026-09-18-a-skill-a-chemist-keeps-is-behaviour-they-approved`), so the two surfaces answer
    "is this available" from one function. A second spelling of these two conditions is how one of
    them gets a third condition later and the other does not.

    Returns:
        A ready `AsyncPostgresStore`, or `None` for a turn with a scratchpad but no memory.
    """
    if not personal_skills_available():
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
    uses, scoped to a subset of its kinds, so the two consumers cannot double-deliver one row and
    neither can starve the other of kinds it does not handle.

    The chemist's words lead and the push-back follows, framed
    (`chemclaw.agent.framing.frame_untrusted`) because a job summary is workflow output, not an
    instruction. Best-effort in both directions: a mailbox that cannot be read must not fail the
    turn, and a memory-backed deployment has no mailbox to read.

    **Bounded, because this is the only producer here that can make a `HumanMessage` of any size**
    (`D-2026-09-16-a-mailbox-nobody-bounded-is-a-human-message-nobody-bounded`). `claim_unconsumed`
    takes no limit and `ConnectorJobResult.summary` declares no maximum, so the block appended below
    is as long as the mailbox happens to be. Measured, one unbounded summary beside a
    maximum-length chemist message is **235,377 characters** — past deepagents'
    200,000-character `HumanMessage` offload threshold, which `agent/compaction.py` argues is
    unreachable and whose safety argument is that the undefanged preview is "a strict substring of a
    message that sat in the model's context verbatim, because a chemist's own message is not framed
    as untrusted data". This block is precisely *not* the chemist's words — it is framed because it
    is untrusted — and the preview is head-and-tail by *lines*, so with a five-line question it
    keeps the closing delimiter and drops the opening one, handing the model unframed workflow
    output terminated by a stray tag.

    So the summary is cut to `agent_max_tool_result_chars` before it is framed, by the same function
    that bounds one tool result, with a notice that names itself as system text. Cut *inside* the
    frame rather than after it, so the delimiters cannot be what a cut removes.
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
    so a turn killed before it answers leaves no transcript row — **and the checkpoint it leaves
    behind is not rolled back to match.** This paragraph used to end "so the two agree about what a
    half-turn is worth", and they do not: `_roll_back_unfinished` reverts `session.state` and
    nothing else, so a teardown landing after the graph run and before this call leaves the
    exchange in the model's record and out of the chemist's. Measured at `checkpoints: 8,
    session_messages: 0`; counted at `chemclaw_transcript_thread_divergence_total`; the whole
    argument, and why the exchange is kept rather than deleted, is in `_roll_back_unfinished`.

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
