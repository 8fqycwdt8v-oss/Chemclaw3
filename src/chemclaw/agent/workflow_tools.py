"""Composing a reusable multi-step workflow, and running one.

A chemist asking the same three-step question every week costs three model calls and three
round-trips every week, and the model has to remember the order. A template makes it one call and
fixes the order — but a template is a git-committed file, so getting one takes a pull request.
These two tools are the run-time half: the agent writes the procedure down once, and afterwards it
is one durable run.

**What keeps that safe is `templates/composed.authored_problems`, not this module**, and the rule
is worth reading there: an agent-authored workflow may name no side-effecting tool, no durable job
and no `write_tools`. Read that file for why the plan gate's exemption survives this.

Two tools and not three. A listing of one owner's workflows is the third thing the model needs and
it rides on the refusal `run_composed_workflow` gives an unknown name, which names what does exist
— a tool schema is re-sent on every model call and a listing is needed on the turn a name is
already wrong.
"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.agent.authz import require_actor, side_effecting_tools
from chemclaw.core.tool_registry import tool
from chemclaw.templates.composed import (
    MAX_PER_OWNER,
    ComposedWorkflow,
    ComposedWorkflowError,
    authored_problems,
    default_composed_store,
    job_steps,
    unapproved_jobs,
)
from chemclaw.templates.manifest import Template

logger = logging.getLogger(__name__)


class WorkflowStep(BaseModel):
    """One step of a composed workflow."""

    model_config = ConfigDict(extra="forbid")

    # Short docstrings and `#` comments throughout: pydantic publishes a class docstring as the
    # JSON-schema `description` on every model call, so rationale lives here where it costs nothing
    # (`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`).
    id: str = Field(min_length=1, description="Unique within the workflow; later steps use it.")
    tool: str = Field(
        default="",
        description="A read-only tool to call. Leave empty for a reasoning step.",
    )
    job: str = Field(
        default="",
        description="A durable job to run and wait for. Needs the owner's approval before it runs.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for `tool`. Use ${inputs.x} and ${steps.id.result} to refer back.",
    )
    prompt: str = Field(
        default="",
        description="For a reasoning step: what to work out, referring to earlier steps.",
    )


class WorkflowInput(BaseModel):
    """One value the workflow is given when it runs."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(default="", description="What the caller should pass.")


def _document(
    name: str, summary: str, inputs: list[WorkflowInput], steps: list[WorkflowStep]
) -> Template:
    """The declared steps as a `Template`, so one shape is validated, stored and run.

    Deliberately the *same* model a `data/templates/*.yaml` file parses into rather than a parallel
    one: every validator a hand-written template gets — unique step ids, well-formed `${…}` spans,
    no forward references — applies here for free, and `TemplateWorkflow` runs the result without
    knowing where it came from. A second shape would be a second set of those rules to keep in
    step.
    """
    return Template.model_validate(
        {
            "name": name,
            "summary": summary or f"A workflow composed for {name}.",
            "inputs": [
                {"name": item.name, "type": "string", "description": item.description or item.name}
                for item in inputs
            ],
            "steps": [
                (
                    {"id": step.id, "kind": "job", "job": step.job, "arguments": step.arguments}
                    if step.job
                    else {"id": step.id, "kind": "agent", "prompt": step.prompt}
                    if not step.tool
                    else {
                        "id": step.id,
                        "kind": "tool",
                        "tool": step.tool,
                        "arguments": step.arguments,
                    }
                )
                for step in steps
            ],
        }
    )


