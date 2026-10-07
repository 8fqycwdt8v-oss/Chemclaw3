"""Where a computed geometry lives so that its address resolves
(D-2026-08-21-a-geometry-is-an-address-not-a-payload).

`Structure.structure_id` is a content address derived identically here and on the server; this store
lets a reported address be passed to the next calculation. Two methods over one content-addressed
table — not a cache and not the artifact store, whose byte- and `(calc_key, name)`-addressing would
fork on provenance that `structure_id` deliberately ignores.

Invariant: every `structure_id` the agent is shown resolves. `chemclaw.science.calc.geometry` strips
a geometry from a payload and this module keeps it, both driven by the same walker.
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

        Takes a sequence so a conformer search's geometries are one round trip, on the cache-hit
        path too.
        """
        ...

    async def get(self, structure_id: str) -> Structure | None:
        """Return the geometry `structure_id` names, or None when nothing is stored under it."""
        ...


class InMemoryStructureStore:
    """The same contract in process — the reference the Postgres one is written to match.

    A differential test oracle, not a deployment backend. A dict, not an LRU: evicting a geometry
    would break a handle a turn still holds.
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

    A `ValueError`, so durable jobs fail fast instead of retrying and the model is handed the
    message verbatim.
    """


async def require_structure(store: StructureStore, structure_id: str) -> Structure:
    """Resolve `structure_id`, or raise a message a model can act on.

    An unresolvable handle almost always comes from older data, and the remedy is to re-run the
    search; one function keeps that message identical across the MCP and Temporal callers.

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
