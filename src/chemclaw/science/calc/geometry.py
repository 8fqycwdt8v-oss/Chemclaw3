"""The best optimized geometry known for a molecule, across methods (STO-4).

The calculation cache keys an optimization on `Structure.structure_id` — the coordinates — which
is exactly right for "did I already relax *this* geometry?" and useless for "do I already have a
good geometry for *this molecule*?". Two RDKit embeddings of the same SMILES differ in the sixth
decimal and miss each other's cache entries; a converged GFN-FF minimum cannot seed the GFN2 run
that would start from almost exactly the same place. `Structure.origin` records where a geometry
came from, and nothing anywhere looked forward.

This module is that forward pointer, and it is deliberately **not** a new table. The pointer is
itself an ordinary cached calculation under `geometry.best`, keyed on a *subject* — the canonical
structure of the molecule, its charge, its multiplicity and the solvent it was relaxed in — so it
inherits the store, the backends and the audit trail every other calculation already has.

**Why this is an opt-in lookup and not a hook inside `run_cached_optimization`.** Silently
swapping a caller's starting geometry for a better-known one would make the same request return
different answers under the same cache key, depending on what the store happened to contain when
it ran. That is precisely the cache dishonesty `chemclaw.science.calc.xtb_spec` was written to
prevent: a key must
name what actually ran. So the reuse is an explicit call — resolve the subject to its best known
geometry, *then* optimize it through the normal cached path, where the key describes the geometry
that was really the input.
"""

import logging

from pydantic import BaseModel, Field

from chemclaw.science.calc.store import CalculationKey, ResultStore, StoredResult
from chemclaw.science.calc.structure import Structure

logger = logging.getLogger(__name__)

# How much to trust a geometry from each method, as an ordering only. A GFN2 minimum is a better
# starting point than a GFN-FF one, which is better than nothing; the numbers have no meaning
# beyond their order. An unrecognised method sorts below every known one rather than raising,
# because refusing to record a geometry is a worse failure than ranking it conservatively.
_METHOD_LEVEL: dict[str, int] = {
    "GFN-FF": 10,
    "GFNFF": 10,
    "GFN0-xTB": 20,
    "GFN1-xTB": 30,
    "GFN2-xTB": 40,
}

# The pointer's own cache version. Bumping it invalidates every recorded pointer, which is the
# right response to a change in what "best" means (a new level, a different subject identity).
_POINTER_VERSION = "v1"


def method_level(method: str) -> int:
    """How good a geometry from `method` is taken to be, as a sortable rank."""
    return _METHOD_LEVEL.get(method, 0)


class GeometrySubject(BaseModel):
    """What a geometry is *of* — the molecule, not the coordinates.

    Frozen and normalized so two callers describing the same species reach the same pointer. The
    solvent is part of the subject rather than of the ranking: a geometry relaxed in water is not
    a better or worse gas-phase geometry, it is a geometry of a different thing.
    """

    model_config = {"frozen": True}

    smiles: str = Field(min_length=1)
    charge: int = 0
    multiplicity: int = 1
    solvent: str | None = None

    def key(self) -> CalculationKey:
        """The cache key of this subject's best-geometry pointer."""
        return CalculationKey.build(
            calc_type="geometry.best",
            calc_version=_POINTER_VERSION,
            inputs=self.model_dump(),
        )


class BestGeometry(BaseModel):
    """The best geometry recorded for a subject, and what produced it.

    `origin` is the `CalculationKey` string of the optimization it came from, so the pointer never
    becomes a second source of truth: it says *which* stored calculation holds the authoritative
    result, and that result is still there to be read.
    """

    structure: Structure
    method: str
    level: int
    energy_hartree: float
    origin: str


