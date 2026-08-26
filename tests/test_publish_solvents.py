"""Every solvent name the calculation layer accepts resolves to a canonical id.

**The gap this closes is measured, not hypothetical.** `ALPB_SOLVENTS` holds 42 names for 25
solvents — `thf` and `tetrahydrofuran`, `hexane`/`n-hexane`/`nhexane`/`n-hexan`/`nhexan`,
`ch2cl2`/`dichloromethane`/`dichlormethane`/`methylenechloride` — and that name reaches the
calculation key verbatim. A published schema storing it as given would answer "every reaction we
ran in THF" with a confident subset of the truth and raise nothing.

The first test is the one that matters over time: a name added upstream that this module has no
group for would quietly become its own solvent, so it is caught here rather than in a query that
under-returns six months later.
"""

import pytest

from chemclaw.publish.solvents import canonical_solvent, display_name, known_solvents
from chemclaw.science.calc.solvents import ALPB_SOLVENTS, SUGGESTED_SOLVENTS


def test_every_upstream_solvent_name_resolves_to_a_known_group() -> None:
    """A name the calculator accepts must reach a canonical id this module knows.

    Without this, a solvent added to `ALPB_SOLVENTS` upstream would pass through
    `canonical_solvent` as itself — a new one-member group nobody declared — and calculations run
    in it would be invisible to every query grouping by its real siblings.
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
    """Where the calculation layer already has a preferred spelling, this module uses it.

    `SUGGESTED_SOLVENTS` is what a refusal message quotes, so a chemist who is told to write `thf`
    and then sees `tetrahydrofuran` in a results table has been given two names for one thing by
    one system.
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
    """The specific collisions that made this module necessary.

    Normalized the way the calculation layer normalizes — stripped and lowercased — because it
    matches that way, and a publish path that canonicalized differently would fork on capitalization
    alone.
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
    """A solvent this registry has not heard of still reaches the record.

    Refusing to publish a finished calculation because its solvent is unfamiliar would lose science
    to protect a lookup table. It lands normalized, as its own id, which reads correctly as "a
    solvent we have no alias group for".
    """
    assert canonical_solvent("  SuperCriticalCO2 ") == "supercriticalco2"


def test_every_group_has_a_readable_name() -> None:
    """A canonical id is a short key; the display name is what a person reads in a report."""
    for canonical in known_solvents():
        assert display_name(canonical), f"{canonical!r} has no display name"
