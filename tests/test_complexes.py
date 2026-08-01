"""Non-covalent complexes: the interaction energy, and the cache key over a *pair* (X11).

The searches need `crest` and skip without it, as every CREST-backed test here does. What
does not need it is the part most likely to break silently: `_combine`, which decides what
"the pair" is as a cache subject, and therefore whether A-with-B and B-with-A are one
calculation or two.
"""

import asyncio

import pytest

from chemclaw.science.calc import crest_cli
from chemclaw.science.calc.complexes import (
    ComplexSpec,
    _combine,
    _opt_spec,
    _ordered,
    compute_interaction,
    run_cached_interaction,
)
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structure import Structure, structure_from_smiles

needs_crest = pytest.mark.skipif(
    not crest_cli.is_available(), reason="the crest binary is not installed"
)

# Interaction energies from CCSD(T)/CBS (the S22 set and its small-molecule companions),
# kcal/mol. Four systems spanning the range this method is asked about: a strong hydrogen
# bond, a mixed donor/acceptor pair, a weak hydrogen bond, and pure dispersion.
_REFERENCE_INTERACTIONS = [
    ("O", "O", -5.0, "water dimer"),
    ("O", "N", -6.4, "water-ammonia"),
    ("N", "N", -3.1, "ammonia dimer"),
    ("C", "C", -0.5, "methane dimer"),
]


def test_combining_two_monomers_does_not_overlap_them() -> None:
    """The starting arrangement separates the pair regardless of either molecule's shape.

    The offset is radius + radius + gap, so a long molecule beside a small one still
    starts clear of it. If this were a fixed displacement instead, a benzoic acid beside a
    water would begin with the water inside the ring -- and CREST would be handed a
    structure the SCF may not even converge on.
    """
    first = structure_from_smiles("c1ccccc1C(=O)O", optimize=True)
    second = structure_from_smiles("O", optimize=True)
    pair = _combine(first, second, separation=3.5)

    assert len(pair.elements) == len(first.elements) + len(second.elements)
    closest = min(
        sum((a - b) ** 2 for a, b in zip(left, right, strict=True)) ** 0.5
        for left in pair.positions[: len(first.elements)]
        for right in pair.positions[len(first.elements) :]
    )
    assert closest > 1.5  # no atom of one monomer sits inside a bond length of the other


def test_the_pair_carries_the_summed_charge_and_a_closed_shell() -> None:
    """Two neutral closed shells make a neutral closed shell; a charged monomer adds up.

    Multiplicity is `m1 + m2 - 1` rather than a hardcoded 1 so the arithmetic is stated
    rather than assumed -- and `Structure` rejects an open-shell monomer upstream, so the
    formula is never asked to guess a coupling it cannot know.
    """
    acetate = structure_from_smiles("CC(=O)[O-]", optimize=True)
    water = structure_from_smiles("O", optimize=True)
    pair = _combine(acetate, water, separation=3.5)
    assert pair.charge == -1
    assert pair.multiplicity == 1
    assert pair.smiles == "CC(=O)[O-].O"


def test_combine_is_not_symmetric_which_is_why_the_pair_is_ordered_first() -> None:
    """The bug this guards, stated as the reason `_ordered` exists.

    `_combine` holds the first monomer at the origin and offsets the second along +x.
    Swapping the arguments therefore negates the *intermolecular* vector while leaving
    each monomer's own orientation untouched — which is not a rigid motion of the pair,
    so the two arrangements are genuinely different geometries with different
    `structure_id`s.

    That was a real cache defect: `run_cached_interaction` keys on the combined structure,
    so before `_ordered` a chemist asking "does the API interact with the excipient" and
    one asking the reverse ran two minutes-long searches for one number. This test pins
    the asymmetry so nobody "simplifies" `_ordered` away on the assumption that `_combine`
    is symmetric — it is not, and the companion test below is the invariant that matters.
    """
    water = structure_from_smiles("O", optimize=True)
    ammonia = structure_from_smiles("N", optimize=True)
    forward = _combine(water, ammonia, separation=3.5)
    backward = _combine(ammonia, water, separation=3.5)
    assert _distances(forward) != pytest.approx(_distances(backward), abs=1e-6)


def test_the_pair_is_canonically_ordered_so_either_direction_is_one_calculation() -> None:
    """A-with-B and B-with-A resolve to the same pair, hence the same cache entry (D-011).

    Asserted at `_ordered` rather than by running two searches, because the searches are
    minutes each and the property under test is entirely deterministic. Spelling is
    canonicalized on the way through, so `OCC` and `CCO` are also one pair.
    """
    assert _ordered("O", "N") == _ordered("N", "O")
    assert _ordered("OCC", "O") == _ordered("O", "CCO")
    # The canonical form is what is stored and reported, not whichever spelling arrived.
    assert _ordered("OCC", "O") == ("CCO", "O")


