"""A stored calculation by-product as a download — the bytes behind a geometry artefact's `source`.

Any authenticated caller, not session-scoped: the calculation cache is shared across sessions and an
artifact has no owner (the artefact citing it is session-scoped). An artifact over
`calc_artifact_max_download_bytes` is refused with 413 from its recorded size, before the store
decompresses it into memory.
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

    404 for a malformed reference, one naming nothing, or an evicted blob; 413 above the download
    cap.
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

    App decorators, not an `APIRouter`; see `chemclaw/api/routes/jobs.py`'s `register`.
    """
    app.get("/calc-artifacts/content")(get_calc_artifact_content)
