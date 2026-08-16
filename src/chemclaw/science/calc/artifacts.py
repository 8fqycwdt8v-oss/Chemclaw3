"""Content-addressed store for a calculation's by-products (D-124).

The calculation cache (`science/calc/store.py`) persists the *answer* — a small JSON payload.
Everything else a run produced was deleted with its temporary directory: the xtb driver executed
the binary inside a `tempfile.TemporaryDirectory`, parsed the Hessian and `vibspectrum` into
numbers, and lost the files. (That driver is `Chemclaw3-mcp`'s now,
`D-2026-08-16-the-physics-leaves-the-cache-stays`; the argument for keeping the bytes is unchanged,
and so is this store.) On a drug-sized substrate that Hessian costs minutes, and it is
exactly the input that makes the *next* question cheap — thermochemistry at a second temperature, IR
at a different broadening, a transition-state search seeded from a known curvature.

This module keeps those bytes. Two ideas, each doing one job:

- **Content addressing.** A blob is named by the SHA-256 of its uncompressed bytes, so two
  calculations that produced an identical geometry store one copy. The hash is also the read
  path: `open(content_hash)` is the whole retrieval API.
- **A named link from the calculation.** `(calc_key, name)` says *which run* produced the blob and
  *what role* it played (`hessian`, `xtbopt.xyz`). A future DFT wavefunction or restart file is
  another `(calc_key, name)` row over the same blob table — nothing here is xTB-specific.

**An artifact is optional by construction.** `put` returns `None` rather than raising when the
payload exceeds `artifact_max_bytes` or the store is disabled, and every caller ignores the
return. Capturing a by-product must never be able to fail the calculation it is a by-product of.

The shape deliberately mirrors `science/calc/store.py`: a `Protocol` with an in-memory backend
for tests and a Postgres backend for real (`science/calc/postgres_artifacts.py`), plus a
`default_artifact_store()` seam that tests monkeypatch at the importing module.
"""

import base64
import hashlib
import logging
import zlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    ResultStore,
    StoredResult,
)

logger = logging.getLogger(__name__)

# How a blob's bytes are stored. `zlib` is the stdlib deflate codec — Python 3.11 has no stdlib
# zstd, and pulling a dependency in for the remaining ~15% on already-compressible text is not a
# trade this codebase makes. The codec is recorded per row, so adding one later is a new value,
# never a migration.
Codec = str


def content_address(data: bytes) -> str:
    """The SHA-256 hex digest of `data` — an artifact's content address.

    Over the *uncompressed* bytes deliberately: the address must not change when the compression
    level changes or a row is rewritten under a different codec. Raw bytes, so
    `chemclaw.core.ids.stable_hash` (which hashes a JSON rendering, for calculation *identity*) is
    the
    wrong tool here.
    """
    return hashlib.sha256(data).hexdigest()


def encode(data: bytes) -> tuple[Codec, bytes]:
    """Compress `data` for storage; return the codec that was used and the payload.

    Returns `("none", data)` when compression is disabled or does not actually shrink the payload
    — an already-compressed or high-entropy artifact would otherwise be stored *larger* than it
    arrived, plus a decompression cost on every read. Text artifacts (a Turbomole `hessian`, an
    `xyz` ensemble) compress several-fold, which is the case this exists for.
    """
    level = settings.artifact_compression_level
    if level <= 0:
        return "none", data
    packed = zlib.compress(data, level)
    return ("zlib", packed) if len(packed) < len(data) else ("none", data)


def decode(codec: Codec, payload: bytes) -> bytes:
    """Return the original bytes of a payload stored under `codec`.

    An unknown codec raises rather than returning the payload as-is: handing a caller deflate
    bytes labelled as a Hessian would surface much later as an unparseable file, and the cause
    would be invisible.
    """
    if codec == "none":
        return payload
    if codec == "zlib":
        return zlib.decompress(payload)
    raise ValueError(f"unknown artifact codec {codec!r}")


class ArtifactRef(BaseModel):
    """A stored by-product: which calculation produced it, its role, and its content address.

    Frozen because it is a value object handed back from the store and held by callers; nothing
    reassigns a ref's fields after it is built.
    """

    model_config = {"frozen": True}

    # `CalculationKey.as_str()` of the run that produced it.
    calc_key: str = Field(min_length=1)
    # The artifact's role — the producer's filename (`hessian`, `xtbopt.xyz`, `vibspectrum`).
    name: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    media_type: str = "application/octet-stream"

    def as_str(self) -> str:
        """Flat string form, for a knowledge-graph note to cite a specific artifact."""
        return f"{self.calc_key}#{self.name}"


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence contract for calculation by-products. Backends implement this."""

    async def put(
        self,
        calc_key: str,
        name: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        compute_seconds: float | None = None,
    ) -> ArtifactRef | None:
        """Store `data` under `(calc_key, name)`; return its ref, or `None` if it was not stored.

        `compute_seconds` is the wall time of the calculation that produced it — the cost of *not*
        having it, which is what the eviction sweep orders by.
        """
        ...

    async def open(self, content_hash: str) -> bytes | None:
        """Return the artifact's original bytes, or `None` if it is not stored."""
        ...

    async def list_for(self, calc_key: str) -> list[ArtifactRef]:
        """Return every artifact this calculation produced, ordered by name."""
        ...


