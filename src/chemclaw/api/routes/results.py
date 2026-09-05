"""Reading back the full text of what a tool returned, one result at a time.

The other half of `ToolResultEvent.result_ref`. The stream keeps its 200-character preview budget
— which exists so a whole evidence sweep is never pushed at every consumer — and a surface that
has decided to render *one* result pulls that one result here, once. Any concept that wants a
hazard table, a charge table or a solvent ranking needs the typed payload the preview cannot
carry, and this is the smallest change that gives it one without re-opening the streaming budget.

**Scoped under the session on purpose.** A ref is the SHA-256 of a result's own text, so it is
unguessable but not secret — anyone who can reproduce the bytes can compute it. Hanging the route
off `/sessions/{session_id}` means it resolves through `resolve_session`, the front door's existing
ownership gate, and the store's own read joins the link row for the same session on top of that. A
bare `/tool-results/{ref}` would have needed a new authorization story invented for it, and a ref
that doubles as a bearer token is the story it would have ended up with.
"""

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from chemclaw.api import app as front_door
from chemclaw.api.deps import resolve_session
from chemclaw.api.routes.caching import revalidatable
from chemclaw.api.tool_results import StoredToolResult


async def get_tool_result(
    session_id: str, ref: str, request: Request, response: Response
) -> StoredToolResult | Response:
    """The full text of one tool result this session produced.

    404 for a ref this session never produced, a ref retention has swept, and a ref belonging to
    somebody else's conversation — one answer for three misses, because telling them apart would
    confirm to an unauthorized caller that a ref exists somewhere (the store's read makes the same
    argument on the SQL side).

    An empty `result_ref` on the event means the result was never stored — the store is off, the
    result was over `stream_max_result_bytes`, or the write failed — and a client must render the
    preview rather than fetch. It should never reach this route with one.

    Read through the front-door module at call time rather than imported by name, which is the
    convention `routes/README.md` states: the suite's patch seam is `chemclaw.api.app`.

    The ownership gate is attached at registration (`dependencies=[Depends(resolve_session)]`)
    rather than taken as a parameter, the same way the event stream does it: nothing in the answer
    depends on the live session object, only on the caller being entitled to this conversation.

    **Revalidated, not `immutable`, and `private` rather than `public`.** The ref addresses the
    *bytes*, so `text` cannot change — but `tool` and `correlation_id` collapse to `''` the moment a
    second call in this session returns the same text (`api/tool_results.py::_UPSERT_LINK`), so the
    body is not immutable and a year-long promise would pin a withdrawn label in the client.
    `routes/caching.py` carries the whole argument, including why `public` on a per-owner resource
    behind `resolve_session` is a hazard rather than a preference.
    """
    stored = await front_door.load_tool_result(session_id, ref)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such tool result")
    not_modified = revalidatable(request, response, stored)
    return not_modified if not_modified is not None else stored


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
    app.get(
        "/sessions/{session_id}/tool-results/{ref}",
        dependencies=[Depends(resolve_session)],
        response_model=StoredToolResult,
    )(get_tool_result)
