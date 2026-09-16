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

**The DELETE is the cap's other half.** At `MAX_PER_OWNER` with no way to remove one, the only way
to make room was to re-compose over a name — which destroys the document anyway and leaves a row
whose name lies about its contents. It is a route rather than a tool for no security reason at all:
the agent may already replace a workflow by composing over it, so a `forget_workflow` tool would add
no reach. It is here because this is where a chemist's own workflows already are.

**The GET is not decoration.** An approval that names no steps is
`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool` again one layer over: a person
approving a *name* has approved whatever it currently contains. So the read hands back the steps,
the subset that launch jobs, and the fingerprint to post — and the POST refuses a fingerprint that
is not the document's current one with a 409, because a workflow that changed between being shown
and being approved is a different procedure.
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

    The reading itself is `composed.step_call`, shared with the terminal's `/approve-workflow`,
    because two surfaces rendering one approval from two copies of this is two chances for one of
    them to stop showing a field.
    """
    calls, arguments, prompt = step_call(step)
    return WorkflowStepOut(
        id=step.id, kind=step.kind, calls=calls, arguments=arguments, prompt=prompt
    )


async def list_workflows(principal: CurrentUser) -> WorkflowListOut:
    """This caller's composed workflows, most recently changed first.

    **Discovery, which the feature shipped without.** The only way to find a workflow's name was
    the refusal `run_composed_workflow` gives an unknown one — which works once you have already
    guessed wrong, and not at all for a surface that wants to show a chemist what they have. A
    listing was deferred on the grounds that a third *tool* costs prompt prefix on every model call;
    a route costs none, which is why it belongs here rather than there.

    `approved` is derived and never the stored flag: a workflow re-composed after approval has a
    fingerprint that no longer matches, and a listing reporting the column would call it approved
    when the next run will refuse it.

    Args:
        principal: The authenticated person. Their oid is the owner this resolves against.

    Returns:
        The caller's workflows, and whether the page was clamped.
    """
    rows = await default_composed_store().list_for(principal.oid)
    # The store fetches one past the cap, so "at the cap" and "over it" are distinguishable; the
    # page a caller reads is still the cap. `>= MAX_PER_OWNER` was the old test and could not tell
    # a full page from a clamped one — the same confusion on the reading side that the store's
    # `LIMIT MAX_PER_OWNER` was on the writing side.
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
        # **The procedure, not its step ids.** Ids alone were what this returned, and a person shown
        # `["rank", "say"]` and asked to authorize real compute has approved a name the model chose
        # — an approval that names no job authorizes every job. The sentence this widening rests on
        # is that "the job's name and its arguments are in the document they approved", and until
        # the document was rendered that sentence was false of every caller.
        steps=[_step_out(step) for step in workflow.document.steps],
        job_steps=job_steps(workflow.document),
        # Derived, never the stored flag — `approved_by` and `approved_at` name whichever version
        # `approved_fingerprint` is, and a re-compose leaves those columns standing while the
        # approval lapses. Answering the question here is what stops a client rendering a stale
        # approver as a current approval.
        approved=not unapproved_jobs(workflow.document, workflow.approved_fingerprint, fingerprint),
        composed_in_session=workflow.session_id,
        fingerprint=fingerprint,
        approved_fingerprint=workflow.approved_fingerprint,
        approved_by=workflow.approved_by,
        approved_at=workflow.approved_at,
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
    if not await store.approve(principal.oid, name, current):
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


async def forget_workflow(name: str, principal: CurrentUser) -> Response:
    """Delete one of this caller's composed workflows.

    **A real delete rather than a tombstone**, because the alternative a chemist had was worse:
    with a cap of `MAX_PER_OWNER` and no way to remove one, the only way to make room was to
    re-compose over a name — which destroys the document anyway *and* leaves a row whose name lies
    about what it contains. The grant already permits DELETE on this table for offboarding
    (`agent/leaver.py`); this is the same verb for the owner's own act, and the owner scoping is
    the authorization exactly as it is for the two routes above.

    Args:
        name: The workflow, in the caller's own namespace.
        principal: The authenticated person. Their oid is the owner this resolves against.

    Returns:
        204 on success.

    Raises:
        HTTPException: 404 when this caller has no workflow of that name. Deliberately not a
            silent 204: the caller asked for a specific thing to stop existing, and "it was already
            gone" and "you named somebody else's" are the same answer here only because the name
            resolves against their own rows.
    """
    if not await default_composed_store().forget(principal.oid, name):
        raise HTTPException(status_code=404, detail=f"no composed workflow called {name!r}")
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    On the app's own decorators rather than an `APIRouter`, for the reason every sibling
    `register` gives: since FastAPI 0.139 `include_router` is lazy, and the route table this
    repository walks by type would hold opaque nodes instead of routes.
    """
    # Before the parameterised path, so `/workflows` is not matched as a workflow named
    # "workflows" — Starlette matches in registration order.
    app.get("/workflows")(list_workflows)
    app.get("/workflows/{name}")(get_workflow)
    app.post("/workflows/{name}/approval", status_code=204)(approve_workflow)
    app.delete("/workflows/{name}", status_code=204)(forget_workflow)
