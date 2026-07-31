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

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from chemclaw.agent.audit import make_audit_middleware
from chemclaw.agent.identity_context import reset_current_identity, set_current_identity
from chemclaw.agent.tool_authz import enforce_tool_authz
from chemclaw.connectors.jobs import prepare_job_launch
from chemclaw.connectors.queues import bundle_queue
from chemclaw.connectors.registry import find_job, open_reachable
from chemclaw.durable.registry import durable_activity

logger = logging.getLogger(__name__)


def _agent_surface() -> Any:
    """Import the agent builder lazily, at call time rather than at module import.

    `chemclaw.agent.chemclaw_agent` reaches the template registry (a template becomes a tool like
    any
    other), which reaches the workflow, which reaches this module — a cycle at import time.
    Deferring it breaks the cycle and is what an activity should do regardless: it runs on a
    worker, long after import, and pulling the whole agent stack into every module that merely
    *mentions* an activity is how a worker's start-up cost quietly triples.
    """
    from chemclaw.agent.chemclaw_agent import build_agent, connector_tools

    return build_agent, connector_tools


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
    refusal raises `AuthorizationError` — a `ValueError`, which `BAD_DATA_RETRY` lists
    non-retryable — so an unentitled step fails on its first attempt naming the reason instead of
    retrying an authorization decision that will never change.

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

    **A bad job name hung the run instead of failing it.** `find_job` raises `ConnectorError`, which
    is a `ValueError` — a plain exception, not an SDK `FailureError`. Raised in workflow code, the
    Temporal SDK treats it as a possible bug and suspends the workflow in an internal task-failure
    retry loop that ignores the retry policy and never gives up (the same trap D-093 documents for
    fan-out children). A template naming a job that no enabled connector declares therefore produced
    a run that sat there forever rather than one that failed and said why. Across an activity
    boundary the same error arrives as an `ActivityError`, and `BAD_DATA_RETRY` lists `ValueError`
    non-retryable, so it fails on the first attempt with the message naming the declared jobs.
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
    """Run a job step's pre-flight inside the audit middleware, so the launch leaves a GxP row.

    Through `make_audit_middleware` over a real `FunctionTool` rather than by emitting an
    `AuditEvent` directly: there is exactly one place that decides what an audit record looks like,
    and a second emitter would drift from it the first time that shape changed. The tool is named
    for the job, so the row reads the same as the one a chat turn's launch of the same job writes —
    which is the point, since the whole finding was that these two paths were governed differently.

    A refusal propagates after being recorded as an `error` outcome, exactly as a denied chat tool
    call is.
    """
    from agent_framework import FunctionInvocationContext, FunctionTool

    async def _run() -> dict[str, Any]:
        return action()

    context = FunctionInvocationContext(
        function=FunctionTool(func=_run, name=tool, description=f"launch the {tool!r} job"),
        arguments=arguments,
    )
    audit = make_audit_middleware(correlation_id=identity.correlation_id, actor=identity.actor)

    async def _invoke() -> None:
        context.result = await _run()

    await audit(context, _invoke)
    payload: dict[str, Any] = context.result
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

    The tool is reached by building the agent's surface and finding it by name, rather than by a
    second lookup path: "which tools exist" already has one answer (`build_agent` plus
    `connector_tools`), and a template resolving names differently from a conversation is exactly
    how the two drift.
    """
    build_agent, connector_tools = _agent_surface()
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        async with AsyncExitStack() as stack:
            connectors = connector_tools()
            unreachable = await open_reachable(stack, connectors)
            agent = build_agent(
                chat_client=_NoChatClient(), correlation_id=step.identity.correlation_id
            )
            return await _invoke(agent, connectors, step, unreachable)
    finally:
        reset_current_identity(tokens)


class _NoChatClient:
    """A stand-in chat client for the tool path, which never talks to a model.

    `build_agent` is used here only to *assemble the tool surface* — the same assembly a chat turn
    gets, so the two cannot drift — and building it would otherwise demand a live LLM credential for
    a step that makes no model call at all.
    """


async def _invoke(
    agent: Any, connectors: list[Any], step: ToolStepInput, unreachable: list[str]
) -> Any:
    """Find `step.tool` on the assembled surface and call it, or raise naming what exists.

    `unreachable` is carried in only to make the failure legible (REV-6). A connector that did not
    come up contributes no functions, so its tools are simply *absent* from `available` — and the
    error then blamed the template for naming a tool that the template names correctly. On a
    retried activity that reads as a broken template rather than a broken host, which sends the
    operator to the wrong file.
    """
    for tool in agent.default_options["tools"]:
        if getattr(tool, "name", None) == step.tool:
            return await _call_function_tool(tool, step)
    for connector in connectors:
        for function in connector.functions:
            if function.name == step.tool:
                # Through the *same* governed path as the in-process branch above (D-168). This
                # used to be `connector.call_tool(...)`, which reaches the connector directly and
                # therefore skipped both `enforce_tool_authz` and the audit middleware — while the
                # branch three lines up hand-applied both, and this module's own docstring said
                # applying them was the point. The consequence was not theoretical: both tool steps
                # of the shipped `hazard-briefing` template left no GxP audit row, and a template
                # naming a role-gated tool ran it for anyone who could run the template.
                #
                # MAF's MCP tools are ordinary `FunctionTool`s, so nothing about the call shape has
                # to change to route them through the middleware — only the decision to do it.
                return await _call_function_tool(function, step)
    available = sorted(
        [t.name for t in agent.default_options["tools"]]
        + [f.name for c in connectors for f in c.functions]
    )
    degraded = f" ({len(unreachable)} unreachable: {', '.join(unreachable)})" if unreachable else ""
    raise ValueError(
        f"template step names unknown tool {step.tool!r}{degraded}; available: {available}"
    )


async def _call_function_tool(tool: Any, step: ToolStepInput) -> Any:
    """Invoke one tool with the audit + authz middleware a chat turn would apply.

    MAF applies the agent's middleware inside its own tool-calling loop, which a template does not
    go through — so calling `tool.invoke(...)` directly would run the tool *ungoverned*. Applying
    the same two middlewares by hand here is what keeps the template path identical to the chat
    path in the way that matters: the call is audited, and an unauthorized one is refused.

    Used for both halves of the surface since D-168. The connector half used to call
    `connector.call_tool` and reach the connector directly, skipping both middlewares; MAF's MCP
    tools are ordinary `FunctionTool`s, so nothing about the call had to change except the decision
    to govern it.

    **`skip_parsing=True`, and it is not a preference.** `invoke` otherwise wraps the result in
    `list[Content]`, and a step's result crosses an activity boundary into Temporal history — where
    the data converter refuses it outright: *"Unable to serialize unknown type:
    agent_framework._types.Content"*. So a `tool` step could never return at all, on either branch,
    which is why no template with one has ever completed. The raw value is what
    `${steps.<id>.result}` should carry anyway: a chemist reading a run's trace wants the tool's
    answer, not the framework's envelope around it.
    """
    from agent_framework import FunctionInvocationContext

    context = FunctionInvocationContext(function=tool, arguments=step.arguments)
    audit = make_audit_middleware(
        correlation_id=step.identity.correlation_id, actor=step.identity.actor
    )

    async def _run_tool() -> None:
        context.result = await tool.invoke(arguments=context.arguments, skip_parsing=True)

    async def _gated() -> None:
        await enforce_tool_authz(context, _run_tool)

    await audit(context, _gated)
    return _serializable(context.result)


def _serializable(result: Any) -> Any:
    """Render a tool result into something Temporal's converter can carry.

    An MCP tool answers as `list[Content]` however it is invoked — `skip_parsing` skips MAF's
    *re-wrapping*, not the MCP client's own parse — and `Content` is not a type the data converter
    knows. Text parts are joined, because that is what an MCP tool's answer *is* on the wire; a
    result with no text parts falls back to `str()` so a step never fails on the shape of a value
    it managed to produce.
    """
    if isinstance(result, list) and result and all(hasattr(item, "type") for item in result):
        texts = [str(getattr(item, "text", "")) for item in result if getattr(item, "text", None)]
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
    build_agent, connector_tools = _agent_surface()
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        async with AsyncExitStack() as stack:
            connectors = connector_tools(step.profile)
            await open_reachable(stack, connectors)
            agent = build_agent(
                profile=_classic(step.profile), correlation_id=step.identity.correlation_id
            )
            response = await agent.run(step.prompt, tools=connectors or None)
            return str(response.text)
    finally:
        reset_current_identity(tokens)


def _classic(profile: str | None) -> Any:
    """The step's profile with the harness switched off — see `run_agent_step`.

    Resolved through the profile registry rather than by passing a bare flag, so a step that names
    a profile keeps every other narrowing that profile applies (its instructions, its tool subset,
    its connectors) and loses only the autonomy loop.
    """
    from chemclaw.agent.profiles import get_profile

    return get_profile(profile).model_copy(update={"harness_enabled": False})