def subject_of(structure: Structure, solvent: str | None = None) -> GeometrySubject | None:
    """The subject a structure is a geometry of, or `None` if it cannot be identified.

    Requires a SMILES: a `Structure` built from raw coordinates with no connectivity cannot be
    matched to another embedding of the same molecule, so it has no subject and is simply not
    recorded. Returning `None` rather than raising keeps this usable on every optimization path
    without each one having to ask first.
    """
    if not structure.smiles:
        return None
    return GeometrySubject(
        smiles=structure.smiles,
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        solvent=solvent,
    )


async def best_known_geometry(store: ResultStore, subject: GeometrySubject) -> BestGeometry | None:
    """The best geometry recorded for `subject`, or `None` if none has been."""
    stored = await store.get(subject.key())
    if stored is None:
        return None
    return BestGeometry.model_validate(stored.result)


async def record_best_geometry(
    store: ResultStore,
    subject: GeometrySubject,
    candidate: BestGeometry,
) -> BestGeometry:
    """Record `candidate` if it beats what is already known; return whichever now stands.

    Ranked by method level first and energy second: a GFN2 minimum always displaces a GFN-FF one,
    and between two geometries from the same method the lower-energy one wins, which is the only
    comparison that means anything within a single potential energy surface. Comparing energies
    *across* methods would be meaningless — the zeros are different — which is why the level is
    the primary key and not a tiebreak.

    Concurrent recorders last-writer-win, exactly as the calculation cache does for two concurrent
    misses. The cost is that a worse geometry can briefly displace a better one; the alternative is
    a lock on a pointer whose only purpose is to save time.
    """
    current = await best_known_geometry(store, subject)
    if current is not None and (current.level, -current.energy_hartree) >= (
        candidate.level,
        -candidate.energy_hartree,
    ):
        return current
    await store.put(StoredResult(key=subject.key(), result=candidate.model_dump()))
    logger.debug(
        "best geometry for %s is now %s from %s", subject.smiles, candidate.method, candidate.origin
    )
    return candidate


async def starting_geometry(
    store: ResultStore, structure: Structure, solvent: str | None = None
) -> Structure:
    """The best known geometry for `structure`'s molecule, or `structure` itself.

    The opt-in reuse path (see the module docstring): a caller that wants "relax this compound"
    rather than "relax these coordinates" resolves through here first and then optimizes normally,
    so the resulting cache key still names the geometry that was actually the input.

    Falls back to the input whenever there is nothing better — no SMILES, no recorded pointer, or
    a pointer whose stored structure no longer parses — because a starting geometry is an
    optimization, not a requirement.
    """
    subject = subject_of(structure, solvent)
    if subject is None:
        return structure
    best = await best_known_geometry(store, subject)
    if best is None:
        return structure
    if best.structure.structure_id == structure.structure_id:
        return structure
    logger.debug(
        "seeding optimization of %s from the stored %s geometry", subject.smiles, best.method
    )
    return best.structure


async def record_optimization(
    store: ResultStore,
    structure: Structure,
    *,
    method: str,
    energy_hartree: float,
    solvent: str | None,
    origin: CalculationKey,
) -> None:
    """Offer a converged optimization to the pointer, doing nothing if it is not an improvement.

    Takes the fields rather than an `OptimizationResult` so this module does not import
    `chemclaw.science.calc.xtb_opt` — the optimizer calls *this*, and the dependency has to point
    one way. It also
    keeps the pointer honest about its own scope: it records geometries, and a geometry is a
    structure plus what produced it, not a whole optimization report.

    Returns nothing and cannot fail: an unidentifiable subject is skipped, and a store error is
    logged rather than raised. Updating a bookkeeping pointer must never be able to discard an
    optimization that already succeeded — the same "optional by construction" contract the artifact
    store takes (D-124).
    """
    subject = subject_of(structure, solvent)
    if subject is None:
        return
    try:
        await record_best_geometry(
            store,
            subject,
            BestGeometry(
                structure=structure,
                method=method,
                level=method_level(method),
                energy_hartree=energy_hartree,
                origin=origin.as_str(),
            ),
        )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning("could not record the best geometry for %s", subject.smiles, exc_info=True)
