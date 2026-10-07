"""Every solvent name the calculation layer accepts resolves to a canonical id.

`ALPB_SOLVENTS` has several names per solvent (`thf`/`tetrahydrofuran`, the hexane spellings) and
the name reaches the calculation key verbatim, so a store keeping it as given would answer "every
reaction in THF" with a subset. The first test catches an upstream name with no group here.
"""

import pytest

from chemclaw.publish.solvents import canonical_solvent, display_name, known_solvents
from chemclaw.science.calc.solvents import ALPB_SOLVENTS, SUGGESTED_SOLVENTS


def test_every_upstream_solvent_name_resolves_to_a_known_group() -> None:
    """Every upstream solvent name resolves to a known group.

    Otherwise a new `ALPB_SOLVENTS` name would pass through `canonical_solvent` as an undeclared
    one-member group, invisible to queries over its real siblings.
    """
    groups = known_solvents()
    unmapped = sorted(name for name in ALPB_SOLVENTS if canonical_solvent(name) not in groups)
    assert not unmapped, (
        f"{unmapped} are accepted by the calculation layer but belong to no group in "
        "`chemclaw.publish.solvents._GROUPS`. Add each to the group of the solvent it names, or "
        "give it its own — a name with no group is a solvent no cross-solvent query can find."
    )


def test_every_declared_alias_is_a_name_the_calculator_accepts() -> None:
    """An alias for a name the calculator rejects is a mapping nothing can ever use.

    The other direction of the same parity. It keeps the group table from accumulating spellings
    that were guessed at rather than observed.
    """
    declared = {alias for aliases in known_solvents().values() for alias in aliases}
    invented = sorted(declared - set(ALPB_SOLVENTS))
    assert not invented, (
        f"{invented} are declared as aliases but no calculator accepts them; delete them rather "
        "than keeping a mapping that can never fire"
    )


def test_the_canonical_spelling_is_the_one_the_system_already_suggests() -> None:
    """The canonical spelling is the one `SUGGESTED_SOLVENTS` already uses.

    So a chemist sees one name for one solvent across refusals and results.
    """
    groups = known_solvents()
    for suggested in SUGGESTED_SOLVENTS:
        assert suggested in groups, (
            f"{suggested!r} is the spelling this system suggests to chemists, but it is an alias "
            f"here rather than the canonical id (it resolves to {canonical_solvent(suggested)!r})"
        )


@pytest.mark.parametrize(
    ("spellings", "expected"),
    [
        (("thf", "THF", " Tetrahydrofuran ", "tetrahydrofuran"), "thf"),
        (("ch2cl2", "dichloromethane", "dichlormethane", "methylenechloride"), "ch2cl2"),
        (("hexane", "n-hexane", "nhexane", "n-hexan", "nhexan"), "hexane"),
        (("water", "h2o", "WATER"), "water"),
        (("acetonitrile", "mecn"), "acetonitrile"),
    ],
)
def test_spellings_of_one_solvent_collapse(spellings: tuple[str, ...], expected: str) -> None:
    """Spellings of one solvent collapse to one id.

    Normalized as the calculation layer normalizes (stripped, lowercased), so capitalization cannot
    fork it.
    """
    assert {canonical_solvent(name) for name in spellings} == {expected}


def test_dry_and_water_saturated_octanol_stay_distinct() -> None:
    """Two solvents, not two spellings — and merging them would be silent.

    They have different dielectrics and are the two halves of a partition coefficient, so a group
    that folded them together would combine incomparable calculations under one id.
    """
    assert canonical_solvent("octanol") != canonical_solvent("woctanol")


def test_gas_phase_is_absence_rather_than_a_solvent() -> None:
    """No solvent is a real state, and it must not become an empty-string solvent id.

    A `solvent_id` of `''` would be a row in the solvent table meaning "none", which is exactly the
    sentinel the schema avoids by letting the column be NULL.
    """
    assert canonical_solvent(None) is None
    assert canonical_solvent("") is None
    assert canonical_solvent("   ") is None


def test_an_unknown_solvent_is_published_rather_than_refused() -> None:
    """An unknown solvent is published normalized under its own id rather than refused.

    Refusing would lose a finished calculation to protect a lookup table.
    """
    assert canonical_solvent("  SuperCriticalCO2 ") == "supercriticalco2"


def test_every_group_has_a_readable_name() -> None:
    """A canonical id is a short key; the display name is what a person reads in a report."""
    for canonical in known_solvents():
        assert display_name(canonical), f"{canonical!r} has no display name"
