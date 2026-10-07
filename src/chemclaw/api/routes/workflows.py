"""A person's standing approval for the durable jobs one composed workflow may launch.

A `job` step in an agent-composed workflow needs a human approval, and a template run has no session
to ask in, so these routes are where the human approves. Routes, not agent tools: a model must never
authorize its own plan (`tests/test_api_workflows.py` asserts no such tool exists). Owner-scoped: a
workflow is keyed `(owner, name)` and every route resolves against the caller's own rows, so another
person's workflow is simply not found.

The GET returns the steps, the job-launching subset and the fingerprint to post, so an approval
names what it authorizes; the POST answers 409 to a fingerprint that is not the document's current
one. The DELETE lets an owner at `MAX_PER_OWNER` make room.
"""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.responses import Response

from chemclaw.api.deps import CurrentUser
from chemclaw.api.schemas import (
    WorkflowApprovalIn,
    WorkflowApprovalOut,
    WorkflowListOut,
    WorkflowStepOut,
    WorkflowSummaryOut,
)
from chemclaw.durable.template_job import template_fingerprint
from chemclaw.templates.composed import (
    MAX_PER_OWNER,
    default_composed_store,
    job_steps,
    step_call,
    unapproved_jobs,
)

logger = logging.getLogger(__name__)


def _step_out(step: Any) -> WorkflowStepOut:
    """One step as the approver needs to read it: what it calls, and with what.

    Reads through `composed.step_call`, shared with the terminal's `/approve-workflow`, so both
    surfaces render the same fields.
    """
    calls, arguments, prompt = step_call(step)
    return WorkflowStepOut(
        id=step.id, kind=step.kind, calls=calls, arguments=arguments, prompt=prompt
    )


async def list_workflows(principal: CurrentUser) -> WorkflowListOut:
    """This caller's composed workflows, most recently changed first.

    A route rather than a tool, so discovery costs no prompt prefix. `approved` is derived from the
    fingerprint, never the stored flag, so a re-composed workflow is not reported approved.

    Args:
        principal: The authenticated person. Their oid is the owner this resolves against.

    Returns:
        The caller's workflows, and whether the page was clamped.
    """
    rows = await default_composed_store().list_for(principal.oid)
    # The store fetches one past the cap, so a full page can be told from a clamped one.
    truncated = len(rows) > MAX_PER_OWNER
    return WorkflowListOut(
        workflows=[
            WorkflowSummaryOut(
                name=row.name,
                summary=row.summary,
                step_count=len(row.document.steps),
                job_steps=job_steps(row.document),
                approved=not unapproved_jobs(
                    row.document, row.approved_fingerprint, template_fingerprint(row.document)
                ),
            )
            for row in rows[:MAX_PER_OWNER]
        ],
        truncated=truncated,
    )


async def get_workflow(name: str, principal: CurrentUser) -> WorkflowApprovalOut:
    """What approving this workflow would authorize, for the person about to decide.

    Args:
        name: The workflow's name, in the caller's own namespace.
        principal: The authenticated person. Their oid is the owner this resolves against.

    Returns:
        The steps, the ones that launch jobs, and the fingerprint to post back.

    Raises:
        HTTPException: 404 when this caller has no workflow of that name.
    """
    workflow = await default_composed_store().get(principal.oid, name)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    fingerprint = template_fingerprint(workflow.document)
    return WorkflowApprovalOut(
        name=workflow.name,
        summary=workflow.summary,
        description=workflow.document.description,
        # The procedure, not its step ids: an approval must show each job's name and arguments.
        steps=[_step_out(step) for step in workflow.document.steps],
        job_steps=job_steps(workflow.document),
        # Derived, never the stored flag: a re-compose leaves `approved_by`/`approved_at` standing
        # while the approval lapses.
        approved=not unapproved_jobs(workflow.document, workflow.approved_fingerprint, fingerprint),
        composed_in_session=workflow.session_id,
        fingerprint=fingerprint,
        approved_fingerprint=workflow.approved_fingerprint,
        approved_by=workflow.approved_by,
        approved_at=workflow.approved_at,
    )


async def approve_workflow(name: str, body: WorkflowApprovalIn, principal: CurrentUser) -> Response:
    """Record that this person approved this exact version's durable job launches.

    Keyed on the document's fingerprint, so re-composing lapses the approval automatically.
    Idempotent and not spent by a run: unlike a plan approval, which authorizes one turn, a composed
    workflow is a procedure a person keeps and may run repeatedly.

    Args:
        name: The workflow, in the caller's own namespace.
        body: The fingerprint the caller was shown.
        principal: The authenticated person, recorded as the approver.

    Returns:
        204 on success.

    Raises:
        HTTPException: 404 when this caller has no such workflow; 409 when the posted fingerprint
            is not the document's current one, which means it changed after it was shown.
    """
    store = default_composed_store()
    workflow = await store.get(principal.oid, name)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    current = template_fingerprint(workflow.document)
    if body.fingerprint != current:
        raise HTTPException(
            status_code=409,
            detail="the workflow changed since it was shown; re-read it and approve again",
        )
    if not await store.approve(principal.oid, name, current):
        # The row went between the read and the write: nothing was authorized, so 404.
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    # The row is the audit record (`approved_by`, `approved_at`), as for other human decisions;
    # `AuditEvent` is the tool-call middleware's trail.
    return Response(status_code=204)


async def forget_workflow(name: str, principal: CurrentUser) -> Response:
    """Delete one of this caller's composed workflows.

    A real delete, not a tombstone; without it the only way under `MAX_PER_OWNER` was re-composing
    over a name. Owner scoping is the authorization.

    Args:
        name: The workflow, in the caller's own namespace.
        principal: The authenticated person. Their oid is the owner this resolves against.

    Returns:
        204 on success.

    Raises:
        HTTPException: 404 when this caller has no workflow of that name.
    """
    if not await default_composed_store().forget(principal.oid, name):
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, so tests that walk the route
    table see them.
    """
    # Before the parameterised path: Starlette matches in registration order.
    app.get("/workflows")(list_workflows)
    app.get("/workflows/{name}")(get_workflow)
    app.post("/workflows/{name}/approval", status_code=204)(approve_workflow)
    app.delete("/workflows/{name}", status_code=204)(forget_workflow)
