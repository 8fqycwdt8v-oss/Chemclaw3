"""Executing one template step — the I/O half, and why these are activities not workflow code.

A `tool` step calls a tool and an `agent` step runs a model turn: both are non-deterministic
network work, so neither can live in the workflow. `chemclaw.durable.template_job` sequences; this
works.

The part worth reading carefully is the identity restoration. A workflow has no request context, so
the turn's actor, roles, session and correlation id travel in the activity's input and are stamped
ambient here *before* the work runs (`_acting_as`) — which is what makes the audit trail name the
real user, in the real conversation, makes every note, launch and log line the step produces carry
the id that joins them back to the turn that asked, and, more importantly, makes
`enforce_tool_authz` decide against that user rather than against nobody. A template must not
become a way to run a tool the requester could not run directly, and this is where that is
enforced.

**The same sentence applies to what a step costs, and used to be false of it.** An `agent` step is a
model turn, so it is metered like one: the token counters, the `turn_costs` ledger and the per-turn
repeat guard are all started and booked by `run_agent_step`. Before that they were not, and the
consequence was measured on this activity — `chemclaw_tokens_total` 0.0 before and 0.0 after a turn
reporting 240 tokens — which made a template a way to spend model tokens that nothing counted. What
is deliberately *not* here is enforcement: `api/budget.py` lives in the front door's memory and a
worker is a different process, so this meters honestly rather than pretending to cap.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.tools import tool as tool_decorator
from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from chemclaw.agent.context_budget import (
    begin_context_watch,
    current_context,
    end_context_watch,
)
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.repeat_guard import begin_call_watch, end_call_watch
from chemclaw.agent.state import answer_text, turn_config, turn_input
from chemclaw.agent.tool_invocation import invoke_governed
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
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity

logger = logging.getLogger(__name__)


def _agent_surface() -> Any:
    """Import the tool surface lazily, at call time rather than at module import.

    `chemclaw.agent.chemclaw_agent` reaches the template registry (a template becomes a tool like
    any other), which reaches the workflow, which reaches this module — a cycle at import time.
    Deferring it breaks the cycle and is what an activity should do regardless: it runs on a
    worker, long after import, and pulling the whole agent stack into every module that merely
    *mentions* an activity is how a worker's start-up cost quietly triples.

    Returns the two halves a step can name: the in-process capability tools and the connector
    specs. Both are the *same* functions a chat turn's graph is built from, which is the property
    that keeps a template's idea of "which tools exist" from drifting from a conversation's.
    """
    from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs

    return _capability_tools, connector_specs


class StepIdentity(BaseModel):
    """Who a template run is acting for — carried in every step's input, stamped before it runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    # Ties this run's audit events together, exactly as a conversation's correlation id does, so a
    # template's steps are one traceable unit in the trail rather than N unrelated tool calls.
    correlation_id: str = Field(min_length=1)
    # The chat that launched the run, stamped ambient by each step exactly as the actor is, so
    # `agent/audit.py` books the conversation on every row a template writes. It read `""` on every
    # one of them before this field existed — the id was carried in `TemplateRunInput` and used
    # only for the completion push-back, so the trail could say who and which run but never which
    # conversation. Empty off the service path, where there is no session (`TemplateRunInput`).
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
    # This step's id within the template, carried for one reason: the cost ledger. `turn_costs`
    # upserts on the correlation id (`agent/turn_cost_store.py`) and every step of a run shares the
    # run's, so two `agent` steps would collapse into one row reporting the second's spend as the
    # whole run's. Defaulted rather than required so an input already in flight when this shipped
    # still decodes — such a run books one row per step id it has, which for the shipped template
    # is one step.
    step_id: str = ""


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
    # The job's declared runtime ceiling (`JobSpec.timeout_seconds`), or `None` where it declared
    # none. Resolved here with the rest of the job, so a template step and a chat launch of the
    # same job get the same ceiling — the two ids this step once dropped are the standing reminder
    # that a field the template path does not carry is a field that silently means something else
    # on that path.
    timeout_seconds: float | None = None
    # And its sibling, which arrived on `JobSpec` after that reminder was written and went missing
    # in exactly the way the reminder describes: `awaits_answer` says the job spends wall clock
    # waiting on a person, so `child_execution_timeout` hands it no ceiling. Absent here it
    # defaulted to False, and a campaign that a chat turn may run for a fortnight was killed at the
    # five-hour fleet ceiling when the same job was a template step. Every field the wrapper reads
    # off a manifest now travels this model — `tests/test_template_job_step.py` derives that set
    # from `JobSpec` and `ConnectorJobInput` rather than listing it, so a sixth lands in the check
    # the day it is declared.
    awaits_answer: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


