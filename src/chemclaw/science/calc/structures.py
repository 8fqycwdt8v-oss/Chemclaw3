"""Where a computed geometry lives so that its address resolves.

`Structure.structure_id` is a content address over a geometry's chemistry, derived byte-identically
here and on the calculation server. Every result model that describes a geometry reports one, and
until `D-2026-08-21-a-geometry-is-an-address-not-a-payload` **nothing accepted one**: the address
was a handle onto a store that did not exist, so a conformer search's twenty geometries could only
reach the next calculation as the SMILES they all came from — which is the search thrown away.

This is that store, and it is deliberately the smallest thing that closes the gap: two methods over
one content-addressed table. It is not a cache (there is nothing to recompute — the bytes *are* the
answer), and it is not the artifact store (see below), so it borrows neither's machinery.

**Why not `artifacts.ArtifactStore`.** That store addresses a blob by the SHA-256 of its bytes and
reaches it through a `(calc_key, name)` link. A geometry's identity is *narrower* than its bytes:
`structure_id` excludes `smiles` and `origin`, because two identical geometries are the same
structure whether one was embedded and the other optimized — and that is precisely the identity
that lets a downstream task hit the cache regardless of which route produced its input. Addressing
it by its bytes would fork on the provenance the identity ignores, and reaching it through a
calculation key would make the *producer* part of the address. `api/tool_results.py` declined to
share that store for the same reason, in the same words: one of the two keys would be pretending to
be the other.

**The invariant this exists to hold: every `structure_id` the agent is shown resolves.** It holds
because the write and the projection are two halves of one act — `chemclaw.science.calc.geometry`
strips a geometry out of a model-facing payload and this module keeps it, and both are driven from
the same walker over the same shape. A handle the agent can read is therefore a handle it can pass
back, structurally rather than by convention.
"""

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from chemclaw.science.calc.models import Structure

logger = logging.getLogger(__name__)


@runtime_checkable
class StructureStore(Protocol):
    """Persistence for geometries, addressed by content. Backends implement this."""

    async def put(self, structures: Sequence[Structure]) -> None:
        """Persist every geometry under its own `structure_id`; a repeat writes nothing new.

        **Takes a sequence rather than one structure, because the calculation that motivates this
        store returns forty-seven of them.** A per-structure method would have made a conformer
        search forty-seven connection checkouts and forty-seven commits — on the *cache hit* path
        too, since the geometries are persisted from whatever the cache returned. One statement is
        one round trip.
        """
        ...

    async def get(self, structure_id: str) -> Structure | None:
        """Return the geometry `structure_id` names, or None when nothing is stored under it."""
        ...


class InMemoryStructureStore:
    """The same contract in process, for tests and for a deployment with no database.

    A dict rather than an LRU: a geometry is kilobytes, a process holds one conversation's worth,
    and evicting one would break the handle a turn is still holding — which is the one failure this
    store exists to prevent.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._by_id: dict[str, Structure] = {}

    async def put(self, structures: Sequence[Structure]) -> None:
        """Keep each geometry under its content address."""
        for structure in structures:
            self._by_id[structure.structure_id] = structure

    async def get(self, structure_id: str) -> Structure | None:
        """Return what is stored under `structure_id`, or None."""
        return self._by_id.get(structure_id)


class UnknownStructureError(ValueError):
    """A `structure_id` was given that this deployment cannot resolve to a geometry.

    A `ValueError` so it takes the bad-data path everywhere that already sorts errors that way:
    `durable/publish._BAD_DATA_TYPES` fails a job fast instead of retrying an id that will never
    resolve, and `agent/tool_authz.surface_domain_errors` hands the sentence to the model verbatim
    so it can say which handle it was and what to run to get a fresh one.
    """


async def require_structure(store: StructureStore, structure_id: str) -> Structure:
    """Resolve `structure_id`, or raise a message a model can act on.

    One function rather than a `get`-then-check at each of the six call sites, because the message
    is the interesting part: a handle that does not resolve is almost always a handle from a
    conversation older than the deployment's data, and the remedy is to re-run the search rather
    than to retry the id.

    Args:
        store: Where geometries are kept.
        structure_id: The address, as a result reported it.

    Returns:
        The geometry.

    Raises:
        UnknownStructureError: Nothing is stored under that address.
    """
    structure = await store.get(structure_id)
    if structure is None:
        raise UnknownStructureError(
            f"no geometry is stored under {structure_id!r}. A structure id names a specific "
            "computed geometry, so an unresolvable one is not a typo to retry — re-run the "
            "calculation that produces it (optimize_geometry, sample_conformers, scan_coordinate) "
            "and use an id from that result."
        )
    return structure
