"""Executing one template step — the I/O half, and why these are activities not workflow code.

A `tool` step calls a tool and an `agent` step runs a model turn: both are non-deterministic
network work, so neither can live in the workflow. `workflows.template_job` sequences; this works.

The part worth reading carefully is the identity restoration. A workflow has no request context, so
the turn's actor and roles travel in the activity's input and are stamped ambient here *before* the
tool runs — which is what makes the audit trail name the real user and, more importantly, makes
`enforce_tool_authz` decide against that user rather than against nobody. A template must not become
a way to run a tool the requester could not run directly, and this is where that is enforced.
"""

import logging
from contextlib import AsyncExitStack
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from agents.audit import make_audit_middleware
from agents.identity_context import reset_current_identity, set_current_identity
from agents.tool_authz import enforce_tool_authz
from connectors.registry import open_reachable

logger = logging.getLogger(__name__)


def _agent_surface() -> Any:
    """Import the agent builder lazily, at call time rather than at module import.

    `agents.chemclaw_agent` reaches the template registry (a template becomes a tool like any
    other), which reaches the workflow, which reaches this module — a cycle at import time.
    Deferring it breaks the cycle and is what an activity should do regardless: it runs on a
    worker, long after import, and pulling the whole agent stack into every module that merely
    *mentions* an activity is how a worker's start-up cost quietly triples.
    """
    from agents.chemclaw_agent import build_agent, connector_tools

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
                return await connector.call_tool(step.tool, **step.arguments)
    available = sorted(
        [t.name for t in agent.default_options["tools"]]
        + [f.name for c in connectors for f in c.functions]
    )
    degraded = f" ({len(unreachable)} unreachable: {', '.join(unreachable)})" if unreachable else ""
    raise ValueError(
        f"template step names unknown tool {step.tool!r}{degraded}; available: {available}"
    )


async def _call_function_tool(tool: Any, step: ToolStepInput) -> Any:
    """Invoke an in-process tool with the audit + authz middleware a chat turn would apply.

    MAF applies the agent's middleware inside its own tool-calling loop, which a template does not
    go through — so calling `tool.invoke(...)` directly would run the tool *ungoverned*. Applying
    the same two middlewares by hand here is what keeps the template path identical to the chat
    path in the way that matters: the call is audited, and an unauthorized one is refused.
    """
    from agent_framework import FunctionInvocationContext

    context = FunctionInvocationContext(function=tool, arguments=step.arguments)
    audit = make_audit_middleware(
        correlation_id=step.identity.correlation_id, actor=step.identity.actor
    )

    async def _run_tool() -> None:
        context.result = await tool.invoke(arguments=context.arguments)

    async def _gated() -> None:
        await enforce_tool_authz(context, _run_tool)

    await audit(context, _gated)
    return context.result


@activity.defn
async def run_agent_step(step: AgentStepInput) -> str:
    """Run one agent turn as the run's actor and return its answer text.

    The step that keeps a template agentic: the sequence around it is fixed, the reasoning inside it
    is not. `profile` narrows which agent runs it, so a summarizing step need not hold the
    durable-job launchers — attenuation applies here exactly as it does to a chat session.
    """
    build_agent, connector_tools = _agent_surface()
    tokens = set_current_identity(step.identity.actor, frozenset(step.identity.roles))
    try:
        async with AsyncExitStack() as stack:
            connectors = connector_tools(step.profile)
            await open_reachable(stack, connectors)
            agent = build_agent(profile=step.profile, correlation_id=step.identity.correlation_id)
            response = await agent.run(step.prompt, tools=connectors or None)
            return str(response.text)
    finally:
        reset_current_identity(tokens)
