"""The bench-chemistry tool surface (gaps TOOL-2, TOOL-3, TOOL-4, TOOL-5).

Four capabilities the agent lacked. The two that matter most:

- **TOOL-2 identity resolution.** Every chemistry tool takes SMILES and every chemist writes names,
  so a name-shaped question missed a SMILES-keyed corpus entirely. The load-bearing property here
  is *conservatism*: an unrecognised name must return nothing, never a guess, because a fabricated
  structure propagates silently into a calculation, a similarity search, and a proposed note.
- **TOOL-3 hazard screening.** The only gap in the analysis whose failure mode is physical. The
  agent is instructed to design protocols and `propose_knowledge_note` turns those into reusable
  precedent, with nothing between the two but a human reading prose.
"""

import asyncio
from typing import Any

import pytest

from agents.chem_tools import (
    render_structure,
    resolve_compound,
    screen_hazards,
    stoichiometry_table,
)
from chemclaw.chem import InvalidSmilesError
from chemclaw.hazard import Severity, screen_species
from chemclaw.reagents import display_name, known_names, resolve_compound_name


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


# --- TOOL-3: hazard screening ----------------------------------------------------------------


def test_a_documented_incompatible_pair_is_flagged_critical() -> None:
    """Sodium azide in DCM forms diazidomethane — a combination with documented detonations.

    Neither species alone is remarkable in a process lab, which is exactly why a pair table is
    needed and why a per-substance screen would miss it.
    """
    report = screen_species(["NaN3", "DCM"])
    assert report.highest_severity is Severity.CRITICAL
    pair = [f for f in report.findings if "+" in f.subject]
    assert len(pair) == 1
    assert "triazidomethane" in pair[0].hazard
    assert "Substitute the solvent" in pair[0].guidance


def test_the_pair_is_flagged_however_it_is_written_and_ordered() -> None:
    """Order and spelling must not change a safety verdict."""
    for species in (["dichloromethane", "sodium azide"], ["ClCCl", "NaN3"], ["DCM", "NaN3"]):
        assert screen_species(species).highest_severity is Severity.CRITICAL


def test_a_novel_energetic_intermediate_is_caught_by_structure() -> None:
    """A table can only list what someone thought of; SMARTS catch a structure nobody listed.

    This acyl azide is not in the reagent table at all — it is flagged purely on its motif.
    """
    report = screen_species(["CC(=O)N=[N+]=[N-]"])
    hazards = {f.hazard for f in report.findings}
    assert any("azide" in h for h in hazards)
    assert report.highest_severity is Severity.CRITICAL


def test_a_substance_hazard_the_structure_does_not_express_is_caught() -> None:
    """NaH/DMF runaway is a property of the pairing, not of either structure."""
    report = screen_species(["NaH", "DMF"])
    assert any("autocatalytic exotherm" in f.hazard for f in report.findings)


def test_a_peroxide_forming_solvent_is_flagged_as_caution_not_critical() -> None:
    """Severity has to be calibrated, or every answer becomes noise and gets ignored."""
    report = screen_species(["THF"])
    assert report.highest_severity is Severity.CAUTION


def test_an_ordinary_combination_is_not_flagged() -> None:
    """A screen that flags everything is a screen nobody reads."""
    report = screen_species(["EtOAc", "water", "K2CO3"])
    assert report.findings == []
    assert report.highest_severity is None


def test_an_unscreenable_species_is_reported_not_silently_dropped() -> None:
    """A clean report over species the screen never saw would read as a clearance it has not earned.

    This is the honesty property of the whole module: `unresolved` is as load-bearing as `findings`.
    """
    report = screen_species(["EtOAc", "Compound 27b"])
    assert report.findings == []
    assert report.unresolved == ["Compound 27b"]
    assert "Compound 27b" not in report.screened


def test_findings_are_ordered_most_severe_first() -> None:
    """The agent quotes the top of the list, so the ordering is part of the contract."""
    report = screen_species(["NaN3", "DCM", "THF"])
    rank = {"critical": 0, "high": 1, "caution": 2}
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=lambda s: rank[s])


def test_the_screen_is_reachable_as_a_tool() -> None:
    """And returns the same report the module produces."""
    report = _run(screen_hazards(["NaN3", "DCM"]))
    assert report.highest_severity is Severity.CRITICAL


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
