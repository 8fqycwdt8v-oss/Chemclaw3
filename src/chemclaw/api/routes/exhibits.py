"""Artefacts over HTTP — the pane beside the chat reads, edits, pins and exports them here.

**A chemist's edit is a REST write, not a tool call composed by a click**, the line
`api/routes/protocols.py` draws for a design: everything the *agent* does reaches it as a chat turn,
and a person authoring a revision is recorded as a person (`author_kind="human"`) without the agent
being involved. The agent learns of the edit on its next turn (`agent/exhibit_notes`).

**Session-scoped through `resolve_session`** — the owner or a member the owner let in; anybody else
gets the same 404 an unknown session id gets, so these routes are no oracle for which ids exist.
Members may revise as well as read (`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`
decision 5), and each revision records who wrote it.

**A stale edit is a 409 bound to a revision**, `{"code": "stale_revision", "head_revision": N}`:
the client names the revision it edited, and a write derived from anything but the head is refused
rather than allowed to discard the revision it did not see.

**Audited the way a person's decision is audited here**: the revision row names its author, kind,
time and correlation id, and every write also emits one structured `exhibit.*` log event. No route
writes an `AuditEvent` — that trail is the tool-call middleware's, shaped around a tool, an outcome
and a latency (`api/routes/workflows.py` states the convention).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.agent.session_events import record_session_event
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentSession, CurrentUser
from chemclaw.api.events import ExhibitEvent
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded
from chemclaw.exhibits.bindings import bind_for_write, resolved_view
from chemclaw.exhibits.diff import diff_specs
from chemclaw.exhibits.export import MEDIA_TYPES, export_filename, resolve_export
from chemclaw.exhibits.models import (
    EXHIBIT_ID,
    PUSH_KIND,
    ExhibitDiff,
    ExhibitHeader,
    ExhibitKind,
    ExhibitLimit,
    ExhibitRevision,
    ExhibitView,
    InvalidExhibit,
    ResultSpec,
    Spec,
    StaleRevision,
    UnknownExhibit,
    parse_spec,
    require_creatable,
    require_writable,
)
from chemclaw.exhibits.sources import require_source_stored
from chemclaw.exhibits.store import default_exhibit_store

logger = logging.getLogger(__name__)

#: An artefact id in a path, held to the minted shape: a malformed one is a 422 naming the
#: parameter rather than a lookup, and it reveals nothing a 404 would not.
ExhibitId = Annotated[str, Path(pattern=EXHIBIT_ID.pattern)]


class ExhibitListOut(BaseModel):
    """A session's artefacts, and whether this deployment offers artefacts and html ones at all."""

    enabled: bool
    html_enabled: bool
    exhibits: list[ExhibitHeader] = Field(default_factory=list)


class ExhibitIn(BaseModel):
    """A person creating an artefact — the pane's "Pin as artefact" sends a `result` this way."""

    kind: ExhibitKind
    title: str
    spec: dict[str, Any]

    model_config = ConfigDict(extra="forbid")


class ExhibitRevisionIn(BaseModel):
    """A person's revision: what they edited, the whole new spec, and why."""

    parent_revision: int
    spec: dict[str, Any]
    change_note: str = ""
    title: str | None = None

    model_config = ConfigDict(extra="forbid")


class ExhibitRevisionsOut(BaseModel):
    """An artefact's history, oldest first."""

    revisions: list[ExhibitRevision] = Field(default_factory=list)


class ExhibitIndexOut(BaseModel):
    """Artefacts across every session the caller owns or was let into, newest first."""

    exhibits: list[ExhibitHeader] = Field(default_factory=list)


async def list_exhibits(session_id: str, live: CurrentSession) -> ExhibitListOut:
    """The session's artefacts, most recently updated first, and whether the feature is on.

    `enabled` is `agent_exhibits_enabled`: off, the agent holds no artefact tools and a surface
    hides the pane. What a session already holds is still listed, because switching the agent's
    tools off is not a reason to hide what a chemist pinned. `html_enabled` is
    `agent_html_artefacts_enabled`, under the same rule: off refuses a new html artefact and still
    lists the ones the session holds.
    """
    return ExhibitListOut(
        enabled=settings.agent_exhibits_enabled,
        html_enabled=settings.agent_html_artefacts_enabled,
        exhibits=await default_exhibit_store().headers(session_id),
    )