@contextmanager
def _acting_as(identity: StepIdentity) -> Iterator[None]:
    """Run a step as its requester, in their conversation, under their correlation id.

    One bracket for all three ambients because they are one fact — a step acts for a person, in a
    chat, within one request — and because splitting them is how they drifted, twice, in the same
    way. The actor was stamped by all three step activities from the day they were written; the
    session by none of them, so every audit row a template produced named the user and booked
    `session_id=""`; and the correlation id by none of them either, while `StepIdentity` carried it
    as a `min_length=1` field whose comment says it ties the run's audit events together.

    **What the third one actually cost, since the audit trail turned out not to be the victim:**
    `agent/audit.py` falls back to the correlation id each step activity passes it explicitly, so
    its rows were right. Everything that reads the ambient instead was not — `core/logging.py`'s
    `ContextFilter` wrote `correlation_id="-"` on every line a durable step logged,
    `kg/proposal.py::ambient_provenance` recorded an empty id on every note a template proposed, and
    `connectors/jobs.py` handed `ConnectorJobInput.correlation_id=""` to every job a template
    launched — so a paged engineer had no grep path from a running job back to the turn behind it.

    A context manager rather than a decorator: two callers want the stamp around only part of their
    body (the resolution, not the `ResolvedJob` built from it), and `run_agent_step` reads the
    identity again inside it. Three `set`/`reset` pairs written out at three call sites would also
    be three chances to forget a reset, which leaks one run's identity into whatever the worker
    picks up next.

    **This is no longer the only thing that binds them, and saying so is the point.**
    `durable/interceptor.py` wraps *every* activity on every worker and reads the same three ids
    off the same `identity` field — measured against the real `ToolStepInput`, `AgentStepInput` and
    `JobStepInput`, it binds exactly what this bracket binds, over a scope that strictly contains
    it. So on a worker this is redundant, and it cannot drift, because both read
    `StepIdentity`'s own fields rather than restating them. What it still covers is an activity
    invoked *directly* — which is how the two authorization tests over `authorize_job_step` prove
    that a step cannot run a tool its requester could not run. Collapsing the two into one producer
    means moving those tests onto a worker harness, and that is a decision with a security control
    in its blast radius rather than a tidy-up (`docs/planning/BACKLOG.md`).
    """
    # The template path DOES bind `identity.roles`, unlike the interceptor and the report retriever
    # which bind empty (security review: roles do not cross the durable boundary from an unsigned
    # payload). The difference is deliberate and its residual is stated: `authorize_job_step` is the
    # *first* authorization for a template step — a step launched by another step has no front-door
    # pre-check to fall back on — so binding empty here would refuse every entitled template job
    # rather than fail closed on a forgery. Keeping the role bind preserves that shipped
    # capability; what it relies on is that only trusted code can enqueue a `TemplateWorkflow` —
    # i.e. broker write access is restricted (Temporal mTLS, enforced under entra_required).
    # Fully closing it without breaking the feature needs a signed payload (a Temporal codec); until
    # then this one path trusts `StepIdentity.roles` and the ADR records why.
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

    **The template's job step used to do none of this** (DARK-2, D-168). `ResolvedJob` carried the
    connector, workflow and queue and dropped `expensive` and `precondition` on the floor, and
    `TemplateWorkflow._run_job_step` started the child workflow with `resolve(step.arguments,
    scope)` exactly as written. So a template naming `sample_conformers` started expensive work for
    anyone entitled to run its `run_<name>` tool, a job's declared domain guard — the one
    `JobSpec.precondition` documents as having no other replay-safe home — never ran on this path,
    and the launch left no audit row. The module docstring above claimed the opposite.

    The pre-flight is `chemclaw.connectors.jobs.prepare_job_launch`, shared with the chat launcher
    rather than reimplemented, so the two cannot drift; the identity is stamped from the step first,
    so `authorize_trigger` decides against the person who asked rather than against nobody. A
    refusal raises `AuthorizationError` — a plain `Exception`, not a `ValueError`
    (`chemclaw.agent.authz` explains why it is deliberately kept out of the `ChemclawError`
    hierarchy). Across an activity boundary it arrives as an `ActivityError` whose
    `ApplicationError.type` is the exact string `"AuthorizationError"` — Temporal matches
    `non_retryable_error_types` by that name, not by `isinstance`, so the `ValueError` question does
    not decide this either way. What does is that `"AuthorizationError"` is itself listed by name
    in `BAD_DATA_RETRY` (`chemclaw.durable.publish`), so an unentitled step fails on its first
    attempt naming the reason instead of retrying an authorization decision that will never change.

    Everything below about resolving off the workflow thread is unchanged (REV-13), and it is why
    the authorization belongs here too: this is the last place before the child starts that can
    read config and import a bundle's precondition without making a replay depend on the disk.

    `TemplateWorkflow._run_job_step` used to call `chemclaw.connectors.registry.find_job` directly,
    inside
    `workflow.unsafe.imports_passed_through()`. Two things were wrong with that (REV-13), and they
    compound:

    **It read the filesystem from workflow code.** `find_job` walks `enabled()`, which reaches
    `discovered()` — directory scans and YAML parsing, `@cache`d per worker process but re-run on
    any process that has not done it. That makes the child-workflow start a function of the *disk
    the replaying worker happens to have* rather than of history. A worker that came up with a
    different bundle set resolves the same step differently, and Temporal refuses the resulting
    history mismatch. Resolving through a local activity records the answer once, exactly as
    `chemclaw.durable.orchestrator.resolve_fan_out_limit` does for the fan-out bound and for the
    same
    reason.

    **A bad job name hung the run instead of failing it.** `find_job` raises `ConnectorError`, a
    plain exception, not an SDK `FailureError`. Raised in workflow code, the Temporal SDK treats it
    as a possible bug and suspends the workflow in an internal task-failure retry loop that ignores
    the retry policy and never gives up (the same trap D-093 documents for fan-out children). A
    template naming a job that no enabled connector declares therefore produced a run that sat
    there forever rather than one that failed and said why. Across an activity boundary the same
    error arrives as an `ActivityError` whose `ApplicationError.type` is the exact string
    `"ConnectorError"` — Temporal matches `non_retryable_error_types` by that name, not by
    `isinstance`, so being a `ValueError` subclass is not what makes this non-retryable. What does
    is that `ConnectorError` is itself listed in `BAD_DATA_RETRY` (`chemclaw.durable.publish`), so
    it fails on the first attempt with the message naming the declared jobs.
    """
    connector, job = find_job(step.job)
    with _acting_as(step.identity):
        payload = await _audited(
            step.identity,
            job.name,
            step.arguments,
            lambda: prepare_job_launch(connector, job, step.arguments),
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


async def _audited(
    identity: StepIdentity,
    tool: str,
    arguments: dict[str, Any],
    action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run a job step's pre-flight through the governed chain, so the launch leaves an audit row.

    Through the chain over a real tool rather than by emitting an `AuditEvent` directly: there is
    exactly one place that decides what an audit record looks like, and a second emitter would
    drift from it the first time that shape changed. The tool is named for the job, so the row
    reads the same as the one a chat turn's launch of the same job writes — which is the point,
    since the whole finding was that these two paths were governed differently.

    The pre-flight is wrapped in a tool built on the spot rather than found on the surface, because
    what is being audited is not a tool the model can call: it is the resolution and validation a
    `job` step does before Temporal starts the workflow. Naming it after the job is what makes it
    legible in the trail.

    A refusal propagates after being recorded as an `error` outcome, exactly as a denied chat tool
    call is.
    """

    @tool_decorator(name_or_callable=tool, description=f"launch the {tool!r} job")
    async def _launch(**_kwargs: Any) -> dict[str, Any]:
        return action()

    payload: dict[str, Any] = await invoke_governed(
        _launch,
        arguments,
        correlation_id=identity.correlation_id,
        actor=identity.actor,
        profile=get_profile(None),
    )
    return payload