@tool
async def compose_workflow(
    name: str,
    summary: str,
    inputs: list[WorkflowInput],
    steps: list[WorkflowStep],
) -> str:
    """Write down a repeatable multi-step procedure so it runs as one durable job from now on.

    Use this when the same sequence of reads keeps coming up and the order matters. Afterwards
    `run_composed_workflow` runs the whole thing in one call: the steps run without a model turn
    between them, independent steps run at the same time, and a run that fails resumes rather than
    starting over.

    **No step may call a tool that changes anything**, because the run has no conversation to
    approve a plan in — ask for the change in the conversation, where a person can see it.

    **A durable job step is allowed and does not run until a person approves this workflow.** So a
    procedure that ranks or searches conformers can be written down here; composing it is not the
    decision. Say so when you hand the name back: its owner approves it on the front door, and the
    approval covers exactly the steps as they stand, so composing it again needs approving again.

    Refer to values with `${inputs.<name>}` and to an earlier step with `${steps.<id>.result}` —
    a step that names no earlier step runs at the same time as its neighbours, so do not chain
    steps that do not actually need each other.

    Composing the same name again replaces it.

    Args:
        name: What to call it. Lowercase words joined by hyphens.
        summary: One line: when to use this and what it produces.
        inputs: The values a caller supplies, each named and described.
        steps: The procedure. Give `tool` and `arguments` for a lookup, or `prompt` for a step
            that reasons over what the earlier steps returned. End with a `prompt` step.

    Returns:
        A confirmation naming the workflow and how many steps it has.

    Raises:
        ChemclawError: When a step names a tool that does not exist, changes something, hands a
            model a write to spend, refers to a step that has not run yet, or when the procedure
            could not finish inside this deployment's run ceiling.
    """
    from chemclaw.agent.template_surface import TemplateSurface, run_ceiling_problems, step_problems

    owner = require_actor()
    try:
        document = _document(name, summary, inputs, steps)
    except ValueError as exc:
        raise ComposedWorkflowError(f"that workflow is not well-formed: {exc}") from exc

    problems = [
        # The same three checks a `data/templates/` file passes, plus the one that is only asked of
        # an agent-authored document. Resolved without signatures, as `unrunnable_reason` does it:
        # the argument half imports every bundle's server module and a tool call must not pay that.
        *step_problems(document, TemplateSurface.resolve(with_signatures=False)),
        *run_ceiling_problems(document),
        *authored_problems(document, side_effecting_tools()),
    ]
    if problems:
        raise ComposedWorkflowError(
            f"the {name!r} workflow was not saved:\n" + "\n".join(f"  - {p}" for p in problems)
        )

    store = default_composed_store()
    existing = await store.list_for(owner)
    if len(existing) >= MAX_PER_OWNER and not any(row.name == name for row in existing):
        raise ComposedWorkflowError(
            f"you already have {len(existing)} composed workflows, which is the limit. "
            "Re-compose one of them under its own name instead of adding another."
        )
    await store.save(ComposedWorkflow(owner=owner, name=name, summary=summary, document=document))
    jobs = job_steps(document)
    if jobs:
        # Said at the moment it is composed rather than left to the refusal a run would give: the
        # chemist is in the conversation *now*, and telling them afterwards costs them a turn to
        # learn something that was knowable when they asked.
        return (
            f"Saved the {name!r} workflow, {len(document.steps)} steps — but it will not run yet. "
            f"Step(s) {jobs} launch durable jobs, so its owner has to approve this version first "
            "(on the front door; there is no tool for it, because a workflow may not approve "
            "itself). Approving covers these steps exactly, so composing it again needs approving "
            "again."
        )
    return (
        f"Saved the {name!r} workflow, {len(document.steps)} steps. "
        f"Run it with run_composed_workflow(name={name!r})."
    )


@tool
async def run_composed_workflow(name: str, inputs: dict[str, str]) -> str:
    """Run a workflow you composed earlier, as one durable job.

    Returns a job id straight away; poll it with `get_durable_job_status`. The steps run without a
    model turn between them, so this costs one call rather than one per step.

    Args:
        name: The workflow's name, as given to `compose_workflow`.
        inputs: A value for each input the workflow declares.

    Returns:
        The job id to poll.

    Raises:
        ChemclawError: When you have no workflow of that name — the message lists the ones you do
            have — or when the workflow can no longer run here.
    """
    from chemclaw.agent.template_surface import TemplateSurface, run_ceiling_problems, step_problems
    from chemclaw.durable.template_job import template_fingerprint
    from chemclaw.templates.registry import start_template_run

    owner = require_actor()
    store = default_composed_store()
    workflow = await store.get(owner, name)
    if workflow is None:
        # The listing rides here rather than on a third tool: a name is already wrong on the turn
        # this fires, which is exactly when knowing the real ones is worth a tool result.
        available = [row.name for row in await store.list_for(owner)]
        raise ComposedWorkflowError(
            f"you have no composed workflow called {name!r}. "
            + (f"You have: {sorted(available)}." if available else "You have none yet.")
        )

    # **Checked again, and this is not belt-and-braces.** The store was written at a different time
    # and possibly against a different deployment: `side_effecting_tools()` grows when a bundle is
    # enabled, so a name that was a read when it was composed can be a write by the time it runs.
    problems = [
        *step_problems(workflow.document, TemplateSurface.resolve(with_signatures=False)),
        *run_ceiling_problems(workflow.document),
        *authored_problems(workflow.document, side_effecting_tools()),
    ]
    if problems:
        raise ComposedWorkflowError(
            f"the {name!r} workflow cannot run here any more:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\nCompose it again without those steps."
        )
    # Asked here and not at the write, because composing is deliberately not the decision: a
    # workflow nobody may compose is a workflow nobody can put in front of a person to approve.
    # The fingerprint is recomputed from the document *as stored*, so an approval only ever covers
    # the steps somebody actually read.
    withheld = unapproved_jobs(
        workflow.document, workflow.approved_fingerprint, template_fingerprint(workflow.document)
    )
    if withheld:
        raise ComposedWorkflowError(f"the {name!r} workflow is not approved to run: {withheld[0]}")
    return await start_template_run(workflow.document, dict(inputs))
