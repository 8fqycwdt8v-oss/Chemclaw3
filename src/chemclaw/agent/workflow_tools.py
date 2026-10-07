"""Composing a reusable multi-step workflow, and running one.

The agent writes a procedure down once and afterwards runs it as one durable call, instead of a
git-committed template. Safety lives in `templates/composed`: `authored_problems` forbids any
side-effecting or `write_tools` step, and `unapproved_jobs` withholds a durable `job` step until the
owner approves that version of the document at the front-door route or the terminal's
`/approve-workflow` — never through a tool. Listing workflows is not a tool: the refusal for an
unknown name lists the ones that exist.
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

    # Short docstrings here: pydantic publishes a class docstring as the JSON-schema `description`
    # on
    # every model call, so rationale goes in comments.
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

    The same model a `data/templates/*.yaml` file parses into, so every template validator applies
    and `TemplateWorkflow` runs it unchanged.
    """
    contradictory = [
        step.id
        for step in steps
        if sum(1 for field in (step.tool, step.job, step.prompt) if field) != 1
    ]
    if contradictory:
        # Refused rather than resolved by precedence: the branch below would silently drop the tool,
        # and
        # the approver sees only the rendered document.
        raise ComposedWorkflowError(
            f"step(s) {contradictory} each have to be exactly one thing: a `tool` to call, a `job` "
            "to run, or a `prompt` to reason with. Split them, or drop the fields that do not "
            "belong."
        )
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
    `run_composed_workflow` runs the whole thing in one call, with no model turn between steps, and
    a run that fails resumes rather than starting over. Composing the same name again replaces it.

    **No step may call a tool that changes anything**, because the run has no conversation to
    approve a plan in — ask for the change in the conversation, where a person can see it.

    **A durable job step is allowed and will not run until a person approves this workflow.** Say so
    when you hand the name back: its owner approves it where they are — `/approve-workflow` at a
    terminal, the workflow screen on the front door — and it covers the steps exactly as they
    stand, so composing it again needs approving again. Two jobs that do
    not read each other are fine; one that waits for another will not fit, because a job's budget is
    most of the whole run's — split that into two workflows.

    Refer to values with `${inputs.<name>}` and to an earlier step with `${steps.<id>.result}`. A
    step naming no earlier step runs at the same time as its neighbours, so do not chain steps that
    do not need each other.

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
        # The checks a `data/templates/` file passes, plus the agent-authored one. Resolved without
        # signatures, which would import every bundle's server module.
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
        # Quote `MAX_PER_OWNER`, not `len(existing)`: the store fetches one past the cap.
        raise ComposedWorkflowError(
            f"you are at the limit of {MAX_PER_OWNER} composed workflows. The most recent are: "
            f"{sorted(row.name for row in existing)}. Re-compose one of them under its own name, "
            "or ask the chemist to forget one they no longer use."
        )
    await store.save(ComposedWorkflow(owner=owner, name=name, summary=summary, document=document))
    jobs = job_steps(document)
    if jobs:
        # Said at composition, while the chemist is in the conversation, rather than on a later run.
        return (
            f"Saved the {name!r} workflow, {len(document.steps)} steps — but it will not run yet. "
            f"Step(s) {jobs} launch durable jobs, so its owner has to approve this version first. "
            "There is no tool for that, because a workflow may not approve itself: they do it "
            "where they are — `/approve-workflow` at the terminal, or the workflow screen on the "
            "front door. Approving covers these steps exactly, so composing it again needs "
            "approving again."
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

    # Checked again at run time: `side_effecting_tools()` grows when a bundle is enabled, so a step
    # that
    # was a read when composed may be a write now.
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
    # Asked here, not at composition, so a workflow can exist to be approved. The fingerprint is
    # recomputed from the stored document, so an approval covers only the steps somebody read.
    withheld = unapproved_jobs(
        workflow.document, workflow.approved_fingerprint, template_fingerprint(workflow.document)
    )
    if withheld:
        raise ComposedWorkflowError(f"the {name!r} workflow is not approved to run: {withheld[0]}")
    # Scoped by owner and version: the run id is an idempotency key, and unscoped two chemists' (or
    # two
    # versions') workflows of one name would rejoin each other's runs.
    return await start_template_run(
        workflow.document,
        dict(inputs),
        f"{owner}:{template_fingerprint(workflow.document)}",
    )
