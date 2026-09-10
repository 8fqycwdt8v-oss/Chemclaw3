"""The durable-run surface over `job_records`: what ran, how it ended, and stopping one.

Reads answer from the same functions the agent's own tools call (`job_status`,
`search_job_records`), so a chemist polling in chat and one refreshing a page cannot disagree
about a run. All three collaborators are read through the front-door module at call time because
the suite patches them there (`chemclaw.agent.durable_tools.job_status` and friends) — see
`chemclaw/api/routes/README.md`.
"""

from fastapi import FastAPI, HTTPException, Response

from chemclaw.agent.durable_tools import DurableJobStatus
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, _is_reviewer
from chemclaw.durable.job_record import JobRecordSummary

# Where the next page's cursor is returned, because the body cannot carry it — the same header
# `GET /sessions` answers with, for the same reason and deliberately spelled the same way: this
# route answers with a bare JSON array that the companion UI parses as one, so an envelope
# (`{"jobs": [...], "next": ...}`) would break every deployed client to add a field, and a per-row
# field would have to go on `JobRecordSummary`, which is the agent tool's shape too.
#
# **The value is a `job_id`, not an opaque token, and that is the difference from the session
# listing.** A session cursor is base64 because its sort key (last activity + id) is not otherwise
# on the row, so spelling it out invites a client to construct one. Here the anchor is a row the
# caller already holds, the store resolves that row's position itself, and nothing about the
# ordering is disclosed — so it survives the ordering gaining a third component, which a
# spelled-out cursor does not.
_NEXT_CURSOR = "X-Next-Cursor"


async def list_jobs(
    principal: CurrentUser,
    response: Response,
    text: str = "",
    connector: str = "",
    after: str = "",
) -> list[JobRecordSummary]:
    """Durable runs this system has finished, newest first — what ran, and why.

    There was no job surface at all: status and result were reachable *only* as an agent tool
    inside a turn, so a chemist could not list what was running, and a result from a session
    that had since been evicted was unreachable even though `job_records` held it.

    **Not owner-scoped**, and that is the deployment's existing position rather than an
    oversight: `find_past_jobs` — the agent tool over this same table — is unscoped for the
    cross-project learning D-004/KM-9 argues for, and a read that the agent can make on a
    chemist's behalf is not one to withhold from the chemist.

    **And `requested_by` could not be the scope even if the policy changed**, which is the part
    this docstring used to leave to the reader. `job_workflow_id` hashes `[connector, job, payload]`
    and deliberately excludes the requester (D-011 — never compute twice), so two chemists asking
    for the same calculation join one run and one row; `job_record_store`'s upsert then sets
    `requested_by = EXCLUDED.requested_by` so the row does not contradict itself. The column names
    who last asked, not an owner, and filtering on it would withhold a run's answer from a chemist
    who requested that very run. `cancel_durable_job` below makes the same argument out loud.

    The exposure this leaves — a `rationale` is free prose naming a programme, a compound code and
    a reason, readable by every authenticated principal — is stated in `SECURITY.md` under
    "Accepted exposures", because an accepted data-exposure decision belongs where a reviewer looks
    for one rather than only in an API-design comment.

    **`job_record_search_limit` is now the page, not the end of the list.** It always bounded this
    answer and nothing said so: a chemist with more finished runs than the cap could not reach the
    older ones from any client, and the listing looked complete. `after` resumes strictly after the
    run a cursor names, and `X-Next-Cursor` is present only when more matched — absent means there
    is not. The cursor is a keyset (the last row's `job_id`) rather than an offset, because runs
    are recorded while a listing is read and a page boundary counted in rows would repeat and skip.
    """
    found = await front_door.search_job_records(text=text, connector=connector, after=after)
    if found.hits_truncated:
        # Only when the store actually saw a further row. It asks for one beyond the page to know,
        # so this is evidence rather than the "a full page might mean more" guess — and a client
        # that follows a cursor into an empty page is a round trip nobody needed.
        response.headers[_NEXT_CURSOR] = found.hits[-1].job_id
    return found.hits


async def get_job(
    job_id: str,
    principal: CurrentUser,
) -> DurableJobStatus:
    """One job's status and, once finished, its result.

    The same function the agent's `get_durable_job_status` calls, so a chemist polling in chat
    and a chemist refreshing a page cannot get different answers about one run. Answers for
    finished jobs indefinitely: Temporal expires a closed run's history, and `job_records` is
    what survives it (D-157).
    """
    try:
        return await front_door.job_status(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc


async def cancel_durable_job(
    job_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    """Ask Temporal to stop a running job — an operator action, not an owner's.

    The obvious design — "let a chemist cancel their own job" — cannot be built, and the reason
    is a property of the system rather than a missing column. `job_workflow_id` hashes
    `[connector, job, payload]` and *deliberately excludes the requester*, so two chemists
    asking for the identical campaign rejoin one run (D-011: never compute twice). A running job
    therefore has no single owner: cancelling it cancels it for everyone who joined it, and the
    first requester is not more entitled to that than the second.

    So it is gated on the same privileged role every other consequential write is, and the cost
    — a chemist cannot stop their own runaway run without an operator — is stated rather than
    hidden behind a scope check that would read as ownership and not be it.

    Cancellation is cooperative: this returns 202 once the request is delivered, not once the
    run has stopped. Poll `GET /jobs/{id}` for the outcome.
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

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.get("/jobs")(list_jobs)
    app.get("/jobs/{job_id}")(get_job)
    app.delete("/jobs/{job_id}", status_code=202)(cancel_durable_job)
