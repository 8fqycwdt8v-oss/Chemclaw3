"""Executing one template step — the I/O half of `chemclaw.durable.template_job`.

`tool` and `agent` steps are non-deterministic network work, so they run as activities. A workflow
has no request context, so the turn's actor, roles, session and correlation id travel in the
input and are stamped ambient before the work runs (`_acting_as`): the audit trail names the real
user and conversation, and `enforce_tool_authz` decides against that user, so a template cannot
run a tool its requester could not. An `agent` step is metered like a chat turn (counters and
`turn_costs`), but not capped: `api/budget.py` lives in the front door's process.
"""

import asyncio
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from typing import Annotated, Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from temporalio import activity

from chemclaw.agent.context_budget import current_context
from chemclaw.agent.loop_cap import loop_capped
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.spend_cap import spend_capped
from chemclaw.agent.state import answer_text, turn_config, turn_input
from chemclaw.agent.tool_invocation import invoke_governed
from chemclaw.agent.tool_result_size import STEP_REMEDY, bounded_content
from chemclaw.agent.turn_ambient import turn_caps
from chemclaw.agent.turn_cost import TurnCost, record_turn_cost
from chemclaw.agent.turn_usage import TurnUsage, llm_result_usage
from chemclaw.connectors.jobs import prepare_job_launch
from chemclaw.connectors.queues import bundle_queue
from chemclaw.connectors.registry import find_job, open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.durable.governed_launch import audited_launch
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity

logger = logging.getLogger(__name__)


def _agent_surface() -> Any:
    """Import the tool surface lazily, at call time rather than at module import.

    `chemclaw.agent.chemclaw_agent` reaches the template registry, which reaches this module, so a
    top-level import is a cycle; deferring also keeps the agent stack out of worker start-up.
    Returns the in-process capability tools and the connector specs — the same functions a chat
    turn's graph is built from, so both paths agree on which tools exist.
    """
    from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs

    return _capability_tools, connector_specs


class StepIdentity(BaseModel):
    """Who a template run is acting for — carried in every step's input, stamped before it runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Stripped, then refused blank: a whitespace actor would be stamped as a principal nobody is.
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    roles: list[str] = Field(default_factory=list)
    # Ties this run's audit events together, exactly as a conversation's correlation id does, so a
    # template's steps are one traceable unit in the trail rather than N unrelated tool calls.
    correlation_id: str = Field(min_length=1)
    # The chat that launched the run, stamped ambient by each step so audit rows book the
    # conversation. Empty off the service path, where there is no session.
    session_id: str = ""


class ToolStepInput(BaseModel):
    """One resolved `tool` step: which tool, with which already-substituted arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    identity: StepIdentity


