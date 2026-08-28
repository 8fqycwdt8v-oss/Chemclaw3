"""The tool-audit trail: record every agent tool call once, from one place.

Why this exists: "who ran what, with which inputs, when, did it succeed, and to what
effect" must be answerable about work a chemist will cite, and it is also the first
thing needed to troubleshoot an agent turn. Rather than sprinkle logging into each of the ~13 tools
(duplication that would drift), one **tool-call middleware** wraps *every* registered
tool uniformly — the audit trail is a single reusable piece (DRY), like the PR-gate.

It is observe-only: it never alters the arguments or the result. Each call records the
correlation id (which conversation), the actor (who — a Phase-6 seam, the configured
`service_actor_id` until Entra identity lands), the tool name, its truncated arguments, the
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

**And a refusal is not a failure, which made four outcomes.** Every governance gate — authorization,
the dry-run guard, the undeclared-write refusal, the plan gate, the repeat guard — stops a call by
raising, so all five landed in the `except Exception` clause as `outcome='error'` beside a genuine
`KeyError` in a parser. The log line was no better: it interpolated the exception *instance*, so
even the class was lost and the only thing separating "the system declined, on purpose" from
"something broke" was free-text prose in an unindexed column. The audit **row** kept the class,
because `bounded_repr` reprs a non-string — so the database was strictly more diagnostic than the
log, inverting this module's own rule that the log is the floor and the sink is the durable record.
`refused` is the fourth outcome, `refusal_reason` is the classification, and both the class name and
the reason now reach the log line. The reason is counted from *here* rather than from the five
gates: four of them moved no metric at all, and a gate that has to remember to count itself is a
gate that eventually does not.

Note: tool arguments and confirmed-answer payloads are user free text, so audit records may
contain PII. `agent_audit_max_arg_chars` bounds what is stored; treat the trail accordingly.
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
from chemclaw.connectors.transport import SERVED_BY
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

# The outcome a call earns when a governance gate stopped it. Beside `ok`, `error` and `cancelled`
# rather than folded into `error`, because a refusal and a crash are different events with
# different remedies and the trail recorded them identically.
REFUSED = "refused"


@cache
def _refusal_types() -> tuple[tuple[type[BaseException], RefusalReason], ...]:
    """The governance refusals, most specific first, each paired with the reason it records.

    **One table, read from one place.** Four of the five gates moved no metric at all, and the
    fifth counted itself — so "why did the agent not do the thing" was answerable only by a `LIKE`
    scan of an unindexed free-text column. Classifying here rather than in each gate is the same
    move the audit trail itself is: the decision is recorded once, by the middleware every call
    passes through, so a new gate cannot forget to count itself as long as it raises one of these.

    **Imported inside the function because `agent/tool_authz.py` imports this module.** The gates
    are recorded *by* the trail, so the dependency runs one way at import time and the other way at
    classification time. Cached because the tuple is fixed for the life of the process and this
    sits on the exception path of every tool call.

    Order is the classification: every type above the last is an `AuthorizationError` subclass, so
    a linear scan gives the most specific reason. What reaches the base is a plain role denial from
    `authz.authorize_tool` and `skill_backend.SkillsReadOnlyRefusal` — both exactly "you may not".
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

    The one predicate that separates "the system declined, on purpose, and said so" from "something
    broke". Both arrived as `outcome='error'` and as a log line reading `tool X failed after N ms`
    whose only distinguishing content was the exception's *message* — `audit.py` interpolated the
    exception instance, so even the class was lost, and a `DryRunRefusal` could not be told from a
    `KeyError` in a parser by any query an operator can write.
    """
    for kind, reason in _refusal_types():
        if isinstance(exc, kind):
            return reason
    return None


# What an unregistered tool name becomes on a metric label. One bucket, not one series per string.
UNKNOWN_TOOL = "unknown"


def metric_tool_name(request: Any) -> str:
    """The tool name safe to use as a metric label — the registered one, or a fixed bucket.

    **`name` is the model's string, and a metric label must not be.** `ToolNode` invokes this chain
    for a name the graph does not hold — `_served_by` records that it passes `tool=None` there
    deliberately, so an interceptor can short-circuit an unregistered call — so the raw name
    reaching `/metrics` mints one time series per string a model invents. Measured on a compiled
    graph: a single hallucinated call created `chemclaw_tool_calls_total{tool="totally_made_up_…"}`
    *and* a full fourteen-bucket histogram, and driven directly the label accepted 230 characters of
    arbitrary text. Model output is attacker-influenceable here — that is the whole reason this tree
    carries `frame_untrusted` — so an injected document could grow the registry until the pod died.

    The registered tool's **own** name is used rather than the caller's, so the label cannot differ
    from it by case, whitespace or an invisible character while still resolving. It therefore takes
    no `name` argument: it had one, every caller passed `request.tool_call["name"]`, and the body
    never read it — a parameter whose value is exactly the string this function exists to refuse
    reads like the clamp is a comparison, and it is not.

    The audit *row* keeps the model's raw string (truncated), because what the model actually asked
    for is the forensic fact; it is only the unbounded *label* that is refused.
    """
    registered = getattr(getattr(request, "tool", None), "name", None)
    return registered if isinstance(registered, str) and registered else UNKNOWN_TOOL


def _observe_tool_latency(name: str, elapsed_ms: float) -> None:
    """Record one tool call's duration in the process histogram, under the tool's own name.

    Here rather than in `chemclaw.api.runner` because this is the only place that sees a tool call
    *complete* — the runner sees the model announce one and never learns when it returned. Failed
    calls are observed too: a tool that fails after 30 s is exactly the sample that explains a slow
    turn, and dropping it would make the histogram flatter the worse things get.

    **Labelled by tool, which it was not.** One distribution pooled a minutes-long xTB call through
    the calc connector with a sub-millisecond `read_attachment`, so per-tool p95 — the single most
    useful number for "why is this turn slow", and the question this histogram's own docstring says
    it exists to answer — could not be read off it.

    `name` here is already clamped by `metric_tool_name`; this docstring used to claim the label
    space "is bounded by configuration rather than by anything a caller sends", which was the
    assumption rather than the code — see that function for what it actually was.
    """
    record_metric(
        lambda metrics: metrics.observe(
            "chemclaw_tool_duration_seconds", elapsed_ms / 1000.0, labels={"tool": name}
        )
    )


def _count_outcome(name: str, outcome: str, reason: str | None) -> None:
    """Count one finished tool call, and the gate that stopped it when one did.

    Both counters move from here — one site, inside the middleware every call passes — rather than
    from the five gates, for the reason `_refusal_types` gives: a gate that has to remember to
    count itself is a gate that eventually does not.
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
    #
    # **`plan_step` below is not this field arriving late, and filling this one from it was
    # considered and refused.** They are different questions: this one promises a *reason*, and the
    # step is a position in a list — a call made during "run the conformer search" was not
    # necessarily made *because of* it. Copying one into the other would be exactly the inference
    # the paragraph above declines, and would spend the one column that is honest about being
    # empty. What changed is that `chemclaw.cli.explain` no longer renders this: an operator tool
    # whose "why" column is structurally blank teaches its reader that the trail sometimes knows
    # and here did not, which is false in both halves. It renders the answerable question instead.
    purpose: str = ""
    actor: str
    # Which specialist made this call — the `AgentProfile` name of the running subagent, empty for
    # the main agent (D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor, invariant 3).
    #
    # **Nothing writes it, and that is now visible rather than claimed.** The contextvar this field
    # was read from had no setter in `src/` for as long as it existed, so the column was empty on
    # every row ever written while three docstrings said the trail named the agent beside the human
    # (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`). The field and its
    # column stay because `infra/sql/006` is merged and a merged migration is never edited, and
    # because the shape is right for when subagents return: **beside `actor`, never instead of it**.
    # Overloading `actor` — the human's Entra oid — would produce exactly the D-040 failure this
    # system has already been bitten by: the trail recorded an agent's self-authorization under the
    # chemist's identity, which is worse than an unrecorded act because it *looks* attributable. And
    # `purpose` is reserved for why a call was made, which is a different question with a different
    # (still unanswerable) answer; filling it with an agent name would spend the one column that is
    # honest about being empty.
    agent: str = ""
    # The plan step this call served — the `content` of the first `in_progress` todo, or empty when
    # the call was not made from a plan step (`agent/plan_link.plan_link_for_call`, the same rule
    # and the same function a durable job's `job_records.plan_step` is stamped from).
    #
    # **Read off the request rather than off the ambient link, because the ambient link is not
    # bound here.** `stamp_plan_link` sits innermost in the governed chain and resets in a
    # `finally`, while this middleware is outermost — measured, `get_current_plan_link()` reads
    # `("", "")` at the moment the row is written. Calling the same function the stamp calls, over
    # the same request, is not a second answer to "what was the plan": it is the same answer, one
    # middleware further out. Calling it over `request.state["todos"]` instead — which is what this
    # did — *was* a second answer, and it was off by one step (see `_plan_step`).
    #
    # It is also the *wider* answer, deliberately. A refused call never reaches `stamp_plan_link`
    # at all (the gate raises above it), so the contextvar could never have named the step a
    # refusal interrupted — which is the row an operator most wants the step on.
    plan_step: str = ""
    tool: str
    arguments: str
    # "ok" | "refused" | "error" | "cancelled". Deliberately a plain string with no CHECK behind it:
    # the column has none (`infra/sql/006`), and adding one to an append-only table to police four
    # literals would cost a migration on every future outcome. The producer is this module alone.
    #
    # `refused` is the fourth, and it is the one that had to be *added* rather than merely
    # documented: a governance gate stopping a call and a parser raising `KeyError` were the same
    # `error`, in a column an auditor reads as "did this work". The classification is
    # `refusal_reason`, applied in `_recording` from the exception's type — never from its message,
    # which is what the log line was reduced to being distinguished by.
    outcome: str
    # Result summary on success, the exception text — or the failure the tool *returned*, for a
    # connector tool, which never raises — on failure, why the attempt was cut short on a
    # cancellation.
    detail: str = ""
    latency_ms: float
    # When the tool call *started*, stamped here rather than defaulted by the INSERT.
    #
    # **The trail used to carry the flusher's clock.** `PostgresAuditSink.record` buffers and
    # returns, `audit_events.ts` defaults to `now()` at INSERT and `id` is a `BIGSERIAL` assigned at
    # the same moment — so under load a turn's rows were both timestamped and *ordered* by whenever
    # a batch happened to drain, and `chemclaw explain` reconstructs a turn by that order. The start
    # rather than the end because that is the order the model emitted the calls in, which is the
    # causal order a reconstruction is trying to show; a long call that overlaps three short ones
    # still sorts where it was asked for.
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # The deployment revision (Git SHA / image digest) in effect for this call (AG-14): ties a past
    # result to the exact prompt/skill/config version that produced it. "unknown" until a deployment
    # sets `deployment_revision`.
    revision: str = "unknown"
    # The build of the *out-of-process server* that answered, when one did:
    # `<connector>@<revision>`, read off the MCP handshake (`connectors/transport.py::_stamped`).
    #
    # **Beside `revision`, because since the migration they answer different questions.** `revision`
    # named the whole system while the chemistry ran in this process. It now names the orchestrator
    # only: a `predict_pka` row records which commit of the *prompt and the routing* asked, and says
    # nothing about the build of the server that did the physics — which releases on another
    # repository's cadence and is the half a reproduction actually needs.
    #
    # Empty means no server was involved: an in-process tool's build *is* `revision`, so a stamp
    # would be a second copy of one fact. `<connector>@unknown` is the third case and a real one —
    # a remote server answered and could not name its build — deliberately distinguishable from
    # both, because an image built without its revision argument is a fixable mistake and an
    # in-process call is not a mistake at all.
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


