"""Reading and editing experiment designs over HTTP — the surface an expert tailors them on.

A human edit is a REST write recorded with `author_kind` of a person, not a tool call: the agent is
not involved. Writes are bound to a revision, so an edit derived from anything but the head is a 409
rather than a silent last-write-wins. Reads are `CurrentUser`-gated and not owner-scoped: a design
is shared laboratory work.
"""

import asyncio
from datetime import datetime

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.agent.protocol_design_tools import recorded_failures, uncited_precedent
from chemclaw.api.auth import Principal
from chemclaw.api.deps import (
    DESIGN,
    CurrentUser,
    _is_reviewer,
    _owner_authorizes,
    record_refusal,
)
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.diff import DesignDiff, diff_designs
from chemclaw.protocols.export import run_sheet_csv, run_sheet_filename
from chemclaw.protocols.models import (
    AuthorKind,
    DesignStatus,
    DesignSummary,
    ExperimentDesign,
    ProtocolCheck,
    StatusEvent,
)
from chemclaw.protocols.store import (
    RevisionConflict,
    StatusConflict,
    UnknownDesign,
    UnstorableDocument,
    default_design_store,
)


class RevisionSummary(BaseModel):
    """One entry of the history list — enough to choose a revision to open."""

    revision: int
    kind: str
    author_kind: AuthorKind
    author: str = ""
    change_note: str = ""
    created_at: datetime
    blockers: int = 0

    model_config = ConfigDict(frozen=True, extra="forbid")


class DesignListOut(BaseModel):
    """A page of designs, **and how many designs that page is a page of**.

    `designs` was the whole of this, and the route bounded it — default 50, clamped to 200 — so a
    site with more designs than the page rendered the newest 50 as the corpus. `GET /sessions` in
    this same package was fixed for exactly that ("it always bounded the answer, and nothing said
    so"); the sibling listing route was not.

    A `total` and a marker rather than the keyset cursor `GET /sessions` grew: see `list_protocols`
    for why the cursor is a separate decision and what it would need.
    """

    designs: list[DesignSummary] = Field(default_factory=list)
    # Everything matching the same filters, before the page bound.
    total: int = 0
    # Whether matching designs exist that this page does not carry.
    truncated: bool = False

    model_config = ConfigDict(frozen=True, extra="forbid")


class DesignOut(BaseModel):
    """One revision, plus every revision's headline so the history is one round trip."""

    design_id: str
    summary: DesignSummary | None = None
    revision: int
    kind: str
    author_kind: AuthorKind
    author: str = ""
    change_note: str = ""
    created_at: datetime
    design: ExperimentDesign
    checks: list[ProtocolCheck] = Field(default_factory=list)
    history: list[RevisionSummary] = Field(default_factory=list)
    # Who approved, ran or abandoned this design and at which revision, returned beside the document
    # so a reader sees it with the revision they are looking at.
    status_history: list[StatusEvent] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class RevisionIn(BaseModel):
    """A human's edit: the whole edited document, what it was derived from, and why."""

    document: ExperimentDesign
    # The revision the editor had open. Required: an edit that does not name its parent is the write
    # that silently discards somebody else's.
    parent_revision: int = Field(ge=1)
    change_note: str = Field(min_length=1, max_length=2000)

    model_config = ConfigDict(extra="forbid")


class RevisionOut(BaseModel):
    """What a stored edit hands back.

    `design_id` echoes the path parameter the caller sent, deliberately: this body is what a client
    stores or logs against a saved revision, and a response that names only `revision: 4` cannot say
    which document it is the fourth of once it is separated from its request URL.
    """

    design_id: str
    revision: int
    checks: list[ProtocolCheck] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusIn(BaseModel):
    """A lifecycle move."""

    status: DesignStatus
    # The revision the person was looking at when they decided; required for the same reason as
    # `RevisionIn.parent_revision`.
    expected_revision: int = Field(ge=1)
    # The status the person saw beside that revision. `expected_revision` guards the document, not
    # the decision; this is the compare-and-set that stops two concurrent sign-offs both succeeding.
    expected_status: DesignStatus
    # Recorded with the status move.
    reason: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid")


