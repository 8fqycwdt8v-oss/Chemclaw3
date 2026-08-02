"""The bench-chemistry tool surface (gaps TOOL-2, TOOL-3, TOOL-4, TOOL-5).

Four capabilities the agent lacked. The two that matter most:

- **TOOL-2 identity resolution.** Every chemistry tool takes SMILES and every chemist writes names,
  so a name-shaped question missed a SMILES-keyed corpus entirely. The load-bearing property here
  is *conservatism*: an unrecognised name must return nothing, never a guess, because a fabricated
  structure propagates silently into a calculation, a similarity search, and a proposed note.
Hazard screening (TOOL-3) landed independently on `main` (`safety/`, D-080); this branch's
named-substance and named-pair knowledge was contributed to `safety/rules.yaml` and is pinned by
`tests/test_safety_pairs.py` rather than by a second screen.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.chem.server.tools import (
    green_metrics,
    render_structure,
    resolve_compound,
    stoichiometry_table,
)
from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.reagents import (
    density_of,
    display_name,
    known_names,
    resolve_compound_name,
)


def _run(coro: Any) -> Any:
    """Drive one async tool call (the repo's convention — no pytest-asyncio)."""
    return asyncio.run(coro)


# --- TOOL-2: identity resolution -------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("DIPEA", "N,N-diisopropylethylamine"),
        ("dipea", "N,N-diisopropylethylamine"),
        ("Hunig's base", "N,N-diisopropylethylamine"),
        ("2-MeTHF", "2-methyltetrahydrofuran"),
        ("Pd(OAc)2", "palladium(II) acetate"),
        ("NaN3", "sodium azide"),
        ("DMF", "N,N-dimethylformamide"),
    ],
)
def test_the_spellings_chemists_actually_write_resolve(written: str, expected: str) -> None:
    """Case, punctuation, and apostrophes all fold — a chemist should not have to guess a format."""
    match = resolve_compound_name(written)
    assert match is not None and match.name == expected


def test_the_same_substance_resolves_identically_from_name_or_structure() -> None:
    """This equivalence is what lets the hazard screen and the calculators agree (KNW-4's seed)."""
    by_name = resolve_compound_name("sodium azide")
    by_abbrev = resolve_compound_name("NaN3")
    assert by_name is not None and by_abbrev is not None
    assert by_name.smiles == by_abbrev.smiles
    by_structure = resolve_compound_name(by_name.smiles)
    assert by_structure is not None and by_structure.smiles == by_name.smiles


def test_an_unknown_name_resolves_to_nothing_rather_than_itself() -> None:
    """The critical property: a miss is a miss.

    The lenient `canonical_smiles` returns its input unparsed, which would have turned every miss
    into a fabricated structure — the exact failure this module exists to prevent.
    """
    for unknown in ("nonsense-xyz", "Compound 27b", "", "   ", "the usual base"):
        assert resolve_compound_name(unknown) is None


def test_a_structure_is_reported_as_such() -> None:
    """`source` lets a caller weigh how the identity was established."""
    assert resolve_compound_name("CCN(CC)CC").source == "smiles"  # type: ignore[union-attr]
    assert resolve_compound_name("Et3N").source == "synonym"  # type: ignore[union-attr]


def test_a_known_structure_renders_back_as_its_name() -> None:
    """So an answer can say "triethylamine", not "CCN(CC)CC"."""
    assert display_name("CCN(CC)CC") == "triethylamine"
    assert display_name("C1CCCCC1CCCCC") is None  # not a known reagent
    assert display_name("not-a-molecule") is None


def test_the_table_is_a_real_working_set() -> None:
    """Small enough to review, large enough to cover a day at the bench."""
    assert len(known_names()) > 60


def test_the_tool_returns_none_not_an_error_on_a_miss() -> None:
    """The agent must be able to say "unknown reagent" without the turn failing."""
    assert _run(resolve_compound("nonsense-xyz")) is None
    assert _run(resolve_compound("DIPEA")) is not None


# --- TOOL-4: stoichiometry -------------------------------------------------------------------