def bounded_repr(value: object) -> str:
    """Render a value as a single-line string bounded by the configured budget.

    A tool argument or result can be a large object (a full optimization problem, an
    evidence sweep); truncating keeps one audit record from ballooning while still
    identifying the call and its effect.

    **The bound applies to the work, not only to the answer.** `repr(value)[:limit]` builds the
    whole repr first — a 100 kB MCP payload fully materialized on the event loop, twice per tool
    call, to keep 200 characters. A string is sliced before it is repr'd, and everything else goes
    through `reprlib` with its per-string budget set to this one; the container counts are raised
    far above `reprlib`'s defaults so an ordinary argument dict still renders whole, because the
    audit row's `arguments` is what a reviewer reads as *what was asked*.

    **Public, because the budget is the tree's one answer for a model-authored string.** Three
    things now reach a record straight out of the model's own output — a tool call's arguments, an
    unparseable call's argument document (`agent/model_calls.py`) and a skill path the model asked
    to read (`agent/skill_backend.py`) — and each is unbounded at the source. One budget, applied
    by one function, is what keeps a megabyte of model prose out of a row, a log line and a
    correction alike; a second truncation written beside it would be a second answer to the same
    question, differing by whichever limit its author happened to pick.
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


def _served_by(request: Any) -> str:
    """`"<connector>@<revision>"` when an out-of-process server answers this call, else `""`.

    The framework-facing half of `tool_revision`, kept here rather than in `_recording` for the same
    reason the `ToolMessage` test is: what crosses that boundary is a decision — a plain string —
    never a library object, so the trail's contents cannot come to depend on which engine ran.

    **The only place in either governance chain that reads `request.tool`, and it is safe here in a
    way it is not one middleware over.** `ToolNode` passes `tool=None` for a name the graph does not
    hold, deliberately, so interceptors can short-circuit an unregistered call — which is why
    `tool_authz` states that nothing in its chain reads this. A refusal that depended on the field
    would fail *open* on exactly the calls it exists to stop. This one is observational: `None`
    means no tool object, which means no connector, which means no server revision, which is the
    same empty string an in-process tool yields. The degenerate case is already the right answer.
    """
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    served = metadata.get(SERVED_BY)
    if not isinstance(served, dict):
        return ""
    return f"{served.get('connector', '')}@{served.get('revision', '') or 'unknown'}"


def _plan_step(request: Any) -> str:
    """The plan step this call serves, read off the request's own todo list.

    The framework-facing half of `AuditEvent.plan_step`, beside `_served_by` and split from
    `_recording` for the same reason: what crosses into the recording is a plain string, never a
    library object, so the trail's contents cannot come to depend on which engine ran.

    `plan_link_for_call` is the *same* function `agent/plan_link.py` stamps a durable job with, so
    a tool call and the job it launched cannot disagree about which step they served. Only the step
    is taken: the plan *hash* is a job's join to an approval decision, and an audit row already
    carries the session the plan belongs to.

    **It has to be that function and not `request.state["todos"]`, which is what this read was.**
    `request.state` is the snapshot `ToolNode` took *before* the batch, and the canonical harness
    batch — "tick step N completed, mark N+1 in_progress, call the tool" — carries the status flip
    in the same assistant message as the call. Measured on that batch: this row was stamped
    `'run the conformer search'` (the step that had just **finished**) while `job_records.plan_step`
    for the job the same call launched said `'compute the pKa'`. So the two records the sentence
    above promises cannot disagree disagreed on every ordinary step of every plan, and
    `chemclaw explain` rendered the previous step for each of them.

    A request with no todos — a profile without the harness, a subagent, a template step — gives the
    empty string, which reads as "this call was not made from a plan step".
    """
    return plan_link_for_call(request)[0]


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
            tool_revision=_served_by(request),
            plan_step=_plan_step(request),
            metric_name=metric_tool_name(request),
        ) as recorded:
            result = await handler(request)
            recorded.result = getattr(result, "content", result)
            # The `ToolMessage` test lives in `returned_failure`, and what crosses into `_recording`
            # is the *decision* — a string or nothing — never the library class. That is the same
            # line the name/arguments split above draws, and it is what keeps the recording itself
            # framework-free.
            failed = returned_failure(result)
            recorded.returned_error = None if failed is None else bounded_repr(failed.content)
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
    tool_revision: str = "",
    plan_step: str = "",
    metric_name: str = "",
) -> AsyncIterator[_Recorded]:
    """The trail itself, with no framework in it — both engines' middlewares are wrappers.

    Everything that makes this the *record* lives here: the identity precedence, the span, the
    latency histogram, the four outcomes, and the shielded write that survives a teardown. A
    second copy of it for the second engine would be the one duplication this system cannot
    afford — an audit trail that disagrees with itself depending on a config flag is not a trail,
    and the `cancelled` outcome exists precisely because a subtle omission here went unnoticed
    until it was measured (D-130).

    What each engine supplies is only the four things it alone knows: the tool's name, its
    arguments, the plan step the request was carrying, and — inside the block — its result.
    """
    args = bounded_repr(arguments)
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
    # The wall clock beside the monotonic one, because they answer different questions and neither
    # substitutes: `start` measures the call, `started_at` *dates* it. The row carries this rather
    # than letting the INSERT default to `now()`, which under a batching sink is the flusher's
    # clock — see `AuditEvent.ts`.
    started_at = datetime.now(UTC)

    def event_for(outcome: str, detail: str, elapsed_ms: float) -> AuditEvent:
        """This call's record under `outcome` — the identity fields resolved once, above."""
        return AuditEvent(
            correlation_id=event_cid,
            session_id=event_session,
            actor=event_actor,
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

        Every exit path below does these three things and only the values differ, so they are
        written once — the omission that produced the `cancelled` outcome (D-130) was exactly a
        path that forgot one of them.

        The span is stamped **here, inside the `with`**, because a span cannot be marked after it
        has ended. `Status(ERROR)` only where OpenTelemetry would not set one itself: a raised
        exception already sets it on the way out, so this covers the two failures that do not
        raise — a returned failure (an MCP tool never raises) and a cancellation, which is a
        `BaseException` that `use_span` does not catch. Both were measured `UNSET` while the audit
        row said otherwise, so an operator filtering a collector by `status=ERROR` saw neither.

        A **refusal is deliberately not an `ERROR` span** where the choice is ours. It raises, so
        OpenTelemetry marks it anyway; the `outcome` attribute is what lets an operator take
        policy decisions back out of an error view, and `chemclaw_tool_refusals_total{reason}` is
        where they are actually counted.
        """
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        span.set_attribute("outcome", outcome)
        if outcome in ("error", "cancelled") and detail:
            span.failed(detail)
        # The clamped name for the two metrics, the model's own for the span and the row: a label
        # is a cardinality decision and an attribute is not. Passed in rather than derived here,
        # because deriving it needs `request` and this function's whole point is that no library
        # object crosses into it — `tool_revision` is passed for the same reason.
        labelled = metric_name or name
        _observe_tool_latency(labelled, elapsed_ms)
        _count_outcome(labelled, outcome, reason)
        return elapsed_ms

    recorded = _Recorded()
    # One span per tool call, which with the turn span above it is the whole first-party
    # trace: "this question took 40 seconds and 31 of them were one xTB call" is the
    # question an operator actually asks, and nothing could answer it. Deliberately not a
    # span per loop iteration or per retriever — the finding was that the docs *overstate*
    # the tracing, and answering that with more unread spans is the same mistake mirrored.
    #
    # `correlation.id` rides on it so a trace and the audit trail can be joined in both
    # directions; the tool's name was already here. Nothing else — a span attribute travels
    # to the collector, so the rule is `/metrics`'s: identifiers, never an argument.
    with start_span("chemclaw.tool", **{"tool.name": name, "correlation.id": event_cid}) as span:
        try:
            yield recorded
        except asyncio.CancelledError:
            # The turn was torn down while this tool was still running — a client disconnect
            # or the front door's wall-clock deadline, which both deliver exactly this
            # (D-130). Its own clause because `CancelledError` derives from `BaseException`,
            # so the handler below never saw it and an interrupted attempt left no row in the
            # trail at all.
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
            # The class name beside the message, which is the whole fix on the log side: `%s`
            # on the exception instance rendered only its prose, so no log query could tell a
            # `DryRunRefusal` from a `KeyError`. The row always kept the class (`bounded_repr`
            # reprs a non-string), which is how the sink came to be more diagnostic than the
            # floor it is supposed to sit on.
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
            # The handler returned, and what it returned was a failure. Recorded exactly like
            # a raised one — same outcome, same WARNING — because the difference between
            # raising and returning is a property of the tool's transport, and an auditor
            # reading the `outcome` column is asking about the call's effect.
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