# **On the background queue, because until now it was on no queue at all.** `resolve_job_step` (as
# it then was) carried `@durable_activity` and these two did not, so no worker ever registered them
# — a template's `tool` and `agent` steps failed with "Activity function run_tool_step ... is not
# registered on this worker" the first time one ran against a real server, which is to say the
# shipped `hazard-briefing` template could not execute a single step. Found by running it live for
# D-168; it is the same class as the eight defects D-155 collected, where a feature is written,
# tested and served by nothing.
@durable_activity("background")
@activity.defn
async def run_tool_step(step: ToolStepInput) -> Any:
    """Call one tool as the run's actor, through the same audit + authz chain a chat turn uses.

    The tool is reached by assembling the surface and finding it by name, rather than by a second
    lookup path: "which tools exist" already has one answer (`_capability_tools` plus
    `connector_specs`), and a template resolving names differently from a conversation is exactly
    how the two drift.

    **No agent is built.** It used to build a whole MAF `Agent` behind a `_NoChatClient` stand-in,
    purely to read its assembled tool list — a model-less step demanding an LLM credential's worth
    of construction. The two halves of the surface are ordinary functions; calling them is the
    whole assembly.
    """
    capability_tools, connector_specs = _agent_surface()
    with _acting_as(step.identity):
        async with AsyncExitStack() as stack:
            connector_tools, unreachable = await open_connector_specs(stack, connector_specs())
            # Beating while the tool runs, because a `tool` step is the one place a template does
            # genuinely opaque work — an MCP call into a real calculation, with no unit boundary to
            # report progress at, which is exactly the case `durable/heartbeat.beating` exists for.
            # Without it `start_to_close_timeout` was the only liveness signal: a worker killed
            # mid-call was indistinguishable from one still calculating, so the run waited out the
            # whole per-step budget before retrying an attempt that had died in its first second.
            return await beating(
                _invoke([*capability_tools(), *connector_tools], step, unreachable),
                f"template tool step {step.tool}",
                settings.template_step_heartbeat_timeout_seconds,
            )