def test_a_charge_table_scales_every_reagent_to_the_limiting_one() -> None:
    """The everyday bench question, answered deterministically from molecular weights."""
    table = _run(stoichiometry_table("EtOH", 46.07, ["Et3N"], [2.0]))
    assert table.basis_name == "ethanol"
    basis, base = table.rows
    assert basis.moles_mmol == pytest.approx(1000.0, rel=1e-3)  # 46.07 g of MW 46.07 = 1 mol
    assert base.moles_mmol == pytest.approx(2000.0, rel=1e-3)
    assert base.mass_g == pytest.approx(2 * 101.19, rel=1e-2)  # 2 mol of Et3N


def test_an_unresolvable_reagent_gets_no_row_rather_than_a_guessed_mass() -> None:
    """A guessed mass is a weighing error; an omission with a name is a question."""
    table = _run(stoichiometry_table("EtOH", 46.07, ["Et3N", "Compound 27b"], [1.0, 1.0]))
    assert [r.name for r in table.rows] == ["ethanol", "triethylamine"]
    assert table.unresolved == ["Compound 27b"]


def test_mismatched_reagents_and_equivalents_are_rejected() -> None:
    """A silently zipped-short list would under-charge a batch."""
    with pytest.raises(ValueError, match="must match"):
        _run(stoichiometry_table("EtOH", 10.0, ["Et3N", "DIPEA"], [1.0]))


def test_a_nonpositive_basis_is_rejected() -> None:
    """Zero mass would divide the whole table to zero or blow up."""
    with pytest.raises(ValueError, match="must be positive"):
        _run(stoichiometry_table("EtOH", 0.0, [], []))


def test_an_unresolvable_basis_is_an_error_not_an_empty_table() -> None:
    """The basis sets the scale, so guessing or skipping it would silently invalidate every row."""
    with pytest.raises(ValueError, match="limiting reagent"):
        _run(stoichiometry_table("Compound 27b", 10.0, [], []))


# --- TOOL-5: rendering -----------------------------------------------------------------------


def test_a_molecule_renders_as_inline_svg() -> None:
    """A chemist reads a drawing far faster than a SMILES string."""
    svg = _run(render_structure("c1ccccc1C(=O)O"))
    assert "<svg" in svg and "</svg>" in svg


def test_a_reaction_renders_too() -> None:
    """Reaction SMILES take the reaction drawer, not the molecule one."""
    assert "<svg" in _run(render_structure("CCO>>CC=O"))


def test_an_unparseable_structure_raises_rather_than_drawing_nothing() -> None:
    """An empty box would read as "no structure", not as "bad input"."""
    with pytest.raises(InvalidSmilesError):
        _run(render_structure("not-a-molecule"))


# --- TOOL-4b: solvent charges by volume ------------------------------------------------------


def test_a_solvent_mixture_is_charged_by_volumes_not_by_equivalents() -> None:
    """The live-run case: "THF/water 4:1 at 10 volumes" on a 2 kg basis.

    The tool took only molar equivalents, so this charge could not be expressed at all; the model
    passed 40 and 10 as equivalents instead and the principal solvent came out 2.17x wrong, with
    the answer then certifying the figures as self-consistent. On the parent commit the same
    substitution against this fixture's Boc2O basis gives 26431 g of THF where 14224 g is correct,
    a factor of 1.86 — the multiplier is whatever the basis's molecular weight makes it, which is
    why the equivalents figure cannot be corrected into a right answer. The fixture is chosen so a
    naive
    implementation gives the wrong answer twice over: the two solvents have different densities
    (0.889 vs 0.998), so assuming 1 g/mL is visibly wrong on both, and the split is 8 + 2 volumes
    rather than 5 + 5, so a table that ignored the ratio would still land on the wrong masses.
    """
    table = _run(
        stoichiometry_table("Boc2O", 2000.0, ["DIPEA"], [1.2], ["THF", "water"], [8.0, 2.0])
    )
    thf, water = (row for row in table.rows if row.role == "solvent")
    assert thf.volume_ml == pytest.approx(16000.0)  # 8 volumes x 2000 g
    assert thf.mass_g == pytest.approx(16000.0 * 0.889)
    assert water.volume_ml == pytest.approx(4000.0)
    assert water.mass_g == pytest.approx(4000.0 * 0.998)
    # Moles and equivalents are derived for a solvent too, so every row of the table is comparable
    # and `green_metrics` can take `mass_g` off all of them.
    assert thf.moles_mmol == pytest.approx(thf.mass_g / thf.molecular_weight * 1000.0)
    assert thf.equivalents > 20.0  # ~22 equiv of THF: the number "10 volumes" never means