def too_large(byte_size: int) -> bool:
    """Whether `byte_size` exceeds the per-artifact cap (0 disables the cap)."""
    cap = settings.artifact_max_bytes
    return cap > 0 and byte_size > cap


class InMemoryArtifactStore:
    """Process-local `ArtifactStore` for tests and single-run use.

    Proves the content-addressing and dedup behavior without a database; the Postgres backend
    implements the same interface for durable, cross-process storage.
    """

    def __init__(self) -> None:
        """Start empty: no blobs, no links."""
        self._blobs: dict[str, bytes] = {}
        self._links: dict[tuple[str, str], ArtifactRef] = {}

    async def put(
        self,
        calc_key: str,
        name: str,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        compute_seconds: float | None = None,
    ) -> ArtifactRef | None:
        """Store `data`, deduplicating by content address. Returns `None` when refused."""
        if not settings.artifact_store_enabled or too_large(len(data)):
            return None
        digest = content_address(data)
        self._blobs.setdefault(digest, data)
        ref = ArtifactRef(
            calc_key=calc_key,
            name=name,
            content_hash=digest,
            byte_size=len(data),
            media_type=media_type,
        )
        self._links[(calc_key, name)] = ref
        return ref

    async def open(self, content_hash: str) -> bytes | None:
        """Return the artifact's bytes, or `None` on a miss."""
        return self._blobs.get(content_hash)

    async def list_for(self, calc_key: str) -> list[ArtifactRef]:
        """Return every artifact this calculation produced, ordered by name."""
        refs = [ref for (key, _), ref in self._links.items() if key == calc_key]
        return sorted(refs, key=lambda ref: ref.name)


async def put_all(
    store: ArtifactStore,
    calc_key: str,
    files: dict[str, bytes],
    *,
    compute_seconds: float | None = None,
) -> list[ArtifactRef]:
    """Store every captured file for one calculation; return the refs that were actually stored.

    The single call site the calculators share (DRY): capture hands back a `{name: bytes}` map and
    this persists it, dropping whatever the store refused. Media types are derived from the name
    so a caller never has to restate them.
    """
    stored: list[ArtifactRef] = []
    for name in sorted(files):
        ref = await store.put(
            calc_key,
            name,
            files[name],
            media_type=media_type_for(name),
            compute_seconds=compute_seconds,
        )
        if ref is None:
            logger.debug("artifact not stored (disabled or over cap): %s#%s", calc_key, name)
            continue
        stored.append(ref)
    return stored


# Media types for the by-products this system captures. `chemical/x-xyz` is the conventional type
# for a coordinate file; the xtb-specific formats have no registered type, so they carry a
# vendor-style name that says what a reader would need to parse them. A name that is not listed
# falls back to opaque bytes rather than guessing — the type is metadata for a human or a future
# reader, and a wrong one is worse than none.
_MEDIA_TYPES: dict[str, str] = {
    "hessian": "application/x-turbomole-hessian",
    "vibspectrum": "application/x-turbomole-vibspectrum",
    "xtbopt.xyz": "chemical/x-xyz",
    "crest_conformers.xyz": "chemical/x-xyz",
    "crest_rotamers.xyz": "chemical/x-xyz",
    "cre_members": "text/plain",
    # Packed numeric arrays this system writes itself rather than captures — the Hessian in the
    # form `calc.xtb_hessian` reads back (STO-2), and the dipole derivatives the in-process
    # backend needs to derive IR intensities from it.
    "hessian.npy": "application/x-npy",
    "dipole_derivatives.npy": "application/x-npy",
    # Reserved for the DFT tier (STO-5, gated on D-010): a converged density or orbital set, whose
    # reuse cuts mean SCF iterations from ~33 to ~2 in published measurement. The name and role are
    # fixed now so the contract exists before the implementation does; nothing writes these yet.
    "density.restart": "application/x-scf-restart",
    "orbitals.molden": "chemical/x-molden",
}


def media_type_for(name: str) -> str:
    """The media type for a captured file, by its producer-given name."""
    return _MEDIA_TYPES.get(name, "application/octet-stream")


