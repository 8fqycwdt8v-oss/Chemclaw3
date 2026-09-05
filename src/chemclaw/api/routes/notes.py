"""Reading one knowledge note over HTTP, so a citation can be followed rather than only shown.

The knowledge graph was readable by the agent and by nobody else: `expand_note`
(`chemclaw.agent.graph_tools`) returns a `NoteView` — the note's body plus its stated-relation
neighbourhood — and is reachable *only* as an agent tool. A surface that renders `note-…` tokens as
citation chips therefore had nothing to resolve them against, so a citation was a highlight rather
than a link, and checking one meant asking the agent to paste it back.

Deliberately the *same* `NoteView` the tool returns, from the same function. A second projection of
a note would be a second answer to "what does this note say", and the two would disagree the first
time either changed — which matters more here than usual, because the body comes back **framed**
(`chemclaw.agent.framing`): note content may be ingested rather than authored, and the envelope is
what marks it as data. A route that unwrapped it to look tidier would be handing a surface the one
representation the injection discipline exists to avoid.

`CurrentUser`-gated and deliberately not owner-scoped, which is the same position `GET /jobs` takes
and for the same reason: the graph is the organisation's shared knowledge (D-004/KM-9 argue for
cross-project reuse), a note has no owner, and a read the agent already makes on a chemist's behalf
is not one to withhold from the chemist. What the gate buys is that a caller exists and is inside
the per-principal rate budget.
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

    404 rather than 400 for an unknown id, and the distinction is worth stating: the commonest
    real cause is a citation to a note still awaiting its PR-gate review (D-018), which is a note
    that does not exist *yet* rather than a malformed request. `expand_note` raises `ChemclawError`
    for it — chemclaw's always-safe bad-input contract — so the message is safe to pass through,
    and a chip that cannot resolve gets told why.

    `hops` is clamped inside `expand_note` against `graph_max_hops`, so this route needs no bound
    of its own; adding one would be a second ceiling to keep in step with the first.

    Read through the front-door module at call time rather than imported by name — the suite's
    patch seam is `chemclaw.api.app` (see `routes/README.md`).

    **Revalidated with an `ETag`, never `immutable`, and `private` despite having no owner.** A note
    id is stable *across edits* — the graph is Markdown in Git and a PR-gate merge rewrites a body
    under the same id — the neighbourhood is other notes' business, and `Note.is_current` is
    evaluated against `date.today()`, so a neighbour leaves this view on the day its `valid_to`
    passes with nothing written at all. None of that is content-addressed, which is why the
    frontend's `immutable` premise does not hold here; `routes/caching.py` carries the argument and
    the reason a `CurrentUser`-gated response is still not `public`.
    """
    try:
        view = await front_door.expand_note(note_id, hops)
    except ChemclawError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    not_modified = revalidatable(request, response, view)
    return not_modified if not_modified is not None else view


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.

    `response_model` is stated rather than inferred because the handler may return a bare 304
    `Response`, which makes its annotation a union FastAPI must not build a schema from.
    """
    app.get("/notes/{note_id}", response_model=NoteView)(get_note)