def test_the_reagent_rows_are_untouched_by_a_solvent_charge() -> None:
    """Adding solvents must not disturb the molar arithmetic the table already did correctly."""
    table = _run(stoichiometry_table("Boc2O", 2000.0, ["DIPEA"], [1.2], ["THF"], [10.0]))
    basis, base = (row for row in table.rows if row.role != "solvent")
    assert (basis.role, base.role) == ("basis", "reagent")
    assert base.equivalents == pytest.approx(1.2)
    assert base.moles_mmol == pytest.approx(basis.moles_mmol * 1.2)


def test_a_solvent_passed_as_a_molar_reagent_is_rejected() -> None:
    """The original error, made unrepeatable rather than merely documented.

    Accepting 40 "equivalents" of THF would produce a plausible table with no sign of which
    reading was meant, and the run showed the wrong reading then being certified as consistent.
    """
    with pytest.raises(ValueError, match="charged by volume"):
        _run(stoichiometry_table("Boc2O", 2000.0, ["THF"], [40.0]))


def test_a_solvent_with_no_density_refuses_rather_than_guessing() -> None:
    """Pyridine is a known reagent with no density on file: an error, not a zero and not 1 g/mL.

    An error rather than an `unresolved` entry, unlike an unknown reagent, and the asymmetry is
    the point: a chemist reads a charge list line by line and notices a missing reagent, whereas
    a silently missing solvent leaves a complete-looking table that halves the E-factor and PMI
    computed from its masses.
    """
    with pytest.raises(ValueError, match="no density on file"):
        _run(stoichiometry_table("Boc2O", 100.0, [], [], ["pyridine"], [5.0]))


def test_an_unresolvable_solvent_is_an_error_too() -> None:
    """A solvent that silently vanished from the table would flatter every mass metric."""
    with pytest.raises(ValueError, match="could not resolve the solvent"):
        _run(stoichiometry_table("Boc2O", 100.0, [], [], ["Compound 27b"], [5.0]))


def test_mismatched_solvents_and_volumes_are_rejected() -> None:
    """The same guard the reagent/equivalent pair has, for the same reason."""
    with pytest.raises(ValueError, match="must match"):
        _run(stoichiometry_table("Boc2O", 100.0, [], [], ["THF"], [5.0, 5.0]))


def test_a_nonpositive_volume_is_rejected() -> None:
    """Zero volumes is not a charge; it is a missing number that would read as a real one."""
    with pytest.raises(ValueError, match="volumes must be positive"):
        _run(stoichiometry_table("Boc2O", 100.0, [], [], ["THF"], [0.0]))


def test_a_density_is_looked_up_never_estimated() -> None:
    """`density_of` answers for the bulk solvents and returns None for everything else."""
    assert density_of("THF") == pytest.approx(0.889)
    assert density_of("tetrahydrofuran") == density_of("C1CCOC1") == density_of("THF")
    assert density_of("DIPEA") is None  # a known reagent, not charged by volume
    assert density_of("Compound 27b") is None  # not a known substance at all


def test_the_charge_table_feeds_green_metrics_with_the_solvent_included() -> None:
    """The pairing `green_metrics`' docstring promises, on the term that dominates both metrics.

    Without solvent the same batch scores an E-factor of ~1.4; with it, ~10. That gap is why the
    omission was worth an error rather than a caveat.
    """
    table = _run(
        stoichiometry_table("Boc2O", 2000.0, ["DIPEA"], [1.2], ["THF", "water"], [8.0, 2.0])
    )
    with_solvent = _run(green_metrics([row.mass_g for row in table.rows], 1800.0))
    reagents_only = _run(
        green_metrics([r.mass_g for r in table.rows if r.role != "solvent"], 1800.0)
    )
    assert with_solvent.e_factor > 5 * reagents_only.e_factor