# Which fields of a Hessian payload are packed arrays, and the artifact name each is stored under.
# Both names were already reserved in `_MEDIA_TYPES` for exactly this role, which is why they carry
# the `.npy` suffix that table keys on.
HESSIAN_ARRAYS: Mapping[str, str] = MappingProxyType(
    {"hessian_npy": "hessian.npy", "dipole_derivatives_npy": "dipole_derivatives.npy"}
)


class ArrayOffloadingStore:
    """A `ResultStore` that keeps a payload's packed arrays here instead of in the result row.

    **Why this exists as a store rather than as a second caching path.** One calculation in this
    system returns megabytes where every other returns numbers: a Hessian is 99x99 doubles at 33
    atoms and about 1.4 MB at 120. `durable/retention.py` refuses to prune `calculation_results`
    outright, because D-011 says a persisted result is never recomputed — so a matrix stored inline
    is a row that can never be reclaimed, in the one table that has no reclaim path. D-124 answered
    this before the capability migration and the answer still holds: the arrays belong in the
    content-addressed artifact store, which `durable/artifact_eviction.py` sweeps by cost and idle
    time, and the row keeps their content hashes. Evicting a cold matrix costs a recomputation,
    which is the trade that policy exists to make.

    Expressing it as a `ResultStore` is what keeps `cached_remote` — and every caller of it —
    unchanged: the decision "is this a hit?" already lives behind `get`, and "is this worth
    caching?" already lives behind `put`. A caller wraps its store and nothing else about the call
    site moves.

    Two rules carry the whole design, and both are the pre-split implementation's, kept because the
    reasoning behind them did not change:

    1. **A hit is a hit only if the blobs come back.** Every reason they might not — the store
       disabled, the matrix evicted as cold, a database restored without its artifact table — is an
       ordinary one, so a missing blob is a *miss to recompute from*, never an error.
    2. **The blobs are written first, and the row only if they all landed.** A row addressing an
       artifact that does not exist would be served as a hit forever and rejected on every read,
       which is strictly worse than not caching. Losing a by-product costs a future recomputation
       and never the calculation in hand, which the caller already holds — so a store that refuses
       is a debug line and an uncached result, not a raise.
    """

    def __init__(
        self, results: ResultStore, artifacts: ArtifactStore, fields: Mapping[str, str]
    ) -> None:
        """Wrap `results`, offloading each payload field in `fields` to `artifacts`."""
        self._results = results
        self._artifacts = artifacts
        self._fields = fields

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the result with its arrays put back, or `None` if any of them is gone."""
        stored = await self._results.get(key)
        if stored is None:
            return None

        payload = dict(stored.result)
        for field, name in self._fields.items():
            content_hash = payload.pop(_address(name), None)
            if content_hash is None:
                # This field was absent when the row was written — `dipole_derivatives_npy` is
                # populated by one backend and not the other — so there is nothing to restore.
                continue
            blob = await self._artifacts.open(str(content_hash))
            if blob is None:
                logger.info("%s is cached but its %s is gone; recomputing", key.as_str(), name)
                return None
            payload[field] = base64.b64encode(blob).decode("ascii")
        return stored.model_copy(update={"result": payload})

    async def put(self, stored: StoredResult) -> None:
        """Write the arrays, then the row — and skip the row entirely if any array did not land."""
        payload = dict(stored.result)
        files: dict[str, bytes] = {}
        for field, name in self._fields.items():
            encoded = payload.get(field)
            if encoded is None:
                continue
            files[name] = base64.b64decode(str(encoded))

        if not files:
            # Nothing to offload: store it as it is, so wrapping a store is never lossy for a
            # payload that happens to carry no arrays.
            await self._results.put(stored)
            return

        try:
            refs = await put_all(
                self._artifacts,
                stored.key.as_str(),
                files,
                compute_seconds=stored.compute_seconds,
            )
        except Exception:
            logger.warning(
                "could not store arrays for %s, so it is not cached",
                stored.key.as_str(),
                exc_info=True,
            )
            return

        by_name = {ref.name: ref for ref in refs}
        if any(name not in by_name for name in files):
            logger.debug("an array for %s was not stored, so it is not cached", stored.key.as_str())
            return

        for field, name in self._fields.items():
            if name not in files:
                continue
            payload.pop(field)
            payload[_address(name)] = by_name[name].content_hash
        await self._results.put(stored.model_copy(update={"result": payload}))

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Delegate, deliberately without restoring anything.

        `find` answers "which calculations exist", which `find_calculations` renders as a listing.
        Rehydrating megabytes per row to build a table nobody reads the matrices from would make a
        listing the most expensive call in the system. The rows come back naming their artifacts,
        which is what `fetch_artifact` takes.
        """
        return await self._results.find(query)


def _address(name: str) -> str:
    """The row field that holds an artifact's content hash, from the artifact's name."""
    return f"{name.removesuffix('.npy')}_artifact"
