"""What a `geometry` artefact cites — a calculation artifact or a stored structure — and its XYZ.

**A geometry may cite rather than copy.** Two stores hold geometries this system computed, and an
artefact naming one shows the structure that calculation actually produced instead of a
transcription of it:

- **`structure_id`**, the structure store's content address (`science/calc/structures.py`). It is
  what the agent holds — every calculation result names its geometry by it, and none hands the
  model 3N coordinates — so it is what `create_exhibit` advertises. Resolved to XYZ when the
  artefact is *read* (`resolved_geometry`): the reader is served `xyz`, the stored revision keeps
  the address, and an address that no longer resolves reads as no `xyz` plus a `bindings`-style
  entry with `ok: false`, the way a swept binding reads.
- **`source`**, a calc artifact's `<calc_key>#<name>` (D-124), content-addressed and
  eviction-managed. Still valid, no longer advertised: the calc fleet stores no coordinate files, so
  the model has nothing to name.

**Either is checked when it is written** (`require_source_stored`) exactly as an inline block is:
it must exist *then* — the agent cannot cite something it guessed — and its XYZ must be **one
frame** within `exhibit_max_atoms` with every highlighted atom inside it. A conformer ensemble is a
stored `chemical/x-xyz` artifact too, and a viewer handed one shows its first frame as if it were
the whole (`D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`). A later read
may find either gone, which the export route reports as a 404 rather than as a corrupt file.

**Every artifact read is bounded by its recorded size first** (`calc_artifact_max_download_bytes`):
the store decompresses a whole blob into memory on `open`, so a size check after the read would
bound nothing. `GET /calc-artifacts/content` applies the same cap from the same field.

`default_artifact_store` and `default_structure_store` are imported by name so a test swaps them
here, the seam `science.calc.postgres_artifacts` documents for every importing module.
"""

from __future__ import annotations

import logging

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.result_handle import handles_resolve
from chemclaw.exhibits.evidence import evidence_params, evidence_predicate
from chemclaw.exhibits.models import (
    ExhibitBinding,
    ExhibitView,
    GeometrySource,
    GeometrySpec,
    InvalidExhibit,
    Spec,
    xyz_atom_count,
)
from chemclaw.science.calc.artifacts import ArtifactRef, find_link, split_ref
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.postgres_structures import default_structure_store

logger = logging.getLogger(__name__)

#: The media type the calc store records for a coordinate file (`artifacts.media_type_for`), and
#: the only kind of artifact a geometry may cite.
XYZ_MEDIA_TYPE = "chemical/x-xyz"


async def find_calc_artifact(calc_key: str, name: str) -> ArtifactRef | None:
    """The stored artifact `(calc_key, name)`, or `None` when nothing is stored under it."""
    return await find_link(default_artifact_store(), calc_key, name)


async def calc_artifact_at(ref: str) -> ArtifactRef | None:
    """The stored artifact a flat `<calc_key>#<name>` reference names, or `None` — malformed too."""
    parts = split_ref(ref)
    return None if parts is None else await find_calc_artifact(*parts)


def within_download_cap(ref: ArtifactRef) -> bool:
    """Whether `ref`'s recorded size is one this process may read into memory and hand out."""
    return ref.byte_size <= settings.calc_artifact_max_download_bytes


async def read_calc_artifact(ref: ArtifactRef) -> bytes | None:
    """The artifact's original bytes, or `None` when its blob was evicted after the listing.

    Callers decide `within_download_cap` first, from the recorded size: this read decompresses the
    whole blob.
    """
    return await default_artifact_store().open(ref.content_hash)


def structure_xyz(structure: Structure) -> str:
    """A stored structure as one standard XYZ block: count, a comment naming it, `El x y z` in Å."""
    lines = [str(len(structure.elements)), structure.structure_id]
    lines += [
        f"{symbol} {x:.4f} {y:.4f} {z:.4f}"
        for symbol, (x, y, z) in zip(structure.symbols, structure.positions, strict=True)
    ]
    return "\n".join(lines) + "\n"


