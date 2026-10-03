"""What a `geometry` artefact's `source` names in the calculation artifact store, and its bytes.

**A geometry may cite a calculation rather than copy it.** The calc artifact store (D-124) keeps a
run's by-products content-addressed and eviction-managed, and an artefact naming one by its
`<calc_key>#<name>` reference shows the structure that calculation actually produced instead of a
transcription of it. So a write is refused unless the artifact is stored *then*
(`require_source_stored`) — the agent cannot cite a by-product it guessed — while a later read may
find it evicted, which the export route reports as a 404 rather than as a corrupt file
(`D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`).

The same lookup serves `GET /calc-artifacts/content`, which reads any stored by-product for any
authenticated caller: the calc cache is shared, not session-owned, so the artefact's own session
scope is not the gate on bytes it only points at.

`default_artifact_store` is imported by name so a test swaps it here, the seam
`science.calc.postgres_artifacts` documents for every importing module.
"""

from __future__ import annotations

from chemclaw.exhibits.models import GeometrySpec, InvalidExhibit, Spec
from chemclaw.science.calc.artifacts import ArtifactRef, find_link, split_ref
from chemclaw.science.calc.postgres_artifacts import default_artifact_store


async def find_calc_artifact(calc_key: str, name: str) -> ArtifactRef | None:
    """The stored artifact `(calc_key, name)`, or `None` when nothing is stored under it."""
    return await find_link(default_artifact_store(), calc_key, name)


async def calc_artifact_at(ref: str) -> ArtifactRef | None:
    """The stored artifact a flat `<calc_key>#<name>` reference names, or `None` — malformed too."""
    parts = split_ref(ref)
    return None if parts is None else await find_calc_artifact(*parts)


async def read_calc_artifact(ref: ArtifactRef) -> bytes | None:
    """The artifact's original bytes, or `None` when its blob was evicted after the listing."""
    return await default_artifact_store().open(ref.content_hash)


async def require_source_stored(spec: Spec) -> None:
    """Refuse a geometry whose `source` names no stored artifact; every other spec passes.

    Write-time only, like `models.require_writable` and beside it in every writer: a stored
    revision stays readable when its source is later evicted.

    Raises:
        InvalidExhibit: naming the reference, so the writer can list what the calculation kept.
    """
    if not isinstance(spec, GeometrySpec) or spec.source is None:
        return
    if await find_calc_artifact(spec.source.calc_key, spec.source.name) is None:
        raise InvalidExhibit(
            f"source {spec.source.as_ref()!r} is not a stored calculation artifact; name one "
            "list_artifacts returns, or give the structure inline as `xyz`"
        )


async def geometry_xyz(spec: GeometrySpec) -> str | None:
    """The geometry's XYZ text — inline, or the source's bytes — or `None` when the source is gone.

    A source whose bytes are not UTF-8 is `None` too: it is not an XYZ file whatever its name says,
    and handing it out as one would be a wrong file rather than a missing one.
    """
    if spec.xyz is not None:
        return spec.xyz
    if spec.source is None:  # pragma: no cover - the spec's validator requires one of the two
        return None
    ref = await find_calc_artifact(spec.source.calc_key, spec.source.name)
    data = None if ref is None else await read_calc_artifact(ref)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