async def list_protocols(
    principal: CurrentUser,
    status: str = "",
    project: str = "",
    limit: int = 50,
) -> DesignListOut:
    """One page of designs, newest first, with how many matched the same filters.

    An empty result is an empty list, not a 404. `total` and `truncated` say the answer is a page; a
    keyset cursor is not offered here.
    """
    known = {"requested", "draft", "approved", "executed", "abandoned"}
    if status and status not in known:
        raise HTTPException(status_code=422, detail=f"unknown status {status!r}")
    index = await default_design_store().listing(
        status=status or None,  # type: ignore[arg-type]
        project=project,
        limit=max(1, min(limit, 200)),
    )
    return DesignListOut(designs=index.designs, total=index.total, truncated=index.truncated)


async def get_protocol(
    design_id: str,
    principal: CurrentUser,
    revision: int = 0,
) -> DesignOut:
    """One revision — the head by default — with the whole revision history beside it.

    One call, because consumers need both and separate reads race with concurrent edits.
    """
    # One store call, one transaction: separate reads could pair a revision with another head's
    # history.
    page = await default_design_store().page(design_id, revision or None)
    if page is None:
        raise HTTPException(
            status_code=404,
            detail=f"no design {design_id!r}" + (f" at revision {revision}" if revision else ""),
        )
    stored, history = page.revision, page.history
    return DesignOut(
        design_id=design_id,
        summary=page.summary,
        revision=stored.revision,
        kind=stored.kind,
        author_kind=stored.author_kind,
        author=stored.author,
        change_note=stored.change_note,
        created_at=stored.created_at,
        design=stored.design,
        checks=stored.checks,
        status_history=page.status_history,
        history=[
            RevisionSummary(
                revision=item.revision,
                kind=item.kind,
                author_kind=item.author_kind,
                author=item.author,
                change_note=item.change_note,
                created_at=item.created_at,
                blockers=len(item.blockers),
            )
            for item in history
        ],
    )


async def _require_writable(design_id: str, principal: Principal) -> None:
    """403 unless this caller may write to this design — its owner, or a reviewer.

    A design is a chemist's own experiment: they sign off their own, and a reviewer reaches other
    people's. Reads stay open, so this answers 403 rather than 404: the design's existence is not
    the secret, the right to change it is.
    """
    header = await default_design_store().summary(design_id)
    if header is None:
        record_refusal(DESIGN, "unknown design", principal, design_id, status=404)
        raise HTTPException(status_code=404, detail=f"no design {design_id!r}")
    if _owner_authorizes(header.opened_by, principal) or _is_reviewer(principal):
        return
    # Record the refusal: who tried to write to whose design is only knowable server-side.
    record_refusal(DESIGN, "another chemist's design", principal, design_id, status=403)
    raise HTTPException(
        status_code=403,
        detail=f"{design_id} was opened by another chemist; writing to it needs a review role",
    )


async def post_revision(
    design_id: str,
    body: RevisionIn,
    principal: CurrentUser,
) -> RevisionOut:
    """Store a chemist's edit as a new revision.

    The checks are re-run here, so a human edit is graded exactly as an agent draft would be. Unlike
    `draft_experiment_protocol`, a blocking check does not refuse a human edit: a chemist passes
    through invalid intermediate states and can see the verdict; a model cannot.
    """
    await _require_writable(design_id, principal)
    store = default_design_store()
    previous = await store.read(design_id, body.parent_revision)
    if previous is None:
        raise HTTPException(
            status_code=404, detail=f"no design {design_id!r} at revision {body.parent_revision}"
        )
    # The stage is derived from the document, so a request without a procedure is not graded as a
    # protocol. The corpus evidence is passed in: a check handed none passes by construction, and a
    # human edit must get the same verdict as the agent's draft. Both lookups answer `[]` rather
    # than raise, so an unreachable corpus costs a quieter check, not a lost edit. Awaited inline;
    # only `recorded_failures` offloads to a thread, and the remaining loop time is a few
    # milliseconds.
    checks = run_checks(
        body.document,
        stage="protocol" if body.document.has_protocol else "request",
        failures=await recorded_failures(body.document),
        precedent=await uncited_precedent(body.document),
    )
    # In a thread: diffing two large revisions can take seconds, and the single worker's event loop
    # serves every SSE stream and the kubelet probes. `run_checks` above is cheap and stays inline.
    changed = (
        await asyncio.to_thread(
            diff_designs,
            previous.design,
            body.document,
            from_revision=body.parent_revision,
            to_revision=body.parent_revision + 1,
        )
    ).paths
    try:
        revision = await store.append(
            design_id,
            body.document,
            checks,
            author_kind="human",
            author=principal.oid or "",
            parent_revision=body.parent_revision,
            change_note=body.change_note,
        )
    except UnstorableDocument as exc:
        # 422: the document is wrong (e.g. a NUL byte) and the caller can fix it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RevisionConflict as exc:
        # 409 with a machine-readable code: the caller's next move is to re-read and re-apply.
        raise HTTPException(
            status_code=409, detail={"code": "revision_conflict", "message": str(exc)}
        ) from exc
    return RevisionOut(
        design_id=design_id,
        revision=revision.revision,
        checks=checks,
        changed_paths=changed,
    )