async def require_source_stored(spec: Spec, session_id: str, *, parent: Spec | None = None) -> None:
    """Refuse a geometry citing something not stored, not one frame, or past a cap; else pass.

    Write-time only, like `models.require_writable` and beside it in every writer: a stored
    revision stays readable when what it cites is later evicted. An inline block is checked by the
    spec itself and passes here; every other kind of spec passes too.

    A `structure_id` must also have been **reported to this conversation** — named by one of its
    stored tool results that is evidence (`_reported_here`) — unless `parent`, the revision being
    revised, already cites it, in which case it is carried as a binding is
    (`D-2026-10-03-a-cited-structure-is-one-this-conversation-was-shown`).

    Raises:
        InvalidExhibit: naming the reference and what is wrong with it, so the writer can name a
            structure a result reported or list what the calculation kept.
    """
    if not isinstance(spec, GeometrySpec):
        return
    if spec.source is not None:
        cited, text = spec.source.as_ref(), await _source_text(spec.source)
    elif spec.structure_id is not None:
        carried = isinstance(parent, GeometrySpec) and parent.structure_id == spec.structure_id
        if not carried and not await _reported_here(session_id, spec.structure_id):
            raise InvalidExhibit(
                f"structure_id {spec.structure_id!r} is not one a tool result of this conversation "
                "reported; cite a structure a calculation here returned"
            )
        structure = await default_structure_store().get(spec.structure_id)
        if structure is None:
            raise InvalidExhibit(
                f"structure_id {spec.structure_id!r} names no stored structure; give one a "
                "calculation result reported"
            )
        cited, text = spec.structure_id, structure_xyz(structure)
    else:
        return
    try:
        atoms = xyz_atom_count(text)
    except ValueError as exc:
        raise InvalidExhibit(f"{cited!r} is not one XYZ structure: {exc}") from exc
    if atoms > settings.exhibit_max_atoms:
        raise InvalidExhibit(
            f"{cited!r} has {atoms} atoms, over the {settings.exhibit_max_atoms}-atom cap"
        )
    if outside := sorted({index for index in spec.highlight_atoms if index >= atoms}):
        raise InvalidExhibit(
            f"highlight_atoms {outside} are not atoms of {cited!r}, which has {atoms} (0-based)"
        )


# Does any evidence this session stored name the id? A substring over the stored bytes, newest
# links first so a structure the turn just computed is found in the first rows. Measured on
# Postgres 16 with the id absent (the worst case, every blob read): 4.5 ms for 50 results of 20 kB,
# 13 ms for 200 of 50 kB, 46 ms for 500 of 100 kB — once per geometry write.
_REPORTED_HERE = f"""
SELECT 1
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s AND {evidence_predicate("l.tool")} AND position(%s::bytea IN b.data) > 0
LIMIT 1
"""


async def _reported_here(session_id: str, structure_id: str) -> bool:
    """Whether a stored, evidence-bearing tool result of `session_id` names `structure_id`.

    Where this deployment keeps no session's results (`handles_resolve` false) there is nothing to
    ask, and the id resolves as the calc tools resolve one — globally — rather than being refused
    on every write.
    """
    if not handles_resolve():
        return True
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _REPORTED_HERE, (session_id, *evidence_params(), structure_id.encode("utf-8"))
            )
            return await cur.fetchone() is not None


