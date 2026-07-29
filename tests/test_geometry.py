"""The cross-method geometry pointer (STO-4).

The gap it closes: the calculation cache keys an optimization on coordinates, so two RDKit
embeddings of the same molecule miss each other entirely and a converged GFN-FF minimum cannot
seed the GFN2 run that would start from nearly the same place. `Structure.origin` recorded where a
geometry came from and nothing looked forward.

The property that makes this safe rather than clever is asserted here too: consulting the pointer
never changes a cache key. It is a lookup a caller performs *before* optimizing, so the key still
names the geometry that was really the input.
"""

import asyncio

from chemclaw.science.calc.geometry import (
    BestGeometry,
    GeometrySubject,
    best_known_geometry,
    method_level,
    record_best_geometry,
    record_optimization,
    starting_geometry,
    subject_of,
)
from chemclaw.science.calc.store import CalculationKey, InMemoryStore
from chemclaw.science.calc.structure import Structure

_ORIGIN = CalculationKey.build("xtb.opt", "GFN2-xTB+tblite+x", inputs={"structure": "s"})


def _ethanol(offset: float = 0.0, smiles: str | None = "CCO") -> Structure:
    """An ethanol-shaped structure, displaced by `offset` so two calls differ in coordinates."""
    return Structure(
        elements=[6, 6, 8],
        positions=[[0.0, 0.0, 0.0 + offset], [1.5, 0.0, 0.0], [2.1, 1.2, 0.0]],
        smiles=smiles,
    )


def _best(structure: Structure, method: str, energy: float) -> BestGeometry:
    """A pointer payload for `structure` from `method`."""
    return BestGeometry(
        structure=structure,
        method=method,
        level=method_level(method),
        energy_hartree=energy,
        origin=_ORIGIN.as_str(),
    )


def test_two_embeddings_of_one_molecule_share_a_subject_but_not_a_structure_id() -> None:
    """The whole premise: coordinates differ, the molecule does not.

    This is why the optimization cache alone cannot answer "do we have a good geometry for this
    compound" — its key is the left-hand side of this assertion, and the question is the right.
    """
    first, second = _ethanol(), _ethanol(offset=1e-4)
    assert first.structure_id != second.structure_id
    assert subject_of(first) == subject_of(second)


def test_a_better_method_displaces_a_worse_one_and_a_worse_one_does_not() -> None:
    """Ranked by method level first: a GFN2 minimum beats a GFN-FF one whatever the energies say.

    Comparing energies across methods is meaningless — the zeros are different — so the level is
    the primary key rather than a tiebreak. The force-field geometry here has the *lower* number,
    which is exactly the trap that ordering avoids.
    """

    async def _run() -> None:
        store = InMemoryStore()
        subject = subject_of(_ethanol())
        assert subject is not None

        await record_best_geometry(store, subject, _best(_ethanol(), "GFN-FF", -100.0))
        after = await record_best_geometry(store, subject, _best(_ethanol(0.1), "GFN2-xTB", -10.0))
        assert after.method == "GFN2-xTB"

        # ...and the force field cannot take it back, however low its own energy scale runs.
        again = await record_best_geometry(store, subject, _best(_ethanol(0.2), "GFN-FF", -999.0))
        assert again.method == "GFN2-xTB"

    asyncio.run(_run())


def test_within_one_method_the_lower_energy_geometry_wins() -> None:
    """The only energy comparison that means anything: two points on the same surface."""

    async def _run() -> None:
        store = InMemoryStore()
        subject = subject_of(_ethanol())
        assert subject is not None
        await record_best_geometry(store, subject, _best(_ethanol(), "GFN2-xTB", -154.1))
        best = await record_best_geometry(store, subject, _best(_ethanol(0.3), "GFN2-xTB", -154.9))
        assert best.energy_hartree == -154.9

        held = await best_known_geometry(store, subject)
        assert held is not None and held.energy_hartree == -154.9

    asyncio.run(_run())