async def get_protocol_diff(
    design_id: str,
    principal: CurrentUser,
    from_revision: int = 1,
    to_revision: int = 0,
) -> DesignDiff:
    """What changed between two revisions of one design."""
    store = default_design_store()
    before = await store.read(design_id, from_revision)
    after = await store.read(design_id, to_revision or None)
    if before is None or after is None:
        raise HTTPException(status_code=404, detail=f"no such revision of {design_id!r}")
    # In a thread for the reason `post_revision` gives; reads are not rate-limited.
    return await asyncio.to_thread(
        diff_designs,
        before.design,
        after.design,
        from_revision=before.revision,
        to_revision=after.revision,
    )


async def get_run_sheet(
    design_id: str,
    principal: CurrentUser,
    revision: int = 0,
) -> Response:
    """The design's arms as a CSV run sheet — one row per arm, in run order.

    Not JSON: its consumers (instrument software, a LIMS import, a workbook) read files. Gated like
    `GET /protocols/{design_id}`, of which it is a projection.

    Args:
        design_id: The `design-…` id.
        principal: The authenticated caller.
        revision: A specific revision, or 0 for the head.

    Returns:
        `text/csv` with a `Content-Disposition` named by `protocols.export.run_sheet_filename`.
    """
    stored = await default_design_store().read(design_id, revision or None)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail=f"no design {design_id!r}" + (f" at revision {revision}" if revision else ""),
        )
    # In a thread: this walks every arm and factor of a document of up to 1536 arms.
    body = await asyncio.to_thread(run_sheet_csv, stored.design)
    return Response(
        content=body,
        # Explicit UTF-8: names are routinely non-ASCII and RFC 4180 defaults to US-ASCII.
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{run_sheet_filename(design_id, stored.revision)}"'
            )
        },
    )


async def post_status(
    design_id: str,
    body: StatusIn,
    principal: CurrentUser,
) -> Response:
    """Move a design's lifecycle status — approve it, mark it run, or abandon it."""
    await _require_writable(design_id, principal)
    try:
        await default_design_store().set_status(
            design_id,
            body.status,
            expected_revision=body.expected_revision,
            expected_status=body.expected_status,
            actor=principal.oid or "",
            reason=body.reason,
        )
    except UnknownDesign as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RevisionConflict as exc:
        # The same 409 and code as the revision route: a sign-off is a write against a revision.
        raise HTTPException(
            status_code=409, detail={"code": "revision_conflict", "message": str(exc)}
        ) from exc
    except StatusConflict as exc:
        # A separate `code`, so the caller can tell a document edit (`revision_conflict`) from
        # somebody else's decision (`status_conflict`).
        raise HTTPException(
            status_code=409, detail={"code": "status_conflict", "message": str(exc)}
        ) from exc
    except ChemclawError as exc:
        # Reachable: `set_status` validates a browser-supplied reason with `require_storable`, so a
        # NUL, C0 character or unpaired surrogate lands here as a 422.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, so tests that walk the route
    table see them.
    """
    app.get("/protocols")(list_protocols)
    app.get("/protocols/{design_id}")(get_protocol)
    app.post("/protocols/{design_id}/revisions")(post_revision)
    app.get("/protocols/{design_id}/diff")(get_protocol_diff)
    app.get("/protocols/{design_id}/run-sheet.csv")(get_run_sheet)
    app.post("/protocols/{design_id}/status", status_code=204)(post_status)