class AgentStepInput(BaseModel):
    """One resolved `agent` step: the rendered prompt, the profile, and the writes it may reach.

    `write_tools` is carried across the activity boundary rather than re-read from the template
    file, for the reason the whole resolved template travels in the workflow's input: a worker that
    re-read `data/templates/<name>.yaml` would decide a *security* narrowing from the disk it
    happens to have, and an edit could then widen a run already in flight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    profile: str | None = None
    write_tools: list[str] = Field(default_factory=list)
    identity: StepIdentity
    # This step's id within the template, so each `agent` step's cost row is distinguishable within
    # a run that shares one correlation id. Defaulted so an input from older code still decodes.
    step_id: str = ""
    # The run's template name, used only to label the prompt-truncation counter; a step's behaviour
    # must not depend on it. Defaulted because a rolling deploy can hand an old workflow's input to
    # a new activity worker.
    template: str = ""


class AgentStepResult(BaseModel):
    """One `agent` step's answer **and what was missing while it was produced**.

    The step used to return a bare `str`, and that is the whole defect this model exists to close:
    a step whose connectors never came up, or whose model loop was stopped by its iteration cap,
    returned a string byte-identical in *shape* to a complete one — so the next step of the
    template read a partial answer as the finished article, `template_job_record` wrote a row with
    nothing on it saying so, and the brief a chemist signs carried no notice. Measured on the real
    activity against one scripted model: complete and connectors-unreachable both returned
    `'FINAL: five hazard flags; two are severe.'`, and the capped run returned its interim sentence
    with `state` unset on the run's record either way.

    The fact was not missing, it was *discarded*. `open_connector_specs` already reports which
    bundles did not open (`api/runner.py` yields `CapabilityDegradedEvent` off exactly that, and
    the `tool` step already names them in its own failure), and `loop_capped`/`spend_capped` were
    already read here — but only into `turn_costs.outcome`, a cost ledger the next step and the
    final artifact cannot see. The comment above the call in `run_agent_step` had said as much
    since the day that fix landed: "a truncated runaway booked `outcome="answered"` and handed the
    next step of the template a partial answer with nothing saying so." It landed in the ledger
    only. This is the other half.

    `outcome` is `turn_costs.outcome`'s vocabulary rather than a second one, for the reason
    `_book_step_spend` gives for spelling that vocabulary out: one word must mean one thing on
    every writer, and this model is fed from the same variable the ledger row is.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str = ""
    # How the turn ended, in `turn_costs.outcome`'s words. A step that raised has no result, so
    # `errored` and `abandoned` are booked by the ledger and never constructed here.
    outcome: str = "answered"
    # The connector bundles that did not come up for this step. Names, not counts, because
    # "2 unreachable" sends nobody anywhere — the same reason `_invoke` renders them by name.
    unreachable: list[str] = Field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """Whether this answer was produced with something missing."""
        return bool(self.unreachable) or self.outcome != "answered"

    def notice(self) -> str:
        """One sentence naming what was missing, or `""` for a clean step.

        Written as system text, not in the model's voice, because it is prepended to the model's
        prose.
        """
        if not self.degraded:
            return ""
        missing = []
        if self.outcome == "loop_capped":
            missing.append("the step reached its model-call cap before it finished")
        if self.outcome == "spend_capped":
            missing.append("the step reached its token budget before it finished")
        if self.outcome == "empty_answer":
            missing.append("the step produced no answer")
        if self.unreachable:
            missing.append(
                f"{len(self.unreachable)} capability bundle(s) were unreachable: "
                f"{', '.join(sorted(self.unreachable))}"
            )
        return f"[INCOMPLETE — {'; '.join(missing)}]"

    def step_value(self) -> str:
        """What the next step and the run's record see: the notice, then the answer.

        Prepended so a reader who stops early, or a cut from the end, never loses the notice.
        """
        notice = self.notice()
        return f"{notice}\n\n{self.answer}" if notice else self.answer


class JobStepInput(BaseModel):
    """One resolved `job` step: which declared job, with which already-substituted arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    identity: StepIdentity


class ResolvedJob(BaseModel):
    """Where a declared job runs, and the payload it was authorized to run with.

    `payload` is here because resolution and authorization are one act, not two (D-168). The
    activity that resolves a job step is the same activity that validated its arguments, checked
    `authorize_trigger` against the requester and ran the job's declared precondition — so handing
    back the *validated* payload is what stops the workflow starting a child with the raw,
    unchecked arguments it happened to have. There is no representable state in which a caller
    holds a `ResolvedJob` and has not passed the pre-flight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str
    job: str
    workflow: str
    task_queue: str
    publish_to_graph: bool
    # The job's declared runtime ceiling (`JobSpec.timeout_seconds`), or `None`. Resolved here so a
    # template step and a chat launch of the same job get the same ceiling.
    timeout_seconds: float | None = None
    # Whether the job waits on a person, so `child_execution_timeout` gives it no ceiling. Every
    # field the job wrapper reads off a manifest must travel this model;
    # `tests/test_template_job_step.py` derives that set from `JobSpec` and `ConnectorJobInput`.
    awaits_answer: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


@contextmanager
def _acting_as(identity: StepIdentity) -> Iterator[None]:
    """Run a step as its requester, in their conversation, under their correlation id.

    One bracket for all ambients because they are one fact: a step acts for a person, in a chat,
    within one request. Logs, notes and launched jobs read the ambient values to join back to the
    turn. A context manager so callers can scope it to part of their body, and so every set has its
    reset — a leak would carry one run's identity into the worker's next task.

    `durable/interceptor.py` binds the same identity around every activity, but with empty roles;
    this bracket binds the real role set (see the comment below). Both are needed and must not be
    merged.
    """
    # The template path binds `identity.roles`, unlike the interceptor, which binds empty because a
    # workflow payload is unsigned. `authorize_job_step` is the first authorization a template step
    # gets, so binding empty here would refuse every entitled job step. The residual trust is that
    # only trusted code can enqueue a `TemplateWorkflow` (broker access restricted by Temporal
    # mTLS); closing it fully needs a signed payload codec.
    identity_token = set_current_identity(identity.actor, frozenset(identity.roles))
    session_token = set_current_session_id(identity.session_id)
    correlation_token = set_current_correlation_id(identity.correlation_id)
    try:
        yield
    finally:
        reset_current_correlation_id(correlation_token)
        reset_current_session_id(session_token)
        reset_current_identity(identity_token)