def test_a_structure_without_a_smiles_has_no_subject_and_is_simply_not_recorded() -> None:
    """Raw coordinates cannot be matched to another embedding, so they have no subject.

    Returning `None` rather than raising is what lets every optimization path call the recorder
    unconditionally without first asking whether the structure is identifiable.
    """

    async def _run() -> None:
        anonymous = _ethanol(smiles=None)
        assert subject_of(anonymous) is None
        store = InMemoryStore()
        # No subject, no write, no error.
        await record_optimization(
            store,
            anonymous,
            method="GFN2-xTB",
            energy_hartree=-154.0,
            solvent=None,
            origin=_ORIGIN,
        )
        assert await starting_geometry(store, anonymous) is anonymous

    asyncio.run(_run())


def test_a_solvent_is_part_of_the_subject_not_of_the_ranking() -> None:
    """A geometry relaxed in water is a geometry of a different thing, not a better one."""
    gas = subject_of(_ethanol(), solvent=None)
    water = subject_of(_ethanol(), solvent="water")
    assert gas != water
    assert gas is not None and water is not None
    assert gas.key() != water.key()


def test_starting_geometry_returns_the_input_when_nothing_better_is_known() -> None:
    """A seed is an optimization, never a requirement — an empty store changes nothing."""

    async def _run() -> None:
        store = InMemoryStore()
        structure = _ethanol()
        assert await starting_geometry(store, structure) is structure

    asyncio.run(_run())


def test_starting_geometry_hands_back_the_recorded_minimum_for_a_new_embedding() -> None:
    """The payoff: a fresh embedding of a known molecule starts from the converged geometry."""

    async def _run() -> None:
        store = InMemoryStore()
        relaxed = _ethanol(offset=0.5)
        await record_optimization(
            store,
            relaxed,
            method="GFN2-xTB",
            energy_hartree=-154.9,
            solvent=None,
            origin=_ORIGIN,
        )
        fresh = _ethanol(offset=1e-3)
        seed = await starting_geometry(store, fresh)
        assert seed.structure_id == relaxed.structure_id

    asyncio.run(_run())


def test_a_recorded_pointer_never_changes_an_optimizations_cache_key() -> None:
    """The safety property, asserted directly (see the module docstring).

    If consulting the pointer could change a key, the same request would return different answers
    depending on what the store happened to hold — the exact cache dishonesty
    `chemclaw.science.calc.xtb_spec` was
    written to prevent. It cannot, because the key is derived from the structure handed to the
    optimizer, and the seeding happens before that.
    """

    async def _run() -> None:
        from chemclaw.science.calc.xtb_opt import OptSpec

        store = InMemoryStore()
        fresh = _ethanol(offset=1e-3)
        before = OptSpec().cache_key(fresh)

        await record_optimization(
            store,
            _ethanol(offset=0.5),
            method="GFN2-xTB",
            energy_hartree=-154.9,
            solvent=None,
            origin=_ORIGIN,
        )
        assert OptSpec().cache_key(fresh) == before

        # The seed is a *different* structure, so optimizing it is a different, honestly-keyed
        # calculation — not the same key with a different answer.
        seed = await starting_geometry(store, fresh)
        assert OptSpec().cache_key(seed) != before

    asyncio.run(_run())


def test_an_unknown_method_ranks_below_every_known_one_rather_than_raising() -> None:
    """Recording a geometry conservatively beats refusing to record it."""
    assert method_level("something-new") == 0
    assert method_level("GFN-FF") > 0
    assert method_level("GFN2-xTB") > method_level("GFN-FF")


def test_a_store_failure_cannot_break_the_optimization_that_succeeded() -> None:
    """The pointer is bookkeeping: losing it costs a future shortcut, never a converged geometry."""

    class _Broken(InMemoryStore):
        async def put(self, stored: object) -> None:
            raise ConnectionError("Postgres unreachable")

    async def _run() -> None:
        await record_optimization(
            _Broken(),
            _ethanol(),
            method="GFN2-xTB",
            energy_hartree=-154.0,
            solvent=None,
            origin=_ORIGIN,
        )

    asyncio.run(_run())


def test_the_subject_key_is_stable_across_equal_subjects() -> None:
    """Two callers describing the same species must reach the same pointer, not two."""
    left = GeometrySubject(smiles="CCO", charge=0, multiplicity=1)
    right = GeometrySubject(smiles="CCO")
    assert left.key() == right.key()
    assert GeometrySubject(smiles="CCO", charge=-1).key() != left.key()