async def _source_text(source: GeometrySource) -> str:
    """A cited calc artifact's text, refused unless it is a stored XYZ file within the cap."""
    found = await find_calc_artifact(source.calc_key, source.name)
    if found is None:
        raise InvalidExhibit(
            f"source {source.as_ref()!r} is not a stored calculation artifact; name one "
            "list_artifacts returns, or give the structure as `structure_id`"
        )
    if found.media_type != XYZ_MEDIA_TYPE:
        # A Hessian or a vibrational spectrum is a stored artifact too, and the geometry's export
        # would hand it out as an `.xyz` file a viewer cannot read. Its media type is the store's
        # own record of what the bytes are (`science/calc/artifacts.media_type_for`).
        raise InvalidExhibit(
            f"source {source.as_ref()!r} is a {found.media_type} artifact, not a geometry; "
            f"a geometry cites a {XYZ_MEDIA_TYPE} artifact holding one structure"
        )
    if not within_download_cap(found):
        raise InvalidExhibit(
            f"source {source.as_ref()!r} is {found.byte_size} bytes, over the "
            f"{settings.calc_artifact_max_download_bytes}-byte calc artifact download cap"
        )
    data = await read_calc_artifact(found)
    if data is None:
        raise InvalidExhibit(f"source {source.as_ref()!r} was evicted while it was being read")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidExhibit(f"source {source.as_ref()!r} is not text") from None


async def geometry_xyz(spec: GeometrySpec) -> str | None:
    """The geometry's XYZ text — inline, or what it cites — or `None` when that is not there.

    `None` for a citation that is gone, for an artifact now over the download cap (decided from its
    recorded size, never by reading it), and for bytes that are not UTF-8: none of those is an
    XYZ file to hand out, and handing out the wrong file is worse than a missing one.
    """
    if spec.xyz is not None:
        return spec.xyz
    if spec.structure_id is not None:
        structure = await default_structure_store().get(spec.structure_id)
        return None if structure is None else structure_xyz(structure)
    if spec.source is None:  # pragma: no cover - the spec's validator requires one of the three
        return None
    ref = await find_calc_artifact(spec.source.calc_key, spec.source.name)
    if ref is None or not within_download_cap(ref):
        return None
    data = await read_calc_artifact(ref)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def resolved_geometry(view: ExhibitView) -> ExhibitView:
    """`view` with a cited `structure_id` resolved: `spec.xyz` served, `raw_spec` as stored.

    A structure that no longer resolves — or a store that cannot be read — leaves `spec` as stored
    (no `xyz`) and lists one entry in `bindings`, `{path: "xyz", tool: "structure", pointer:
    <structure_id>, ok: false}`, the shape a swept binding reads in, so a reader has one place to
    find "this value's source is gone". Any other view is returned unchanged.
    """
    raw = view.raw_spec
    if not isinstance(raw, GeometrySpec) or raw.structure_id is None:
        return view
    try:
        structure = await default_structure_store().get(raw.structure_id)
        error = "" if structure is not None else "no structure is stored under this id any more"
    except Exception as exc:
        degraded(
            logger,
            "exhibits",
            "could not read structure %s for artefact %s: %s",
            raw.structure_id,
            view.exhibit_id,
            exc,
        )
        structure, error = None, "the structure store could not be read"
    if structure is None:
        missing = ExhibitBinding(
            path="xyz",
            result_ref="",
            tool="structure",
            pointer=raw.structure_id,
            ok=False,
            error=error,
        )
        return view.model_copy(update={"spec": raw, "bindings": [*view.bindings, missing]})
    shown = raw.model_copy(update={"xyz": structure_xyz(structure), "structure_id": None})
    return view.model_copy(update={"spec": shown})


def spec_for_model(view: ExhibitView) -> Spec:
    """The spec a model is shown of a resolved `view`: a cited structure as its address.

    A geometry citing a `structure_id` is resolved to XYZ for a reader (`resolved_geometry`), and
    its coordinates are a viewer's input — about fifty characters an atom of nothing the model can
    act on, and an invitation to transcribe them back as an inline block. So `read_exhibit` and the
    turn note that carries a referenced artefact both show the address the model holds and writes
    back; every other spec is shown resolved.
    """
    raw = view.raw_spec
    if isinstance(raw, GeometrySpec) and raw.structure_id is not None:
        return raw
    return view.spec