@durable_activity("background")
@activity.defn
async def authorize_job_step(step: JobStepInput) -> ResolvedJob:
    """Resolve, validate and authorize one `job` step as its requester — outside the workflow.

    Runs `chemclaw.connectors.jobs.prepare_job_launch`, the same pre-flight the chat launcher uses
    (expensive-work authorization, the job's precondition, audit), with the step's identity stamped
    first.

    An activity rather than workflow code for two reasons. Resolving a job reads the connector
    registry from disk, which would make replay depend on the replaying worker's bundle set; the
    activity records the answer in history. And a `ConnectorError` raised in workflow code suspends
    the run in the SDK's task-failure loop forever, while across an activity boundary it fails it.
    Both `ConnectorError` and `AuthorizationError` are listed by name in `BAD_DATA_RETRY`
    (`chemclaw.durable.publish`), so a bad job name or an unentitled step fails on the first
    attempt with its reason.
    """
    connector, job = find_job(step.job)
    with _acting_as(step.identity):
        payload = await audited_launch(
            job.name,
            step.arguments,
            lambda: prepare_job_launch(connector, job, step.arguments),
            actor=step.identity.actor,
            correlation_id=step.identity.correlation_id,
        )
    return ResolvedJob(
        connector=connector,
        job=job.name,
        workflow=job.workflow,
        task_queue=bundle_queue(connector),
        publish_to_graph=job.publish_to_graph,
        timeout_seconds=job.timeout_seconds,
        awaits_answer=job.awaits_answer,
        payload=payload,
    )


# On the background queue, so the background worker registers it.
@durable_activity("background")
@activity.defn
async def run_tool_step(step: ToolStepInput) -> Any:
    """Call one tool as the run's actor, through the same audit + authz chain a chat turn uses.

    The tool is found by name on the assembled surface (`_capability_tools` plus `connector_specs`),
    the same answer to "which tools exist" a conversation uses. No agent is built.
    """
    capability_tools, connector_specs = _agent_surface()
    with _acting_as(step.identity):
        async with AsyncExitStack() as stack:
            connector_tools, unreachable = await open_connector_specs(stack, connector_specs())
            # Heartbeat while the tool runs: an MCP call has no progress boundary, and without a
            # beat a worker killed mid-call is detected only when the whole per-step timeout
            # expires.
            return await beating(
                _invoke([*capability_tools(), *connector_tools], step, unreachable),
                f"template tool step {step.tool}",
                settings.template_step_heartbeat_timeout_seconds,
            )


async def _invoke(tools: list[Any], step: ToolStepInput, unreachable: list[str]) -> Any:
    """Find `step.tool` on the assembled surface and call it, or raise naming what exists.

    `unreachable` makes the failure legible: a connector that did not come up contributes no tools,
    and the error should blame the host, not the template.
    """
    # One list and one call path for in-process and connector tools, so both go through audit and
    # `enforce_tool_authz`.
    for tool in tools:
        if getattr(tool, "name", None) == step.tool:
            return await _call_governed(tool, step)
    available = sorted(str(getattr(t, "name", "")) for t in tools)
    degraded = f" ({len(unreachable)} unreachable: {', '.join(unreachable)})" if unreachable else ""
    raise ValueError(
        f"template step names unknown tool {step.tool!r}{degraded}; available: {available}"
    )


async def _call_governed(tool: Any, step: ToolStepInput) -> Any:
    """Invoke one tool through the same middleware chain a chat turn applies.

    A template does not go through `create_agent`'s tool node, so calling the tool directly would
    run it ungoverned. `invoke_governed` folds the identical list from the identical builder, so the
    two paths cannot drift. MCP results arrive as content blocks Temporal cannot serialise, hence
    `_mcp_text`.
    """
    message = await invoke_governed(
        tool,
        step.arguments,
        correlation_id=step.identity.correlation_id,
        actor=step.identity.actor,
        profile=get_profile(None),
        want_message=True,
    )
    structured = _structured(message)
    return structured if structured is not None else _mcp_text(getattr(message, "content", message))


