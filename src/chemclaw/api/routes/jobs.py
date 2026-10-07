"""The durable-run surface over `job_records`: what ran, how it ended, and stopping one.

Reads use the same functions as the agent's tools (`job_status`, `search_job_records`), so chat
and page agree. Collaborators are read through the front-door module at call time, the suite's
patch seam (see `chemclaw/api/routes/README.md`).
"""

from fastapi import FastAPI, HTTPException, Query, Request, Response

from chemclaw.agent.durable_tools import DurableJobStatus
from chemclaw.api import app as front_door
from chemclaw.api.auth import Principal
from chemclaw.api.deps import CurrentUser, _is_reviewer, _resolve_session
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.durable.job_record import JobRecordSummary

# Header carrying the next page's cursor, as on `GET /sessions`: the body is a bare JSON array the
# UI
# parses as one. The value is the last row's `job_id`; the store resolves its position, so nothing
# about the ordering is disclosed.
_NEXT_CURSOR = "X-Next-Cursor"


async def list_jobs(
    principal: CurrentUser,
    response: Response,
    text: str = "",
    connector: str = "",
    after: str = "",
) -> list[JobRecordSummary]:
    """Durable runs this system has finished, newest first — what ran, and why.

    Not owner-scoped, matching the agent's `find_past_jobs`, which is unscoped for cross-project
    learning. `requested_by` could not scope it anyway: identical requests share one run and row
    (the workflow id excludes the requester), so the column names who last asked. The resulting
    exposure of `rationale` text is recorded in `SECURITY.md` under "Accepted exposures".

    Paged by `job_record_search_limit`. `after` resumes strictly after the named run;
    `X-Next-Cursor`
    is present only when more matched. A keyset rather than an offset, since runs are recorded while
    a
    listing is read.
    """
    found = await front_door.search_job_records(text=text, connector=connector, after=after)
    if found.hits_truncated:
        # Only when the store saw a further row (it fetches one past the page).
        response.headers[_NEXT_CURSOR] = found.hits[-1].job_id
    return found.hits


async def get_job(
    job_id: str,
    principal: CurrentUser,
    request: Request,
    session_id: str | None = Query(
        default=None,
        description="The conversation reading this job; a report's `exhibit_id` is kept only "
        "when the caller can read that session and the run's artefact lives there.",
    ),
) -> DurableJobStatus:
    """One job's status and, once finished, its result.

    The function the agent's `get_durable_job_status` calls. Finished jobs answer indefinitely from
    `job_records`, which outlives Temporal's history. An open run reads `running`, or `queued` when
    nothing has started it, with the reason as `summary`.

    `session_id` is a reading context, never a filter or a refusal: `exhibit_id` is kept only for a
    reader of the session that started the run (`agent/durable_tools.readable_in_this_session`). A
    session the caller cannot read is treated as none, so the parameter is no oracle.
    """
    readable = session_id is not None and await _can_read(request, session_id, principal)
    token = set_current_session_id(session_id) if readable and session_id else None
    try:
        return await front_door.job_status(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    finally:
        if token is not None:
            reset_current_session_id(token)


async def _can_read(request: Request, session_id: str, principal: Principal) -> bool:
    """Whether `principal` may read `session_id` — the session gate's answer, as a boolean.

    The gate's 404 covers both stranger and unknown id; a 403 cannot arise from a read. Anything
    else
    (store unreachable) propagates.
    """
    try:
        await _resolve_session(request, session_id, principal)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            return False
        raise
    return True


async def cancel_durable_job(
    job_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    """Ask Temporal to stop a running job — an operator action, not an owner's.

    A run has no single owner: identical requests join one run (the workflow id excludes the
    requester), so cancelling it cancels it for everyone. It is therefore gated on the privileged
    role. Cooperative: 202 once the request is delivered; poll `GET /jobs/{id}` for the outcome.
    """
    if not _is_reviewer(principal):
        raise HTTPException(
            status_code=403,
            detail="cancelling a durable job is an operator action: the run may be shared by "
            "several requesters, so it needs a privileged role",
        )
    if not await front_door.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="no such job")
    return {"status": "cancelling", "job_id": job_id}


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    App decorators rather than `APIRouter` + `include_router`: since FastAPI 0.139 `include_router`
    is
    lazy, leaving opaque `_IncludedRouter` nodes that route-table walkers
    (`tests/test_route_auth_coverage.py`, `tests/test_service.py`) cannot see, and a standalone
    router
    has no `dependency_overrides_provider`, which disables `app.dependency_overrides`.
    """
    app.get("/jobs")(list_jobs)
    app.get("/jobs/{job_id}")(get_job)
    app.delete("/jobs/{job_id}", status_code=202)(cancel_durable_job)
