"""Reading back the full text of what a tool returned, one result at a time.

The other half of `ToolResultEvent.result_ref`: the stream carries a short preview, and a surface
that renders one result fetches its full text here. Scoped under the session so it resolves through
`resolve_session`'s ownership gate; a ref is a SHA-256 of the text, unguessable but not secret, and
must not act as a bearer token.
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

    404 for a ref never produced here, swept by retention, or belonging to another conversation —
    one answer, so an unauthorized caller learns nothing. An empty `result_ref` on an event means
    the result was not stored; a client renders the preview instead. Responses are `private` and
    revalidated, not immutable: `tool` and `correlation_id` can collapse to `''` when a second call
    returns the same text (see `api/routes/caching.py`).
    """
    stored = await front_door.load_tool_result(session_id, ref)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such tool result")
    not_modified = revalidatable(request, response, stored)
    return not_modified if not_modified is not None else stored


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, which would hide it from tests
    that walk the route table and disable `app.dependency_overrides`. `response_model` is explicit
    because the handler may return a bare 304 `Response`.
    """
    app.get(
        "/sessions/{session_id}/tool-results/{ref}",
        dependencies=[Depends(resolve_session)],
        response_model=StoredToolResult,
    )(get_tool_result)