def _structured(message: Any) -> Any:
    """The MCP tool's `structuredContent`, if it sent one — the shape a later step can walk.

    Without it a tool result reaches the resolver as text, and `${steps.<id>.result.<field>}` fails
    inside the workflow. `langchain_mcp_adapters` puts `structuredContent` in the tool's artifact,
    which `ainvoke(args)` discards. Read defensively: the artifact is an upstream TypedDict and an
    in-process tool has none.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        return structured if isinstance(structured, dict) else None
    return None


def _mcp_text(result: Any) -> Any:
    """Flatten an MCP tool's content blocks into the text a step's result should carry.

    MCP blocks are not a type Temporal's converter knows. Text parts are joined; a result with no
    text parts falls back to `str()` so a step never fails on the shape of a value it produced.
    Matched on being a list of content blocks, not on a `.type` attribute, because structured
    results such as `NoteRef` also have `type`.
    """
    if (
        isinstance(result, list)
        and result
        and all(isinstance(item, dict) and "type" in item for item in result)
    ):
        texts = [str(item["text"]) for item in result if item.get("text")]
        return "\n".join(texts) if texts else str(result)
    return result


class _StepMeter(AsyncCallbackHandler):
    """Accumulates a step's token spend as each model call *ends*, rather than after the turn does.

    A sum taken after `ainvoke` returns never runs when the turn raises (provider error, recursion
    ceiling), which would book a runaway as costing nothing. `on_llm_end` fires once per model call,
    streamed or not, so it cannot double-count. `llm_result_usage` is the chat path's reader too, so
    both paths price tokens the same way.
    """

    def __init__(self) -> None:
        self.usage = TurnUsage()

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Add what one finished model call reported to this step's running total.

        Args:
            response: The call's result. `generations` is a list per prompt, each a list of
                candidates; both are walked.
            **kwargs: The rest of the callback contract, unused.
        """
        self.usage.add(llm_result_usage(response))