async def create_exhibit_route(
    session_id: str, body: ExhibitIn, principal: CurrentUser, live: CurrentSession
) -> ExhibitView:
    """Create an artefact as a person — revision 1, `author_kind` human.

    A `result` artefact pins a stored tool result: its `result_ref` must be one this session can
    fetch, so a person cannot pin bytes another conversation produced — the rule a binding is held
    to as well (`exhibits.bindings`).
    """
    spec = await _parsed(session_id, body.spec, title=body.title, change_note="", creating=True)
    if spec.kind != body.kind:
        raise HTTPException(
            status_code=422, detail=f"kind is {body.kind!r} and the spec is a {spec.kind!r}"
        )
    await _require_session_result(session_id, spec)
    try:
        view = await default_exhibit_store().create(
            session_id,
            title=body.title,
            spec=spec,
            author_kind="human",
            author=principal.oid,
            correlation_id=get_current_correlation_id() or "",
        )
    except ExhibitLimit as exc:
        raise HTTPException(
            status_code=409, detail={"code": "exhibit_limit", "message": str(exc)}
        ) from exc
    await _announce(view, "created")
    return await resolved_view(view)


async def get_exhibit(
    session_id: str, exhibit_id: ExhibitId, live: CurrentSession, revision: int = 0
) -> ExhibitView:
    """One revision of an artefact — the head for `revision=0` — with every binding resolved."""
    view = await default_exhibit_store().view(session_id, exhibit_id, revision)
    if view is None:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, revision))
    return await resolved_view(view)


async def list_revisions(
    session_id: str, exhibit_id: ExhibitId, live: CurrentSession
) -> ExhibitRevisionsOut:
    """An artefact's history, oldest first — enough to pick a revision to open or compare."""
    revisions = await default_exhibit_store().revisions(session_id, exhibit_id)
    if revisions is None:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, 0))
    return ExhibitRevisionsOut(revisions=revisions)


async def get_exhibit_diff(
    session_id: str,
    exhibit_id: ExhibitId,
    live: CurrentSession,
    from_revision: int = Query(default=0, alias="from", ge=0),
    to_revision: int = Query(default=0, alias="to", ge=0),
) -> ExhibitDiff:
    """What changed between two revisions; `to=0` is the head, `from=0` its parent.

    Between the *stored* specs, bindings and all (`exhibits.diff`): the store's views are not
    resolved, so a binding whose result has since been swept reads as unchanged here.
    """
    store = default_exhibit_store()
    after = await store.view(session_id, exhibit_id, to_revision)
    if after is None:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, to_revision))
    start = from_revision or after.parent_revision
    before = await store.view(session_id, exhibit_id, start) if start else None
    if before is None and start:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, start))
    # Revision 1 has no parent, so `before` is None and all of it reads as one addition. Off the
    # loop: a diff is CPU work proportional to the spec, and this route serves every chemist.
    return await asyncio.to_thread(
        diff_specs,
        None if before is None else before.spec,
        after.spec,
        from_revision=start,
        to_revision=after.revision,
    )


async def post_exhibit_revision(
    session_id: str,
    exhibit_id: ExhibitId,
    body: ExhibitRevisionIn,
    principal: CurrentUser,
    live: CurrentSession,
) -> ExhibitView:
    """A person's revision, refused with 409 when it was made against anything but the head."""
    store = default_exhibit_store()
    current = await store.view(session_id, exhibit_id)
    if current is None:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, 0))
    title = current.title if body.title is None else body.title
    spec = await _parsed(
        session_id, body.spec, title=title, change_note=body.change_note, parent=current.raw_spec
    )
    await _require_session_result(session_id, spec)
    try:
        view = await store.append(
            session_id,
            exhibit_id,
            spec=spec,
            parent_revision=body.parent_revision,
            author_kind="human",
            author=principal.oid,
            change_note=body.change_note,
            title=body.title,
            correlation_id=get_current_correlation_id() or "",
        )
    except StaleRevision as exc:
        raise HTTPException(
            status_code=409, detail={"code": "stale_revision", "head_revision": exc.head}
        ) from exc
    except UnknownExhibit as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidExhibit as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExhibitLimit as exc:
        raise HTTPException(
            status_code=409, detail={"code": "exhibit_limit", "message": str(exc)}
        ) from exc
    await _announce(view, "revised")
    return await resolved_view(view)


