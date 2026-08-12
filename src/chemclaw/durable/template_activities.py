"""Executing one template step — the I/O half, and why these are activities not workflow code.

A `tool` step calls a tool and an `agent` step runs a model turn: both are non-deterministic
network work, so neither can live in the workflow. `chemclaw.durable.template_job` sequences; this
works.

The part worth reading carefully is the identity restoration. A workflow has no request context, so
the turn's actor and roles travel in the activity's input and are stamped ambient here *before* the
tool runs — which is what makes the audit trail name the real user and, more importantly, makes
`enforce_tool_authz` decide against that user rather than against nobody. A template must not become
a way to run a tool the requester could not run directly, and this is where that is enforced.
"""

import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.tools import tool as tool_decorator
from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from chemclaw.agent.profiles import get_profile
from chemclaw.agent.state import turn_input
from chemclaw.agent.tool_invocation import invoke_governed
from chemclaw.connectors.jobs import prepare_job_launch
from chemclaw.connectors.queues import bundle_queue
from chemclaw.connectors.registry import find_job, open_connector_specs
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
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


class ToolStepInput(BaseModel):
    """One resolved `tool` step: which tool, with which already-substituted arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    identity: StepIdentity


class AgentStepInput(BaseModel):
    """One resolved `agent` step: the rendered prompt and the profile to run it under."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    profile: str | None = None
    identity: StepIdentity


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
    payload: dict[str, Any] = Field(default_factory=dict)


@durable_activity("background")
@activity.defn
async def authorize_job_step(step: JobStepInput) -> ResolvedJob:
    """Resolve, validate and authorize one `job` step as its requester — outside the workflow.

    **The template's job step used to do none of this** (DARK-2, D-168). `ResolvedJob` carried the
    connector, workflow and queue and dropped `expensive` and `precondition` on the floor, and
    `TemplateWorkflow._run_job_step` started the child workflow with `resolve(step.arguments,
    scope)` exactly as written. So a template naming `compute_dft_energy` started HPC work for
    anyone entitled to run its `run_<name>` tool, a job's declared domain guard — the one
    `JobSpec.precondition` documents as having no other replay-safe home — never ran on this path,
    and the launch left no GxP audit row. The module docstring above claimed the opposite.

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
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        payload = await _audited(
            step.identity,
            job.name,
            step.arguments,
            lambda: prepare_job_launch(connector, job, step.arguments),
        )
    finally:
        reset_current_identity(tokens)
    return ResolvedJob(
        connector=connector,
        job=job.name,
        workflow=job.workflow,
        task_queue=bundle_queue(connector),
        publish_to_graph=job.publish_to_graph,
        payload=payload,
    )


async def _audited(
    identity: StepIdentity,
    tool: str,
    arguments: dict[str, Any],
    action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run a job step's pre-flight through the governed chain, so the launch leaves a GxP row.

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
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        async with AsyncExitStack() as stack:
            connector_tools, unreachable = await open_connector_specs(stack, connector_specs())
            return await _invoke([*capability_tools(), *connector_tools], step, unreachable)
    finally:
        reset_current_identity(tokens)


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
    # `hazard-briefing` template left no GxP audit row, and a template naming a role-gated tool ran
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
    result = await invoke_governed(
        tool,
        step.arguments,
        correlation_id=step.identity.correlation_id,
        actor=step.identity.actor,
        profile=get_profile(None),
    )
    return _mcp_text(result)


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
    """
    from chemclaw.agent.langgraph_agent import build_langgraph_agent

    _capability_tools, connector_specs = _agent_surface()
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        async with AsyncExitStack() as stack:
            connectors, _unreachable = await open_connector_specs(
                stack, connector_specs(step.profile)
            )
            # Compiled here, with this step's connectors, for the reason `build_langgraph_agent`
            # gives: a graph binds its tools at construction and a connector session belongs to
            # exactly one caller. A step is that caller.
            #
            # No checkpointer. A template step is one bounded turn with no conversation before or
            # after it — Temporal is what makes the *run* durable, and giving the step its own
            # checkpointed thread would be a second durability mechanism inside the first (D-002).
            graph = build_langgraph_agent(
                profile=_classic(step.profile),
                actor=step.identity.actor,
                correlation_id=step.identity.correlation_id,
                connectors=connectors,
            )
            result = await graph.ainvoke(turn_input(step.prompt))
            return _answer_text(result)
    finally:
        reset_current_identity(tokens)


def _answer_text(result: Any) -> str:
    """The final assistant text out of a completed graph turn.

    The graph returns its whole message list rather than MAF's single `response.text`, so the
    answer is the last message's content. Joined across content blocks because a model may answer
    in parts, and coerced with `str` so a step never fails on a shape it managed to produce.
    """
    messages = result.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def _classic(profile: str | None) -> Any:
    """The step's profile with the harness switched off — see `run_agent_step`.

    Resolved through the profile registry rather than by passing a bare flag, so a step that names
    a profile keeps every other narrowing that profile applies (its instructions, its tool subset,
    its connectors) and loses only the autonomy loop.
    """
    from chemclaw.agent.profiles import get_profile

    return get_profile(profile).model_copy(update={"harness_enabled": False})