class ResumeRequest(BaseModel):
    """Which run to look for completed steps from, and the definition they must belong to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    # The fingerprint of the *resolved* template this run executes. The run id hashes only name and
    # inputs, so a relaunch after an edit shares the id; this refuses folding old steps into a new
    # procedure.
    fingerprint: str = Field(min_length=1)


#: The recorded ends a relaunch may resume from. A cancelled run's completed steps are as real as a
#: failed one's, and `ALLOW_DUPLICATE_FAILED_ONLY` lets the id start again after either. A terminate
#: or execution timeout runs no workflow code and writes no row.
_RESUMABLE = frozenset({"failed", "cancelled"})


@durable_activity("background")
@activity.defn
async def completed_steps(request: ResumeRequest) -> dict[str, Any]:
    """The steps a previous failed or cancelled run of `request.job_id` already finished, or `{}`.

    An activity because it is a database read, and its result is recorded in history so a replay
    folds the same steps. Resumes only when the row exists, is `_RESUMABLE` (rows are upserted on
    `job_id`, so a completed run must not resume itself), and its fingerprint matches. Best-effort:
    an unreadable resume is a slower run, never a failed one.

    Args:
        request: The run to resume and the definition its steps must belong to.

    Returns:
        `{step_id: result}` for the steps already done, empty when there is nothing to resume.
    """
    from chemclaw.durable.job_record import lookup_job_record

    try:
        record = await lookup_job_record(request.job_id)
    except Exception:
        logger.warning("could not read %s to resume it; starting over", request.job_id)
        return {}
    if record is None or record.state not in _RESUMABLE:
        return {}
    result = record.result if isinstance(record.result, dict) else {}
    if result.get("template_fingerprint") != request.fingerprint:
        if result.get("steps"):
            logger.info(
                "not resuming %s: its completed steps belong to a different version of %r",
                request.job_id,
                record.job,
            )
        return {}
    steps = result.get("steps")
    return dict(steps) if isinstance(steps, dict) else {}


def bounded_prompt(step: AgentStepInput) -> str:
    """`step.prompt` cut to what a model may be handed in one blob, and said so in the text.

    A `tool` step runs without the chat path's result bound, and its result is interpolated into the
    next step's prompt; a single-message prompt is unreducible by compaction. Bounded here, in the
    activity, because a setting read in workflow code would become a replay-time argument and break
    determinism if the ceiling changed. Uses `agent_max_tool_result_chars`, the one ceiling on text
    reaching a model in one blob. Head-and-tail, so the instructions at both ends survive and the
    interpolated middle is cut.

    Args:
        step: The resolved step. `template` labels the counter; `prompt` is what is bounded.

    Returns:
        The prompt, unchanged when it was already inside the ceiling.
    """
    bounded, removed = bounded_content(
        step.prompt,
        # Named for what it is rather than for a tool, because no tool returned it: the sentence
        # the model reads says "this <name> result", and "template step input" is the true filler.
        "template step input",
        settings.agent_max_tool_result_chars,
        remedy=STEP_REMEDY,
    )
    if not removed:
        return step.prompt
    METRICS.increment("chemclaw_template_prompt_truncated_total", 1.0, {"template": step.template})
    log_event(
        logger,
        "template.prompt_truncated",
        "cut %d characters from the %s step's prompt to stay inside the context ceiling",
        removed,
        step.step_id or "agent",
        template=step.template,
        step=step.step_id,
        characters_removed=removed,
        ceiling=settings.agent_max_tool_result_chars,
    )
    return str(bounded)


@durable_activity("background")
@activity.defn
async def run_agent_step(step: AgentStepInput) -> AgentStepResult | str:
    """Run one agent turn as the run's actor and return its answer **with its degradation**.

    Returns `AgentStepResult`, or `str` from older code: during a rolling deploy a new workflow can
    be served by an old activity worker, and the sequencer reads both shapes.

    `profile` narrows which agent runs the step, resolved once by `step_profile` and used for both
    the connectors and the graph. The harness is off: its todo list and plan loop are the discretion
    a template removes, and its plan gate would block every write. The step is ungated and read-only
    by default — every side-effecting tool it did not declare is removed. An agent-authored template
    can declare none (`templates/composed.py::authored_problems`).

    Metered like a chat turn (counters and a `turn_costs` row) but not capped: `api/budget.py` lives
    in the front door's process. All per-turn cap ambients are opened via
    `agent.turn_ambient.turn_caps`, including the repeat guard.
    """
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    _capability_tools, connector_specs = _agent_surface()
    profile = step_profile(step.profile, step.write_tools)
    started = time.perf_counter()
    meter = _StepMeter()
    answered = False
    # The floor outcome: a step that ran to its end and produced nothing. Every other ending
    # overwrites it before the `finally` books it.
    outcome = "empty_answer"
    # Every cap ambient a turn runs under: context record, loop and spend watches. `meter.usage` is
    # passed so the caps are enforced against the same object `_book_step_spend` reads.
    step_label = f"template {step.template or '?'} step {step.step_id or '?'}"
    with turn_caps(meter.usage, closing=step_label), _acting_as(step.identity):
        try:
            async with AsyncExitStack() as stack:
                # `unreachable` is kept so the answer can say which capabilities were dark.
                connectors, unreachable = await open_connector_specs(
                    stack, connector_specs(profile)
                )
                # Compiled per step: a graph binds its tools at construction and a connector session
                # belongs to one caller. No checkpointer: Temporal makes the run durable, and a step
                # is one bounded turn.
                graph = build_langgraph_agent(
                    profile=profile,
                    actor=step.identity.actor,
                    correlation_id=step.identity.correlation_id,
                    connectors=connectors,
                )
                # The loop and spend caps apply even with the harness off; they stop a turn by
                # returning, so the readers below make that visible instead of booking a truncated
                # runaway as `answered`. Beyond them `turn_config()` sets `agent_recursion_limit`,
                # which raises with no partial answer — which is why the meter is a callback on
                # `turn_config()`.
                result = await beating(
                    graph.ainvoke(
                        turn_input(bounded_prompt(step)),
                        {**turn_config(), "callbacks": [meter]},
                    ),
                    f"template agent step {step.step_id or step.profile or 'agent'}",
                    settings.template_step_heartbeat_timeout_seconds,
                )
                answer = answer_text(result)
                # An empty answer is not an answer, matching the chat path.
                answered = bool(answer)
                # Caps before `answered`, in `api/runner._settle_outcome`'s ranking, so both writers
                # of `turn_costs` share one vocabulary. `completed` stays answered: the partial
                # answer was delivered.
                if loop_capped(result):
                    outcome = "loop_capped"
                elif spend_capped(result):
                    outcome = "spend_capped"
                else:
                    outcome = "answered" if answered else "empty_answer"
                # Returned as well as booked: the next step, the run record and the reader cannot
                # see the ledger.
                return AgentStepResult(answer=answer, outcome=outcome, unreachable=unreachable)
        except asyncio.CancelledError:
            # A Temporal cancellation (workflow cancelled, worker draining, timeout). Temporal owns
            # the clock and gives no reason, so `timed_out` is not guessed.
            outcome = "abandoned"
            raise
        except Exception:
            # Everything else, including the recursion ceiling: it raises with no answer, so it is
            # `errored`, not `loop_capped`.
            outcome = "errored"
            raise
        finally:
            # Booked on every path, including failure and cancellation: a step that broke after
            # three model calls still spent them. Only an in-flight call at cancellation goes
            # unbooked. Inside `turn_caps`, because the row reads the context watch the manager
            # tears down on exit.
            _book_step_spend(step, meter.usage, time.perf_counter() - started, answered, outcome)


def _book_step_spend(
    step: AgentStepInput, usage: TurnUsage, duration_seconds: float, answered: bool, outcome: str
) -> None:
    """Publish one agent step's spend: the five counters, and the durable per-turn cost row.

    The same instruments and labels as the chat path: counters for the fleet-wide rate, `turn_costs`
    for per-actor attribution. Counters are skipped at zero so no fabricated series appears.
    `outcome` uses `api/runner._OUTCOMES`' vocabulary, spelled out because `durable` may not import
    `api`. The step id is appended to the correlation id so a human can tell which step a row
    belongs to; rows are keyed by a server-minted `turn_id`.

    Args:
        step: The step whose turn just ended — its profile labels the spend, its identity bills it.
        usage: What that turn's model calls reported, already summed.
        duration_seconds: Wall clock for the step, for the ledger's duration column.
        answered: Whether the step produced its answer. Recorded, not filtered — see `TurnCost`.
        outcome: How the step ended, in `turn_costs.outcome`'s vocabulary.
    """
    labels = {"profile": step.profile or "default"}
    context = current_context()
    record_turn_cost(
        TurnCost(
            correlation_id=(
                f"{step.identity.correlation_id}:{step.step_id}"
                if step.step_id
                else step.identity.correlation_id
            ),
            session_id=step.identity.session_id,
            actor=step.identity.actor,
            profile=step.profile or "default",
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            duration_seconds=duration_seconds,
            completed=answered,
            outcome=outcome,
            compacted=context.compacted if context is not None else False,
            context_unreducible=context.unreducible if context is not None else False,
        )
    )
    if usage.total:
        METRICS.increment("chemclaw_tokens_total", float(usage.total), labels)
    for name, value in (
        ("chemclaw_input_tokens_total", usage.input),
        ("chemclaw_output_tokens_total", usage.output),
        ("chemclaw_cache_read_tokens_total", usage.cache_read),
        ("chemclaw_cache_write_tokens_total", usage.cache_write),
    ):
        if value:
            METRICS.increment(name, float(value), labels)


def step_profile(profile: str | None, write_tools: Sequence[str]) -> AgentProfile:
    """The step's profile: harness off, and no write it did not declare.

    One profile for both halves of the surface (connectors and graph), so a narrowing covers both.

    - `harness_enabled=False`, for the reasons `run_agent_step` gives.
    - `tool_names = advertised − (side-effecting − declared)`: the read-only default, applied
      through the profile's own `tool_names` dial, which narrows in-process tools, connector
      allow-lists and skills together. Starting from `advertised_tool_names` keeps the profile's
      own narrowing, so this only subtracts.

    The classification is `chemclaw.agent.authz.side_effecting_tools()`, shared with the dry-run
    guard and the plan gate. `declared` is intersected, never added, so a step cannot gain a tool
    its profile did not advertise.

    Args:
        profile: The profile the step named, or `None` for the default agent.
        write_tools: The side-effecting tools this step declared it may reach.

    Returns:
        An `AgentProfile` copy, narrowed. Never the registered object — profiles are shared,
        process-lived and frozen.
    """
    # Lazily, to avoid the import cycle `_agent_surface` describes.
    from chemclaw.agent.authz import side_effecting_tools
    from chemclaw.agent.chemclaw_agent import advertised_tool_names

    prof = get_profile(profile)
    advertised = advertised_tool_names(prof)
    declared = advertised & frozenset(write_tools)
    return prof.model_copy(
        update={
            "harness_enabled": False,
            "tool_names": advertised - (side_effecting_tools() - declared),
        }
    )
