"""Content-addressed store for a calculation's by-products (D-124).

Keeps the bytes a run produced (a Hessian, a geometry), named by the SHA-256 of their uncompressed
content and linked to the run as `(calc_key, name)`. Optional by construction: `put` returns `None`
rather than raising when disabled or over `artifact_max_bytes`, so capturing a by-product never
fails its calculation.
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
from chemclaw.science.calc.flight import ClaimsProvider, PostgresClaims
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    ResultStore,
    StoredResult,
)

logger = logging.getLogger(__name__)

# How a blob's bytes are stored. `zlib` is stdlib; the codec is recorded per row, so adding one
# later is a new value, never a migration.
Codec = str


def content_address(data: bytes) -> str:
    """The SHA-256 hex digest of `data` — an artifact's content address.

    Over the uncompressed bytes so the address is independent of codec and level. Raw bytes, unlike
    `core.ids.stable_hash`, which hashes a JSON rendering.
    """
    return hashlib.sha256(data).hexdigest()


def encode(data: bytes) -> tuple[Codec, bytes]:
    """Compress `data` for storage; return the codec that was used and the payload.

    Returns `("none", data)` when compression is disabled or does not shrink the payload, so
    incompressible artifacts are not stored larger.
    """
    level = settings.artifact_compression_level
    if level <= 0:
        return "none", data
    packed = zlib.compress(data, level)
    return ("zlib", packed) if len(packed) < len(data) else ("none", data)


def decode(codec: Codec, payload: bytes) -> bytes:
    """Return the original bytes of a payload stored under `codec`.

    An unknown codec raises rather than passing compressed bytes off as the file.
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

        `compute_seconds` is the producing calculation's wall time — the cost of not having it,
        which eviction orders by.
        """
        ...

    async def open(self, content_hash: str) -> bytes | None:
        """Return the artifact's original bytes, or `None` if it is not stored."""
        ...

    async def list_for(self, calc_key: str) -> list[ArtifactRef]:
        """Return every artifact this calculation produced, ordered by name."""
        ...


async def find_link(store: ArtifactStore, calc_key: str, name: str) -> ArtifactRef | None:
    """The stored `(calc_key, name)` link, or `None` — the one lookup by name the store offers.

    Filters `list_for`, since a calculation keeps only a handful of by-products. Used by every
    surface that addresses an artifact as `<calc_key>#<name>`.
    """
    return next((ref for ref in await store.list_for(calc_key) if ref.name == name), None)


def split_ref(text: str) -> tuple[str, str] | None:
    """`<calc_key>#<name>` as its two halves, or `None` when it is not one.

    Split at the **first** `#`: a calculation key never contains `#`, while a name may.
    """
    calc_key, separator, name = text.partition("#")
    if not separator or not calc_key or not name:
        return None
    return calc_key, name


def too_large(byte_size: int) -> bool:
    """Whether `byte_size` exceeds the per-artifact cap (0 disables the cap)."""
    cap = settings.artifact_max_bytes
    return cap > 0 and byte_size > cap


class InMemoryArtifactStore:
    """Process-local `ArtifactStore` — the reference the Postgres one is written to match.

    A differential test oracle, not a deployment backend: no configuration returns it.
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

    Media types are derived from the name; whatever the store refuses is dropped.
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


# Media types for captured by-products. xtb-specific formats get vendor-style names; an unlisted
# name falls back to opaque bytes, since a wrong type is worse than none.
_MEDIA_TYPES: dict[str, str] = {
    "hessian": "application/x-turbomole-hessian",
    "vibspectrum": "application/x-turbomole-vibspectrum",
    "xtbopt.xyz": "chemical/x-xyz",
    "crest_conformers.xyz": "chemical/x-xyz",
    "crest_rotamers.xyz": "chemical/x-xyz",
    "cre_members": "text/plain",
    # Packed numeric arrays written rather than captured: the Hessian and the dipole derivatives
    # needed to derive IR intensities from it.
    "hessian.npy": "application/x-npy",
    "dipole_derivatives.npy": "application/x-npy",
    # Reserved names for a converged density or orbital restart file; nothing writes these yet.
    "density.restart": "application/x-scf-restart",
    "orbitals.molden": "chemical/x-molden",
}


def media_type_for(name: str) -> str:
    """The media type for a captured file, by its producer-given name."""
    return _MEDIA_TYPES.get(name, "application/octet-stream")


# Which fields of a Hessian payload are packed arrays, and the artifact name each is stored
# under (both `.npy` names in `_MEDIA_TYPES`).
HESSIAN_ARRAYS: Mapping[str, str] = MappingProxyType(
    {"hessian_npy": "hessian.npy", "dipole_derivatives_npy": "dipole_derivatives.npy"}
)


class ArrayOffloadingStore:
    """A `ResultStore` that keeps a payload's packed arrays here instead of in the result row.

    `calculation_results` is never pruned, so megabyte Hessians go to the evictable artifact store
    and the row keeps their hashes. A hit counts only if every blob comes back (else it is a miss to
    recompute); blobs are written before the row, and a refusal leaves the result uncached rather
    than raising.
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

    def claims(self) -> PostgresClaims | None:
        """The wrapped store's claim ledger, if it has one: arrays live beside the same row."""
        inner = self._results
        return inner.claims() if isinstance(inner, ClaimsProvider) else None

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Delegate, deliberately without restoring anything.

        A listing does not read the matrices, so rows come back naming their artifacts (what
        `fetch_artifact` takes) rather than rehydrating megabytes each.
        """
        return await self._results.find(query)


def _address(name: str) -> str:
    """The row field that holds an artifact's content hash, from the artifact's name."""
    return f"{name.removesuffix('.npy')}_artifact"