async def export_exhibit(
    session_id: str,
    exhibit_id: ExhibitId,
    fmt: str,
    live: CurrentSession,
    revision: int = 0,
) -> Response:
    """The artefact as a file; a format its kind does not offer is a 404, not a wrong file.

    So is a geometry whose cited calculation artifact has been evicted since it was written: the
    artefact still reads, and the file it pointed at is not there to give. A bound value is
    exported as what it resolves to; one whose result is gone is an empty cell. An html page is
    exported as `text/plain` (`exhibits.export.MEDIA_TYPES`): this server never answers
    `text/html` for an artefact.
    """
    view = await default_exhibit_store().view(session_id, exhibit_id, revision)
    if view is None:
        raise HTTPException(status_code=404, detail=_missing(exhibit_id, revision))
    body = await resolve_export((await resolved_view(view)).spec, fmt)
    if body is None:
        raise HTTPException(
            status_code=404, detail=f"a {view.kind} artefact has no {fmt!r} export here"
        )
    filename = export_filename(view.title, view.exhibit_id, view.revision, fmt)
    return Response(
        content=body,
        media_type=MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def list_my_exhibits(
    principal: CurrentUser, limit: int = Query(default=50, ge=1)
) -> ExhibitIndexOut:
    """Artefacts across every session the caller owns or is a member of, newest first.

    Empty under the in-memory session store, for `GET /sessions`' reason: there is no durable
    registry of who owns or joined a session to enumerate.
    """
    bounded = min(limit, settings.exhibit_max_listing)
    return ExhibitIndexOut(
        exhibits=await default_exhibit_store().listing_for(principal.oid, bounded)
    )


async def _parsed(
    session_id: str,
    raw: dict[str, Any],
    *,
    title: str,
    change_note: str,
    creating: bool = False,
    parent: Spec | None = None,
) -> Spec:
    """The spec to store, write-checked with its bindings resolved, or the 422 naming the fault.

    A person's spec may keep any binding whose result this session holds — one copied from
    `raw_spec` — or replace it with a literal; a ref outside the session's results is refused, so
    nobody can bind bytes the conversation never produced (`exhibits.bindings`). A binding carried
    unchanged from `parent`, the revision being revised, is kept even when retention has swept its
    result, so an expired cell does not block every other edit.
    """
    try:
        spec = parse_spec(raw)
        if creating:
            require_creatable(spec)
        bound = await bind_for_write(session_id, spec, parent=parent)
        require_writable(
            bound.resolved,
            title=title,
            change_note=change_note,
            stored=bound.stored,
            vanished=bound.vanished,
        )
        await require_source_stored(spec)
    except InvalidExhibit as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return bound.stored


async def _require_session_result(session_id: str, spec: Spec) -> None:
    """Refuse a pinned result this session cannot fetch: another session's bytes are not pinned."""
    if isinstance(spec, ResultSpec) and spec.result_ref not in await front_door.fetchable_refs(
        session_id
    ):
        raise HTTPException(
            status_code=422, detail="result_ref is not a stored tool result of this session"
        )


async def _announce(view: ExhibitView, op: Literal["created", "revised"]) -> None:
    """Log the person's write, and push it to the session's other open tabs — best effort.

    The push goes through the same mailbox a finished job does (`session_events`), claimed by
    `GET /sessions/{id}/events`. Best effort in both senses: the claim is at-most-once across
    every tab of the session, and a surface refetches the list on focus anyway, so a lost push costs
    a refresh. A mailbox that cannot be written is counted and does not fail the write that already
    committed. The in-memory session store has no mailbox, so nothing is pushed there.
    """
    log_event(
        logger,
        f"exhibit.{op}",
        "artefact %s revision %d %s by %s",
        view.exhibit_id,
        view.revision,
        op,
        view.author,
        actor=view.author,
        session=view.session_id,
        exhibit_id=view.exhibit_id,
        revision=view.revision,
    )
    if settings.session_store != "postgres":
        return
    event = ExhibitEvent(
        exhibit_id=view.exhibit_id,
        revision=view.revision,
        kind=view.kind,
        title=view.title,
        op=op,
        author_kind=view.author_kind,
        author=view.author,
    )
    try:
        await record_session_event(view.session_id, PUSH_KIND, event.model_dump(exclude={"type"}))
    except Exception:
        degraded(
            logger,
            "exhibits",
            "could not push artefact %s revision %d to session %s's other tabs",
            view.exhibit_id,
            view.revision,
            view.session_id,
        )


def _missing(exhibit_id: str, revision: int) -> str:
    """The one 404 sentence: an unknown id and another session's id read the same."""
    return f"no artefact {exhibit_id!r}" + (f" at revision {revision}" if revision else "")


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    With the app's own decorators rather than an `APIRouter`, for the reason
    `chemclaw/api/routes/sessions.py`'s `register` gives. `GET /exhibits` has no `{session_id}`
    segment and authorizes by listing only the caller's own sessions.
    """
    app.get("/exhibits")(list_my_exhibits)
    app.get("/sessions/{session_id}/exhibits")(list_exhibits)
    app.post("/sessions/{session_id}/exhibits", status_code=201)(create_exhibit_route)
    app.get("/sessions/{session_id}/exhibits/{exhibit_id}")(get_exhibit)
    app.get("/sessions/{session_id}/exhibits/{exhibit_id}/revisions")(list_revisions)
    app.post("/sessions/{session_id}/exhibits/{exhibit_id}/revisions", status_code=201)(
        post_exhibit_revision
    )
    app.get("/sessions/{session_id}/exhibits/{exhibit_id}/diff")(get_exhibit_diff)
    app.get("/sessions/{session_id}/exhibits/{exhibit_id}/export.{fmt}")(export_exhibit)
