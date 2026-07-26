"""The named-substance incompatibility pairs this branch contributed to the rule table (D-086).

`main` shipped structural hazard screening (D-080) with a citation-carrying SMARTS table and one
generic oxidizer/reductant pair rule. This branch had independently built a parallel screen keyed on
resolved reagent *identity*, whose genuinely additive content was the named pairs below — each safe
apart and dangerous together, and each therefore invisible to a per-substance screen.

Rather than keep two screens, that knowledge moved into `safety/rules.yaml` as SMARTS pair rules.
These tests pin it there, because a rule that silently never fires is worse than no rule: it reports
"no rule matched" for a hazard the table claims to cover.

The azide rule is the cautionary case. Written the obvious way — the X2 form that is correct for an
*organic* azide — it never fired on a **salt**, because RDKit sanitizes the azide anion to two
one-coordinate nitrogens. It was caught only by screening a parsed molecule, which is exactly what
the rule table's own header tells a contributor to do.
"""

import pytest

from safety.screen import screen_reaction


@pytest.mark.parametrize(
    ("label", "components", "expected_rule"),
    [
        ("azide salt in DCM", ["[Na+].[N-]=[N+]=[N-]", "ClCCl"], "azide-with-dichloromethane"),
        ("NaH in DMF", ["[Na+].[H-]", "CN(C)C=O"], "hydride-with-dipolar-aprotic"),
        ("NaH in DMSO", ["[Na+].[H-]", "CS(C)=O"], "hydride-with-dipolar-aprotic"),
        ("peroxide with ketone", ["OO", "CC(C)=O"], "peroxide-with-ketone"),
        (
            "LiAlH4 in DCM",
            ["[Li+].[AlH4-]", "ClCCl"],
            "complex-hydride-with-chlorinated-solvent",
        ),
    ],
)
def test_a_contributed_pair_actually_fires(
    label: str, components: list[str], expected_rule: str
) -> None:
    """Each pair is individually unremarkable and dangerous together — the reason pairs exist."""
    fired = {flag.rule_id for flag in screen_reaction(components).flags}
    assert expected_rule in fired, f"{label}: rule never fired"


@pytest.mark.parametrize(
    "components",
    [
        ["CCOC(C)=O", "O"],  # ethyl acetate + water
        ["Cc1ccccc1", "CCN(CC)CC"],  # toluene + triethylamine
    ],
)
def test_an_ordinary_combination_is_not_flagged(components: list[str]) -> None:
    """A table that flags everything trains people to ignore it — including the real flags."""
    assert screen_reaction(components).flags == []


def test_the_azide_in_an_acceptable_solvent_raises_no_pair_flag() -> None:
    """Swapping dichloromethane for acetonitrile clears the *pair* rule — not the reagent itself.

    Sodium azide in MeCN still carries its own structural flag: it is acutely toxic and liberates
    explosive HN3 on contact with acid, in any solvent. Only the diazidomethane hazard — the one
    the solvent substitution exists to remove — goes away, so this asserts the pair rule is silent
    rather than that the whole screen is. Asserting "no flags at all" here is what let the missing
    azide-salt alert (`non-carbon-azide`) look like correct behavior.
    """
    fired = {flag.rule_id for flag in screen_reaction(["[Na+].[N-]=[N+]=[N-]", "CC#N"]).flags}
    assert "azide-with-dichloromethane" not in fired
    assert fired == {"non-carbon-azide"}


def test_each_component_alone_is_unflagged_by_its_pair_rule() -> None:
    """A pair rule must need *both* sides; firing on one would make the pairing meaningless."""
    for single in (["[Na+].[N-]=[N+]=[N-]"], ["ClCCl"]):
        fired = {flag.rule_id for flag in screen_reaction(single).flags}
        assert "azide-with-dichloromethane" not in fired