def _distances(structure: Structure) -> list[float]:
    """Every interatomic distance in `structure`, sorted — its shape, free of orientation."""
    return sorted(
        sum((a - b) ** 2 for a, b in zip(left, right, strict=True)) ** 0.5
        for index, left in enumerate(structure.positions)
        for right in structure.positions[index + 1 :]
    )


@needs_crest
@pytest.mark.parametrize(("smiles_a", "smiles_b", "reference", "name"), _REFERENCE_INTERACTIONS)
def test_interaction_energies_track_ccsdt_references(
    smiles_a: str, smiles_b: str, reference: float, name: str
) -> None:
    """GFN2 reproduces the CCSD(T)/CBS binding energies of four small complexes.

    Measured: water dimer -4.97 (ref -5.0), water-ammonia -5.31 (-6.4), ammonia dimer
    -2.86 (-3.1), methane dimer -0.41 (-0.5). Three are within a few tenths; the mixed
    donor/acceptor pair is the worst at 1.1 kcal/mol under-bound, which is why the
    tolerance here is 1.5 rather than something tighter. That is the accuracy the skill
    should be claiming -- good enough to rank association strength and to say bound or
    not, not good enough to quote a binding constant from.
    """
    result = compute_interaction(ComplexSpec(), smiles_a, smiles_b)
    assert result.interaction_energy_kcal == pytest.approx(reference, abs=1.5)
    assert result.interaction_energy_kcal < 0  # every one of these is a bound pair
    assert result.binding_modes >= 1
    assert result.sampled is True
    assert len(result.monomer_energies_hartree) == 2


@needs_crest
def test_a_hydrogen_bonded_pair_binds_more_strongly_than_a_dispersion_one() -> None:
    """The ordering claim, which is what this is actually good for.

    Water dimer against methane dimer is about an order of magnitude in binding energy,
    far outside the method's error, so this holds regardless of run-to-run sampling.
    """
    hydrogen_bonded = compute_interaction(ComplexSpec(), "O", "O")
    dispersion = compute_interaction(ComplexSpec(), "C", "C")
    assert hydrogen_bonded.interaction_energy_kcal < dispersion.interaction_energy_kcal - 2.0


@needs_crest
def test_a_repeated_pair_is_served_from_the_store() -> None:
    """The pair is the cache subject, so the second request computes nothing (D-011).

    Asked in the *opposite order* the second time, which is the end-to-end proof of what
    `_ordered` buys: one entry for the pair, not one per direction. It matters more here
    than anywhere else in the calc layer — a complex search is a metadynamics run plus
    three optimizations, and it is minutes rather than seconds.
    """

    async def _run() -> None:
        store = InMemoryStore()
        first, cached_first = await run_cached_interaction(store, "O", "N")
        second, cached_second = await run_cached_interaction(store, "N", "O")
        assert cached_first is False
        assert cached_second is True
        assert first.interaction_energy_kcal == second.interaction_energy_kcal
        assert (first.smiles_a, first.smiles_b) == (second.smiles_a, second.smiles_b)

    asyncio.run(_run())


def test_the_three_optimizations_run_on_the_backend_the_key_names() -> None:
    """The half of the cache key that a *propagation* has to keep true.

    `ComplexSpec.calc_version` names `engine` because the three `optimize_structure` calls in
    `compute_interaction` produce every number in an `InteractionResult`. That claim is only
    honest while `_opt_spec` carries the engine across instead of letting `OptSpec` re-resolve
    it from config — a re-resolve would let the key say `xtb` while tblite did the relaxing,
    which is the same defect (D-011) read from the other end.
    """
    assert _opt_spec(ComplexSpec(engine="xtb")).engine == "xtb"
    assert _opt_spec(ComplexSpec(engine="tblite")).engine == "tblite"


def test_an_open_shell_monomer_is_rejected_before_any_search_runs() -> None:
    """A radical partner would make the pair's multiplicity a guess, so it fails fast (G4).

    Two doublets can couple to a singlet or a triplet and nothing in the input says which.
    `_combine`'s arithmetic would silently pick one; `Structure` refuses the monomer first.
    """
    with pytest.raises(ValueError):
        structure_from_smiles("[CH3]", optimize=True)
