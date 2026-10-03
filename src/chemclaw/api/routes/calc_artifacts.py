"""A stored calculation by-product as a download — the bytes behind a geometry artefact's `source`.

**Any authenticated caller, not session-scoped**, the position `GET /notes/{id}` and `GET /jobs`
take and for the same reason: the calculation cache is shared — one Hessian serves every session
that asks about that molecule — and an artifact has no owner to scope it to. The artefact that
*cites* one is session-scoped; the cited bytes are the organisation's cache
(`D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`). What the gate buys is
that a caller exists and is inside the per-principal rate budget.

**Bounded before it is read.** The store decompresses a whole blob into memory on `open`, so an
artifact larger than `calc_artifact_max_download_bytes` is refused with 413 from its recorded size,
never read first.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Response

from chemclaw.api.deps import CurrentUser
from chemclaw.core.config import settings
from chemclaw.exhibits.export import safe_filename
from chemclaw.exhibits.sources import calc_artifact_at, read_calc_artifact, within_download_cap


async def get_calc_artifact_content(
    principal: CurrentUser,
    ref: str = Query(min_length=1, description="`<calc_key>#<name>`, URL-encoded"),
) -> Response:
    """The artifact's bytes with its stored media type, as an attachment named by its role.

    404 for a reference that is malformed, names nothing, or names a blob evicted between the
    listing and the read — all three are "there is no file here"; 413 above the download cap.
    """
    found = await calc_artifact_at(ref)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no stored calculation artifact {ref!r}")
    if not within_download_cap(found):
        cap = settings.calc_artifact_max_download_bytes
        raise HTTPException(
            status_code=413,
            detail=f"{ref!r} is {found.byte_size} bytes, over the {cap}-byte download cap",
        )
    data = await read_calc_artifact(found)
    if data is None:
        raise HTTPException(status_code=404, detail=f"{ref!r} is no longer stored")
    filename = safe_filename(found.name, "artifact")
    return Response(
        content=data,
        media_type=found.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    With the app's own decorators rather than an `APIRouter`, for the reason
    `chemclaw/api/routes/sessions.py`'s `register` gives.
    """
    app.get("/calc-artifacts/content")(get_calc_artifact_content)
