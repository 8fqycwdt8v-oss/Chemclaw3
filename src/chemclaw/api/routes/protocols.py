"""Reading and editing experiment designs over HTTP — the surface an expert tailors them on.

A protocol this system drafts is almost always altered before it is run, and until now there was
nowhere for that to happen: the draft lived in a transcript, so "change the temperature to 60" meant
another turn and another whole draft, and what the chemist actually changed was unrecoverable
afterwards. These routes make the design a document with an address.

**A human edit is a REST write, not a tool call composed by a click.** That distinction is the one
`Chemclaw3_ui`'s own rule states (`docs/chemistry-aware-frontend.md` §9): everything the *agent*
does reaches it as a chat turn, and a button that composed a tool call would be a surface deciding
what the agent does. Editing a document is not that — it is the chemist authoring a revision, which
is exactly what `POST /proposals/{id}/decision` already is for a note. So the write lands here,
`author_kind` records that a person made it, and the agent is not involved.

**The conflict is a 409 and it is bound to a revision**, the same shape `POST
/sessions/{id}/plan/decision` uses for `plan_hash`: a caller says which revision they edited, and a
write derived from anything but the head is refused rather than allowed to discard the revision it
did not see. That is not a nicety — two chemists editing one plate is the ordinary case, and a last
-write-wins store would lose one of them silently.

`CurrentUser`-gated and deliberately not owner-scoped, the position `GET /jobs` and `GET /notes`
already take: a design is a piece of shared laboratory work, and a chemist who did not open it is
exactly who needs to read it before running it.
"""

from datetime import datetime

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.api.auth import Principal
from chemclaw.api.deps import CurrentUser, _is_reviewer, _owner_authorizes
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.diff import DesignDiff, diff_designs
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
    """A page of designs."""

    designs: list[DesignSummary] = Field(default_factory=list)

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
    # Who approved, ran or abandoned this design and at which revision. Beside the document rather
    # than behind a route of its own, for the reason `history` is: a reader deciding whether to run
    # a protocol needs "revision 3 was approved by X" at the same instant as the revision they are
    # looking at, and a design demoted back to `draft` by a later revision has that fact *only*
    # here.
    status_history: list[StatusEvent] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class RevisionIn(BaseModel):
    """A human's edit: the whole edited document, what it was derived from, and why."""

    document: ExperimentDesign
    # The revision the editor had open. Not optional and not defaulted to the head: an edit that
    # did not say what it was derived from is precisely the write that silently discards somebody
    # else's, and accepting one "for convenience" would remove the control this field is.
    parent_revision: int = Field(ge=1)
    change_note: str = Field(min_length=1, max_length=2000)

    model_config = ConfigDict(extra="forbid")


class RevisionOut(BaseModel):
    """What a stored edit hands back."""

    design_id: str
    revision: int
    checks: list[ProtocolCheck] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusIn(BaseModel):
    """A lifecycle move."""

    status: DesignStatus
    # The revision the person was looking at when they decided. Required and not defaulted to the
    # head for exactly `RevisionIn.parent_revision`'s reason, one field along: an approval that did
    # not say what it approved is the one that gets attributed to a document nobody read. A
    # colleague saving while a chemist thinks is the ordinary case, not the exotic one.
    expected_revision: int = Field(ge=1)
    # **Recorded, which it was not.** `Chemclaw3_ui`'s status panel labels this "recorded with the
    # move", disables every button until it is filled in, and confirms "the move is recorded
    # against you with the reason you wrote" — and `set_status` took no `reason` at all, so the one
    # sentence anybody writes about a design ("abandoned — the SM decomposes above 40 °C") was
    # accepted from the chemist, validated to 2,000 characters, and dropped on the way to a 204.
    reason: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid")


async def list_protocols(
    principal: CurrentUser,
    status: str = "",
    project: str = "",
    limit: int = 50,
) -> DesignListOut:
    """Designs, newest first.

    A list route, so an empty result is an empty list rather than a 404 — the same policy the
    client's own `orEmpty()` expects of every listing here.
    """
    known = {"requested", "draft", "approved", "executed", "abandoned"}
    if status and status not in known:
        raise HTTPException(status_code=422, detail=f"unknown status {status!r}")
    designs = await default_design_store().listing(
        status=status or None,  # type: ignore[arg-type]
        project=project,
        limit=max(1, min(limit, 200)),
    )
    return DesignListOut(designs=designs)