async def _invoke(tools: list[Any], step: ToolStepInput, unreachable: list[str]) -> Any:
    """Find `step.tool` on the assembled surface and call it, or raise naming what exists.

    `unreachable` is carried in only to make the failure legible (REV-6). A connector that did not
    come up contributes no functions, so its tools are simply *absent* from `available` — and the
    error then blamed the template for naming a tool that the template names correctly. On a
    retried activity that reads as a broken template rather than a broken host, which sends the
    operator to the wrong file.
    """
    # One list, one loop, and that is the fix D-168 argued for made structural. The two halves used
    # to be searched separately and called differently — the connector half through
    # `connector.call_tool`, which reaches the connector directly and skipped both the audit trail
    # and `enforce_tool_authz`. The consequence was not theoretical: both tool steps of the shipped
    # `hazard-briefing` template left no audit row, and a template naming a role-gated tool ran
    # it for anyone who could run the template. A connector tool is an ordinary LangChain tool, so
    # there is no longer a second shape to tempt a second path.
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

    LangChain composes that chain inside `create_agent`'s tool node, which a template does not go
    through — so calling the tool directly would run it *ungoverned*. `invoke_governed` composes
    the identical list, from the identical builder, which is what keeps the template path and the
    chat path from drifting: not that both apply "the middlewares", but that there is one list and
    both fold it.

    **This used to be two of the six, hand-nested**: audit around authorization, with the dry-run
    guard, the repeat guard, the plan gate and the failure announcer all absent. That was not a
    stated attenuation, it was the reachable subset of a chain the framework owned.

    Two workarounds went with the framework. `skip_parsing=True` existed because MAF's `invoke`
    re-wrapped every result in `list[Content]`, which Temporal's data converter refuses outright
    ("Unable to serialize unknown type: agent_framework._types.Content") — so a `tool` step could
    never return at all, which is why no template with one had ever completed. And `_serializable`
    unwrapped that envelope. What survives is `_mcp_text`, for the case that was never a framework
    artifact: an MCP tool answers as content blocks on the wire whatever calls it.
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

    **A tool result was reaching the resolver as a string, and that is what made half the shipped
    templates dead on their second step.** `_mcp_text` joins content blocks into text, so
    `${steps.forms.result.smiles}` asked for a field of a `str` and raised `UnresolvedReference` —
    after the launch, inside the workflow, with CI green. The templates that field-walk a **`job`**
    step never hit it, because a `ConnectorJobResult` is a real model; these were the first to
    field-walk a **tool** result.

    Hoisting a container field on the tool's return model was necessary and not sufficient: it
    fixes the *indexing* limit `templates/resolve.py` has, and cannot fix a value that is not a
    model by the time the resolver sees it. The structure was on the wire the whole time —
    `langchain_mcp_adapters` builds every tool with `response_format="content_and_artifact"` and
    puts the server's `structuredContent` in the artifact — and `ainvoke(args)` simply discards it.

    Read defensively rather than by type: the artifact is upstream's `MCPToolArtifact` TypedDict,
    an in-process tool has no artifact at all, and neither shape is promised to us.
    """
    artifact = getattr(message, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        return structured if isinstance(structured, dict) else None
    return None


def _mcp_text(result: Any) -> Any:
    """Flatten an MCP tool's content blocks into the text a step's result should carry.

    Not a framework artifact: an MCP tool answers as blocks on the wire however it is called, and
    those blocks are not a type Temporal's data converter knows. Text parts are joined, because
    that is what the answer *is*; a result with no text parts falls back to `str()` so a step never
    fails on the shape of a value it managed to produce.

    **Matched on being a list of blocks, not on having a `.type` attribute.** Duck-typing here is
    wrong in a way that is easy to miss: `find_notes` returns `list[NoteRef]`, and a `NoteRef` *has*
    a `type` field (the note's kind). A `hasattr` test therefore matched it, found no `.text`, and
    flattened a structured result into a Python repr — silently, for every template step naming
    such a tool.
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

    **This is the whole difference between a ledger and a ledger of the tidy runs.** The spend used
    to be summed off `result["messages"]` once `ainvoke` returned, which is a line that only runs
    when the turn returns — so a step that raised booked an all-zero row, and a row that exists
    saying zero is worse than no row: it asserts the step cost nothing. Measured on this activity
    against a scripted model reporting 120 tokens per call: a provider error after two paid calls
    booked `(0, 0)` and moved `chemclaw_tokens_total` by 0.0, and a runaway that made **52** paid
    model calls (6,240 tokens) before the recursion ceiling stopped it booked the same. The runaway
    is the case the metering was added for.

    A callback rather than a `try`/`except` around the sum, because the messages of an *abandoned*
    turn are not reachable at all — `GraphRecursionError` and a provider exception both propagate
    out of `ainvoke` with no result to read. The only place the numbers exist is the moment each
    call returns them, which is what `on_llm_end` is.

    `llm_result_usage` is the same reader the chat path's off-stream meter uses
    (`agent/turn_usage.py`), which is itself `graph_usage_tokens` over the callback's payload — so
    no two paths can disagree about what a cached token costs, and a generation carrying no usage
    meters 0 rather than failing a step.

    **One `on_llm_end` per model call, whether the provider streamed or not**, which is why this
    cannot double-count: LangChain aggregates a stream's chunks and fires this hook once with the
    summed message. It is also the only accumulation path left — the post-hoc loop is deleted, not
    kept beside it.
    """

    def __init__(self) -> None:
        self.usage = TurnUsage()

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Add what one finished model call reported to this step's running total.

        Args:
            response: The call's result. `generations` is a list per prompt, each a list of
                candidates; both are walked because the shape is upstream's, and a chat call's
                single generation is the degenerate case of it rather than a different thing.
            kwargs: `run_id`, `parent_run_id` and the rest of the callback contract, unused here —
                a step's spend is one number, not a per-call breakdown.
        """
        self.usage.add(llm_result_usage(response))


@durable_activity("background")
@activity.defn
async def run_agent_step(step: AgentStepInput) -> str:
    """Run one agent turn as the run's actor and return its answer text.

    The step that keeps a template agentic: the sequence around it is fixed, the reasoning inside it
    is not. `profile` narrows which agent runs it, so a summarizing step need not hold the
    durable-job launchers — attenuation applies here exactly as it does to a chat session.

    **Run without the harness, whatever the deployment's default is** (D-168). Two reasons, and the
    first was found by running the shipped template live: the harness middleware refuses a
    session-less `agent.run` ("ToolApprovalMiddleware requires an AgentSession" — the same wall
    D-152 hit for the CLI), so under `harness_enabled=true`, which is what the Helm chart sets, an
    `agent` step could not run at all. The second is why the fix is *disable* rather than
    *invent a session*: the harness adds a todo list, a plan/execute mode and an autonomous
    completion loop, and a template exists precisely to fix the sequence instead. Running a
    planning loop inside one step of a fixed procedure would give the step back the discretion the
    template was written to remove — and, with the plan gate now enforced, would refuse every write
    inside it for want of a plan nobody can approve.

    **So the step is ungated and the step is read-only by default**, which is the same sentence from
    both ends. The plan gate exists to put a human between an autonomously-chosen write and its
    execution; a template already has that human — the author of a reviewed, git-committed file that
    nothing at run time can produce — so gating it again would only ask for an approval of a plan
    nobody wrote. What the gate *also* did was bound the blast radius of a model improvising inside
    the step, and that half is kept structurally: `step_profile` hands this turn a surface with
    every side-effecting tool the step did not declare removed from it, so the graph is built
    without them.

    **The profile is resolved once and threaded through both calls.** It used to be resolved twice —
    the raw name to `connector_specs` and a modified copy to the builder — which is exactly the
    shape in which a narrowing silently covers half a surface: `compute_xtb_energy` is a *connector
    endpoint* tool, so narrowing only the builder's copy would leave it (and every other connector
    write) bound to the graph while the in-process half looked correctly closed.

    **This is a model turn, so it is metered like one** — and it was not, which made a template a
    hole in every number the deployment has about what it spends. Measured on this activity against
    a scripted model reporting 120 tokens per call: `chemclaw_tokens_total` read 0.0 before and
    0.0 after, and the audit row booked `session_id=""`. The chat path
    (`api/runner.run_turn`) stamps the session, watches the turn's repeated calls, sums the usage,
    books a `turn_costs` row and publishes five counters; this path did none of it, so a chemist
    who wanted work unmetered only had to ask for it through a template. What that costs is not
    just a dashboard: `turn_costs` is the per-actor attribution ledger, and a spend it never sees is
    a spend nobody can bill or find.

    **What is *not* wired here is enforcement, and that is a property of the process rather than an
    omission.** `api/budget.py` is explicitly in-process and best-effort — LRU maps living in the
    front door's memory, read by the admission check before the *next* turn on that connection. A
    Temporal worker is a different process with no access to them, and booking a template's spend
    into a worker-local copy would produce a second, invisible ledger that refuses nothing. So this
    meters (the counters, and the durable row that outlives the process) and does not pretend to
    cap. A run-level cap on template spend needs a durable counter, which is a decision, not a call.

    **The repeat guard is watched here too**, because it is per-turn ambient state that the middle-
    ware reads and its caller owns the lifetime of (`agent/repeat_guard.py`). Without
    `begin_call_watch` the guard is inert — its contextvar is `None`, so the counter it increments
    is discarded — and a step's model could ask one tool the identical question indefinitely, which
    is precisely the shape the guard was measured against (`find_past_jobs` ×8 in one turn).
    """
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    _capability_tools, connector_specs = _agent_surface()
    profile = step_profile(step.profile, step.write_tools)
    started = time.perf_counter()
    meter = _StepMeter()
    answered = False
    # How this step ended, in the vocabulary `turn_costs.outcome` is read in. `empty_answer` is
    # the floor for the same reason it is in `api/runner._settle_outcome`: a step that ran to its
    # own end and produced nothing is the silent death, and every other ending overwrites this
    # before the `finally` books it.
    outcome = "empty_answer"
    calls_token = begin_call_watch()
    # Started for the same reason as the call watch above it: a step runs a real model turn, so the
    # context policy's per-turn state has to exist here too or compaction reports one standing
    # reduction once per model call and the step's cost row cannot say the policy fired
    # (`agent/context_budget.py`).
    context_token = begin_context_watch()
    with _acting_as(step.identity):
        try:
            async with AsyncExitStack() as stack:
                connectors, _unreachable = await open_connector_specs(
                    stack, connector_specs(profile)
                )
                # Compiled here, with this step's connectors, for the reason
                # `build_langgraph_agent` gives: a graph binds its tools at construction and a
                # connector session belongs to exactly one caller. A step is that caller.
                #
                # No checkpointer. A template step is one bounded turn with no conversation before
                # or after it — Temporal is what makes the *run* durable, and giving the step its
                # own checkpointed thread would be a second durability mechanism inside the first
                # (D-002).
                graph = build_langgraph_agent(
                    profile=profile,
                    actor=step.identity.actor,
                    correlation_id=step.identity.correlation_id,
                    connectors=connectors,
                )
                # No thread — a template step is one bounded turn — but the step ceiling still
                # applies, and it is the only thing that applies: this path runs with the harness
                # off, and `_harness_middleware` attaches `enforce_loop_cap` only with the harness,
                # so nothing here stops the loop gracefully. `turn_config()` sets
                # `agent_recursion_limit` unconditionally (verified — the config carries no
                # thread-dependent branch), which is what keeps a looping step from inheriting
                # `create_agent`'s baked 9999. The cap is deliberately *not* attached instead: it
                # comes bundled with `TodoListMiddleware`, and a todo list is the discretion this
                # step exists without. What the ceiling cannot do is let the partial answer out, so
                # a step that reaches it raises with no result to read — which is exactly why the
                # meter is a callback on `turn_config()` rather than a sum over the returned
                # messages, and is what makes that runaway visible in `chemclaw_tokens_total`
                # rather than free and silent. Measured: 52 paid model calls, 6,240 tokens, booked.
                result = await beating(
                    graph.ainvoke(turn_input(step.prompt), {**turn_config(), "callbacks": [meter]}),
                    f"template agent step {step.step_id or step.profile or 'agent'}",
                    settings.template_step_heartbeat_timeout_seconds,
                )
                answer = answer_text(result)
                # **An empty answer is not an answer, in both fields at once.** The chat path
                # settles the same case the same way — `_empty_answer_event` returns *before*
                # `answered = True`, so the silent turn books `completed=False` — and a step that
                # returned nothing hands the next step of the template nothing.
                answered = bool(answer)
                outcome = "answered" if answered else "empty_answer"
                return answer
        except asyncio.CancelledError:
            # A Temporal activity cancellation — the workflow was cancelled, the worker is
            # draining, or an activity timeout fired. The chat path calls this ending `abandoned`
            # and tells a *wall-clock* kill apart from it by the caller's own deadline; there is
            # no such deadline here (Temporal owns the clock and the cancellation carries no
            # reason), so `timed_out` is deliberately not produced by this writer rather than
            # guessed at.
            outcome = "abandoned"
            raise
        except Exception:
            # Everything else, the step ceiling included: `agent_recursion_limit` surfaces as a
            # raise with no partial answer to read, so it is `errored` and not `loop_capped` —
            # `loop_capped` names a turn that was stopped gracefully *and still answered*, which
            # this path cannot be.
            outcome = "errored"
            raise
        finally:
            # Booked on every path, including a failure, a runaway and a cancelled attempt: a step
            # that broke after three model calls still spent them, and a ledger that kept only the
            # tidy runs would be wrong in the direction that hides a runaway. Same stance as
            # `api/runner.run_turn`'s `finally`. What makes that true here is `_StepMeter`, not this
            # line — a sum taken after `ainvoke` returned never ran on the paths where it mattered.
            #
            # The residual limit, stated because it is small rather than absent: the meter books a
            # call when the call *ends*, so a turn cancelled or failing **mid-call** does not book
            # that one in-flight call — the provider reported no usage for it, and there is nothing
            # to read. Every call that completed is booked. So the ledger can under-report by at
            # most one call, never by a whole turn.
            end_call_watch(calls_token)
            # Booked *before* the context watch is torn down, because the row reads it. The call
            # watch above has no such reader, which is why the two ends are not adjacent.
            _book_step_spend(step, meter.usage, time.perf_counter() - started, answered, outcome)
            end_context_watch(context_token)


def _book_step_spend(
    step: AgentStepInput, usage: TurnUsage, duration_seconds: float, answered: bool, outcome: str
) -> None:
    """Publish one agent step's spend: the five counters, and the durable per-turn cost row.

    The same two instruments the chat path publishes, with the same labels, because they answer two
    different questions and neither substitutes for the other: the counters are the fleet-wide rate
    (labelled `profile`, low cardinality by construction) and `turn_costs` is the per-actor
    attribution ledger, which needs an unbounded key and quarters of history — `agent/turn_cost.py`
    records why one instrument cannot be both. Each counter is guarded on a non-zero value so a
    provider that reports none of a dimension leaves its series untouched rather than publishing a
    fabricated zero, which is the rule `core/metrics.py` states for gauges.

    **`outcome` is written, and leaving it defaulted was a defect.** This is the *second* writer
    of `turn_costs`, and it used to pass `completed=answered` and nothing else — so every row a
    template agent step has ever written carries `outcome='unknown'`, which is the column default
    meaning "written before the column existed". Two populations in one value: an outcome query
    cannot tell a 2026-08 backfill row from a step booked today, and the index on that column
    indexes a value that means two things. The vocabulary is
    `api/runner._OUTCOMES`, and it is spelled out here rather than imported because `durable` may
    not import `api` (`tests/test_layering.py`); what this writer can produce is four of the six —
    `answered`, `empty_answer`, `errored`, `abandoned` — and `run_agent_step` says at each raise
    site why the other two are not among them.

    **The cost row's key is the run's correlation id plus the step id.** `turn_costs` upserts on
    `correlation_id` (`agent/turn_cost_store.py`) — deliberately, so a retried write replaces rather
    than doubles — and every step of a template run shares the run's id, which is what ties them
    together in the audit trail. Booking each step under the bare run id would therefore have made
    a five-`agent`-step template report the *last* step's spend as the whole run's. The prefix keeps
    the join to `audit_events` a prefix match rather than an equality, which is the smaller loss.

    Args:
        step: The step whose turn just ended — its profile labels the spend, its identity bills it.
        usage: What that turn's model calls reported, already summed.
        duration_seconds: Wall clock for the step, for the ledger's duration column.
        answered: Whether the step produced its answer. Recorded, not filtered — see `TurnCost`.
        outcome: How the step ended, in `turn_costs.outcome`'s vocabulary — see below.
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

    **One profile, used for both halves of the surface.** The caller builds its connectors from
    this object *and* builds the graph from it, because the surface has two halves and a narrowing
    that reaches one of them is not a narrowing. It was resolved twice before this, and the
    connector half got the un-narrowed name.

    Two overrides, each answering a different question:

    - `harness_enabled=False` — the reason `run_agent_step` gives: the harness's todo list, plan
      mode and completion loop are the discretion a template exists to remove, and its approval
      middleware refuses a session-less run outright.
    - `tool_names = advertised − (side-effecting − declared)` — the step's read-only default. The
      profile's own `tool_names` dial is the seam this uses rather than a new one, because it is the
      documented attenuation point and it already spans *both* halves (`_capability_tools` narrows
      the in-process tools, `connector_specs` narrows each bundle's allow-list) and re-narrows the
      skills backend along with them. `advertised_tool_names` is the starting set rather than the
      whole registry so a step that names a profile keeps that profile's own narrowing: this can
      only subtract from what the profile already offered.

    The classification is `chemclaw.agent.authz.side_effecting_tools()`, shared with the dry-run
    guard and the plan gate rather than restated. A second list of "which tools write" is the second
    source of truth this tree forbids, and it would be wrong in the same direction each time: a
    connector's own manifest is the only thing that knows `compute_xtb_energy` spends real compute
    while `resolve_compound` is a lookup, and core cannot tell them apart from the name.

    `declared` is intersected in rather than added: a step cannot name a tool its profile never
    advertised and gain it, which keeps this attenuation-only in the sense `agent/profiles.py`
    means it. A name that resolves to nothing is a `make template-validate` failure, not a silent
    widening here.

    Args:
        profile: The profile the step named, or `None` for the default agent.
        write_tools: The side-effecting tools this step declared it may reach.

    Returns:
        An `AgentProfile` copy, narrowed. Never the registered object — profiles are shared,
        process-lived and frozen.
    """
    # Lazily, both of them: `chemclaw.agent.chemclaw_agent` reaches the template registry, which
    # reaches the workflow, which reaches this module — the cycle `_agent_surface` defers for the
    # same reason. `side_effecting_tools` reaches the same two registries.
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
