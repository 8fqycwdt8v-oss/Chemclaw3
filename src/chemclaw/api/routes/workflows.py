"""A person's standing approval for the durable jobs one composed workflow may launch.

`D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction` refuses a `job` step in a
workflow the agent composed, because a template run has no session and so nothing can put a human
in front of an unreviewed launch. These two routes are the human, moved to where one exists.

**Routes and deliberately not agent tools**, for the reason `api/routes/plan.py::decide_plan`
gives in as many words: a model must never be able to authorize its own plan. The guarantee here
is obtained the way that one's is — by not building the tool rather than by removing it afterwards
— and `tests/test_api_workflows.py` asserts the absence over the whole registered surface rather
than trusting this paragraph.

**Owner-scoped, and that is the authorization.** A composed workflow is keyed `(owner, name)`, and
both routes resolve against the caller's *own* rows. So there is no "approve somebody else's
workflow" to refuse: a name that is not yours simply is not found, which is the same answer the
store gives the agent and one fewer branch than a 403 nobody can reach.

**The GET is not decoration.** An approval that names no steps is
`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` again one layer over: a person
approving a *name* has approved whatever it currently contains. So the read hands back the steps,
the subset that launch jobs, and the fingerprint to post — and the POST refuses a fingerprint that
is not the document's current one with a 409, because a workflow that changed between being shown
and being approved is a different procedure.
"""

import logging

from fastapi import FastAPI, HTTPException
from starlette.responses import Response

from chemclaw.api.deps import CurrentUser
from chemclaw.api.schemas import WorkflowApprovalIn, WorkflowApprovalOut
from chemclaw.durable.template_job import template_fingerprint
from chemclaw.templates.composed import default_composed_store, job_steps

logger = logging.getLogger(__name__)


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
    return WorkflowApprovalOut(
        name=workflow.name,
        summary=workflow.summary,
        steps=[step.id for step in workflow.document.steps],
        job_steps=job_steps(workflow.document),
        fingerprint=template_fingerprint(workflow.document),
        approved_fingerprint=workflow.approved_fingerprint,
        approved_by=workflow.approved_by,
    )


async def approve_workflow(name: str, body: WorkflowApprovalIn, principal: CurrentUser) -> Response:
    """Record that this person approved this exact version's durable job launches.

    **What is approved is one version of one workflow.** The phrase this feature was asked for was
    a standing approval *per actor*; keyed on the document's hash instead, re-composing lapses the
    approval automatically, because the stored fingerprint stops matching and nothing has to
    remember to clear it. A per-actor permission never lapses, so a workflow re-composed into
    something else would inherit the approval granted to what it used to be.

    Recording is idempotent and re-approving is meaningful: the approval is not spent by a run, so
    the same version may run as often as its owner asks. That is the deliberate difference from a
    plan approval, which authorizes one turn — a plan is a thing the agent chose this turn, while a
    composed workflow is a procedure a person keeps.

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
    if not await store.approve(principal.oid, name, current, principal.oid):
        # The row went between the read and the write. A 404 rather than a silent success: the
        # caller asked to authorize something and nothing was authorized.
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    # **The row is the record, and that is this repository's convention rather than a shortcut.**
    # No route writes an `AuditEvent`: that trail is the tool-call middleware's, shaped around a
    # tool, an outcome and a latency, and an approval is none of those. A human decision is audited
    # here by being a row naming who made it — `approved_by` and `approved_at` — exactly as
    # `plan_approvals.actor`, `experiment_protocol_status_events.actor`,
    # `pending_requests.answered_by` and `effects.approved_by` each are.
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    On the app's own decorators rather than an `APIRouter`, for the reason every sibling
    `register` gives: since FastAPI 0.139 `include_router` is lazy, and the route table this
    repository walks by type would hold opaque nodes instead of routes.
    """
    app.get("/workflows/{name}")(get_workflow)
    app.post("/workflows/{name}/approval", status_code=204)(approve_workflow)