async def get_protocol(
    design_id: str,
    principal: CurrentUser,
    revision: int = 0,
) -> DesignOut:
    """One revision — the head by default — with the whole revision history beside it.

    The history comes back in the same call rather than behind a second route because every
    consumer needs both: a document view renders the revision and its lineage together, and asking
    for them separately makes the two answers race whenever somebody else is editing.
    """
    # **One call, because four were four transactions and they tore.** The paragraph above says
    # asking for these separately makes the answers race; the store then answered each from its own
    # connection, and a read concurrent with one `append` was internally inconsistent in 92 to 100
    # of every 100 — revision 1's document under a header saying head revision 2, with revision 2
    # in the history beside it.
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

    **Nothing gated either write before this.** Both routes took `CurrentUser` and nothing else, so
    any authenticated principal — with no role at all — could sign off on and rewrite another
    chemist's design: measured, an unrelated principal wrote `executed` into the status trail of a
    design opened by somebody else, then landed a revision on it as its author. A lab record saying
    an experiment was run is the most consequential thing this table holds.

    Owner **or** reviewer, rather than the reviewer-only rule `POST /proposals/{id}/decision` uses.
    That route's subject is machine-written knowledge entering a shared graph, where the whole point
    is that the author does not decide. A design is a chemist's own experiment: they approve their
    own plate, and a reviewer reaches other people's for the same reason `_is_reviewer` exists.

    Reads stay open on purpose. A design is a shared scientific artifact — the schema says so where
    it keeps `opened_by` through offboarding, and `find_experiment_protocols` lists the deployment's
    designs — so this refuses with 403 rather than the 404 the session routes use: the id's
    existence is not the secret, the right to change it is.
    """
    header = await default_design_store().summary(design_id)
    if header is None:
        raise HTTPException(status_code=404, detail=f"no design {design_id!r}")
    if _owner_authorizes(header.opened_by, principal) or _is_reviewer(principal):
        return
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

    **The checks are re-run here rather than trusted from the caller**, and that is the point of
    computing them in code at all: an edit that breaks the charge table has to say so with the same
    verdict the draft got, or the two halves of the surface would grade the same document
    differently depending on who wrote it.

    A blocking check does **not** refuse a human edit, which is the one place this differs from
    `draft_experiment_protocol`. A chemist editing towards a working protocol passes through
    invalid intermediate states — half a charge table is not a reason to lose their work — and they
    can see the verdict. A model cannot, so its draft is refused.
    """
    await _require_writable(design_id, principal)
    store = default_design_store()
    previous = await store.read(design_id, body.parent_revision)
    if previous is None:
        raise HTTPException(
            status_code=404, detail=f"no design {design_id!r} at revision {body.parent_revision}"
        )
    # **The stage is derived from the document, not assumed.** This route serves the ADR's second
    # hole — an artefact the chemist can correct *before* the expensive work — and the first version
    # graded at the protocol stage unconditionally, so correcting an ask reported `is_a_protocol`
    # and `evidence_present` as blockers on a design with no procedure in it. That is exactly the
    # failure `_REQUEST_STAGE` was introduced to prevent, reintroduced on the human path: a blocker
    # that fires on the normal path is a blocker a reader learns to ignore, which is the property
    # the one real blocker depends on. The revision's `kind` is the same question and is no longer
    # any caller's to answer — `store.revision_kind` derives it from `has_protocol` there.
    checks = run_checks(
        body.document, stage="protocol" if body.document.has_protocol else "request"
    )
    changed = diff_designs(
        previous.design,
        body.document,
        from_revision=body.parent_revision,
        to_revision=body.parent_revision + 1,
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
        # 422, because it is the document that is wrong and the caller can fix it — not a 500,
        # which is what a NUL byte anywhere in a browser-supplied design used to produce, out of
        # psycopg, with a correlation id and nothing actionable in it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RevisionConflict as exc:
        # 409 with a machine-readable code, because the caller's next move is to re-read and
        # re-apply rather than to retry. Deliberately *not* the shape `POST
        # /sessions/{id}/plan/decision` uses — that one answers a 409 with a plain string, and a
        # caller has to match on prose to tell it from the other 409 the front door serves. This
        # comment used to claim it mirrored a `plan_changed` code, which exists nowhere in `src/`.
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
    return diff_designs(
        before.design,
        after.design,
        from_revision=before.revision,
        to_revision=after.revision,
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
            actor=principal.oid or "",
            reason=body.reason,
        )
    except UnknownDesign as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RevisionConflict as exc:
        # The same 409 and the same machine-readable code the revision route answers with, because
        # the caller's next move is the same one: re-read the design and decide again. A sign-off is
        # a write against a revision exactly as an edit is.
        raise HTTPException(
            status_code=409, detail={"code": "revision_conflict", "message": str(exc)}
        ) from exc
    except ChemclawError as exc:
        # No `pragma: no cover` and no claim that this cannot happen: the comment here said "the
        # store raises only the two above today", and `set_status` runs `require_storable(...,
        # reason=...)` over a reason a browser collects from a chemist — so a NUL, a C0 character
        # or an unpaired surrogate in it reaches exactly this clause, correctly, as a 422. The
        # pragma also suppressed coverage on a reachable branch, which is how the belief survived.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(status_code=204)


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`, for the
    reason every other route module states: since FastAPI 0.139 `include_router` is lazy, so the
    routes would be invisible to everything that walks the route table by type — including
    `tests/test_route_auth_coverage.py`, which is what proves each of these takes `CurrentUser`.
    """
    app.get("/protocols")(list_protocols)
    app.get("/protocols/{design_id}")(get_protocol)
    app.post("/protocols/{design_id}/revisions")(post_revision)
    app.get("/protocols/{design_id}/diff")(get_protocol_diff)
    app.post("/protocols/{design_id}/status", status_code=204)(post_status)
