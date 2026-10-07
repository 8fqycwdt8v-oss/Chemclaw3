"""Reading one knowledge note over HTTP, so a citation can be followed rather than only shown.

Returns the same `NoteView` the agent's `expand_note` returns, from the same function, so there is
one answer to what a note says. The body stays framed (`chemclaw.agent.framing`), since note content
may be ingested and the envelope marks it as data. `CurrentUser`-gated and not owner-scoped, like
`GET /jobs`: the graph is shared organisational knowledge.
"""

from fastapi import FastAPI, HTTPException, Request, Response

from chemclaw.agent.graph_tools import NoteView
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser
from chemclaw.api.routes.caching import revalidatable
from chemclaw.core.errors import ChemclawError


async def get_note(
    note_id: str,
    principal: CurrentUser,
    request: Request,
    response: Response,
    hops: int = 1,
) -> NoteView | Response:
    """One note's body and the notes within `hops` stated relations of it.

    404 for an unknown id: a citation is a string in prose, so a miss is a missing note (usually a
    typo'd `[[wikilink]]`). `expand_note` raises `ChemclawError`, whose message is safe to pass
    through.
    `hops` is clamped inside `expand_note` against `graph_max_hops`. Read through the front-door
    module
    at call time (the suite's patch seam). Revalidated with an `ETag`, `private`; see
    `api/routes/caching.py`.
    """
    try:
        view = await front_door.expand_note(note_id, hops)
    except ChemclawError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    not_modified = revalidatable(request, response, view)
    return not_modified if not_modified is not None else view


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    `response_model` is explicit because the handler may return a bare 304 `Response`.
    """
    app.get("/notes/{note_id}", response_model=NoteView)(get_note)
