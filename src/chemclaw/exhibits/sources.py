"""What a `geometry` artefact cites — a calculation artifact or a stored structure — and its XYZ.

A geometry may cite rather than copy, so it shows what the calculation actually produced:

- **`structure_id`**, the structure store's content address (`science/calc/structures.py`) and
  what the agent holds. Resolved to XYZ on read (`resolved_geometry`); one that no longer
  resolves reads as no `xyz` plus an `ok: false` entry, like a swept binding.
- **`source`**, a calc artifact's `<calc_key>#<name>`. Still valid, no longer advertised.

Either is checked at write time (`require_source_stored`): it must exist, be one frame within
`exhibit_max_atoms`, and contain every highlighted atom. Artifact reads are bounded by their
recorded size first (`calc_artifact_max_download_bytes`), because the store decompresses a whole
blob on `open`. The default stores are imported by name so tests can swap them.
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

    Callers check `within_download_cap` from the recorded size first: this decompresses the whole
    blob.
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

    Write-time only, beside `models.require_writable`; a stored revision stays readable if what it
    cites is later evicted. Inline blocks and other kinds pass. A `structure_id` must also have been
    reported to this conversation by an evidence-bearing tool result (`_reported_here`), unless the
    parent revision already cites it.

    Raises:
        InvalidExhibit: naming the reference and what is wrong with it.
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
        atoms = xyz_atom_count(text, max_atoms=settings.exhibit_max_atoms)
    except ValueError as exc:
        raise InvalidExhibit(f"{cited!r} is not one XYZ structure within the caps: {exc}") from exc
    if outside := sorted({index for index in spec.highlight_atoms if index >= atoms}):
        raise InvalidExhibit(
            f"highlight_atoms {outside} are not atoms of {cited!r}, which has {atoms} (0-based)"
        )


# Does any evidence this session stored name the id? A substring scan over stored bytes, newest
# links first; cheap enough to run once per geometry write.
_REPORTED_HERE = f"""
SELECT 1
FROM tool_result_links l
JOIN tool_result_blobs b ON b.content_hash = l.content_hash
WHERE l.session_id = %s AND {evidence_predicate("l.tool")} AND position(%s::bytea IN b.data) > 0
LIMIT 1
"""


async def _reported_here(session_id: str, structure_id: str) -> bool:
    """Whether a stored, evidence-bearing tool result of `session_id` names `structure_id`.

    Where the deployment keeps no session results (`handles_resolve` false), the id resolves
    globally, as the calc tools resolve one.
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
        # Only XYZ artifacts: a Hessian or spectrum is stored too but is not a geometry. The media
        # type is the store's own record (`science/calc/artifacts.media_type_for`).
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

    `None` for a missing citation, an artifact over the download cap (by recorded size, never read),
    or non-UTF-8 bytes: none of those is an XYZ file to hand out.
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

    An unresolvable structure (or unreadable store) leaves `spec` without `xyz` and adds a
    `bindings` entry `{path: "xyz", tool: "structure", pointer: <structure_id>, ok: false}`. Other
    views are returned unchanged.
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

    Coordinates are viewer input the model cannot act on and might transcribe back, so
    `read_exhibit` and the turn note show the `structure_id`; every other spec is shown resolved.
    """
    raw = view.raw_spec
    if isinstance(raw, GeometrySpec) and raw.structure_id is not None:
        return raw
    return view.spec
