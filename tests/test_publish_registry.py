"""The property registry is coherent, and it is not quietly fragmenting.

**The registry is the one thing standing between this design and EAV.** A foreign key guarantees a
property is *defined*; it does not guarantee it is the *only* definition of that quantity. Three
teams shipping `pka`, `pka_acid` and `pka_conjugate_acid` would each pass the constraint, and every
query would then return a confident subset with nothing raising — which is exactly the failure the
registry was chosen to avoid.

Only review can prevent a synonym being registered. What a test can do is narrow the gap, and these
do: they fail on a registered unit that cannot be converted, on a value written under a kind its
definition forbids, and — the one that matters most — on two properties that share a dimension and
land on the same subject, which is what a split looks like from the outside.
"""

from collections import defaultdict

import pytest

from chemclaw.publish import project as projection
from chemclaw.publish.properties import (
    REGISTRY,
    UNIT_CONVERSIONS,
    UnknownPropertyError,
    definition_for,
    to_canonical,
)


def test_every_canonical_unit_is_reachable_within_its_dimension() -> None:
    """Properties sharing a dimension must agree on a unit, or be convertible to one.

    The check that catches a row shipped with `kcal/mol` under `molar_entropy`. Without it, two
    energies could be registered in different units under one dimension and a query summing them
    would be adding hartree to kilocalories — a mistake that produces a plausible number.
    """
    by_dimension: dict[str, set[str]] = defaultdict(set)
    for definition in REGISTRY.values():
        by_dimension[definition.dimension].add(definition.canonical_unit)

    for dimension, units in sorted(by_dimension.items()):
        if len(units) == 1:
            continue
        # More than one unit under one dimension is allowed only if every pair converts.
        for source in units:
            for target in units:
                assert source == target or (source, target) in UNIT_CONVERSIONS, (
                    f"dimension {dimension!r} registers both {source!r} and {target!r}, but "
                    f"`UNIT_CONVERSIONS` has no path between them — a query over this dimension "
                    "would be comparing incommensurable numbers"
                )


def test_a_dimensionless_property_declares_no_unit() -> None:
    """A unit on a dimensionless quantity is a contradiction that would mislead a reader."""
    for definition in REGISTRY.values():
        if definition.dimension in {"dimensionless", "flag", "category", "count", "similarity"}:
            assert definition.canonical_unit == "", (
                f"{definition.property!r} is {definition.dimension} but declares unit "
                f"{definition.canonical_unit!r}"
            )


def test_an_unregistered_property_is_refused_rather_than_stored() -> None:
    """The registry refuses a name it does not know, naming the fix.

    A value stored under an unregistered name is a value no query will find, so it must be a loud
    failure at write time rather than a row that looks stored.
    """
    with pytest.raises(UnknownPropertyError) as caught:
        definition_for("pKa")
    assert "_DEFINITIONS" in str(caught.value), "the message must say where to add it"


def test_a_unit_with_no_conversion_path_is_refused_rather_than_passed_through() -> None:
    """Canonicalization refuses a unit it cannot convert.

    Passing it through is exactly how a mis-tagged row falls silently out of a range filter: the
    number is stored, the column says it is canonical, and nothing raises.
    """
    with pytest.raises(UnknownPropertyError):
        to_canonical("reaction_delta_g", 1.0, "furlongs")


def test_hartree_converts_to_kilocalories_correctly() -> None:
    """The one conversion the whole schema rests on, checked against the known constant."""
    assert to_canonical("reaction_delta_g", -0.02, "hartree") == pytest.approx(-12.5502, abs=1e-3)
    # A value already in the canonical unit passes through untouched.
    assert to_canonical("reaction_delta_g", -12.5, "kcal/mol") == -12.5


def _projected_properties_by_subject() -> dict[str, set[str]]:
    """Every property each projector emits, keyed by the subject kind it emits them for.

    Derived from the projectors themselves rather than listed here, so a new calculator is covered
    by this check the day it ships rather than the day someone remembers to add it.
    """
    from tests.test_publish_projection import _cases

    found: dict[str, set[str]] = defaultdict(set)
    for kind, calc_type, _, payload in _cases():
        record = projection.project(
            calc_ref=f"{calc_type}@v:a:b",
            calc_type=calc_type,
            payload=payload,
            payload_kind=kind,
        )
        names = {fact.property for fact in record.properties}
        names |= {fact.property for fact in record.sites}
        names |= {fact.property for fact in record.points}
        found[record.subject.kind] |= names
    return found


def test_no_two_properties_of_one_dimension_land_on_the_same_subject() -> None:
    """The fragmentation check: what a split property looks like from the outside.

    If `pka` and `pka_acid` both existed and both described a molecule, this fails — which is the
    only automatic signal available that the registry has grown two names for one quantity.

    **Dimensions where several distinct quantities legitimately coexist are exempted by name**, and
    the exemption list is deliberately short and reasoned: a thermochemistry genuinely establishes
    an enthalpy *and* a Gibbs energy, and a reaction genuinely has a delta-E, a delta-H and a
    delta-G. Those are different quantities, not two spellings of one.
    """
    # Dimensions that carry several genuinely distinct quantities per subject, with why.
    exempt = {
        "energy": "an absolute energy, an enthalpy and a Gibbs energy are three quantities",
        "energy_difference": "a reaction establishes delta-E, delta-H and delta-G together",
        "orbital_energy": "HOMO, LUMO and the gap between them are three readings",
        "count": "a molecule has many independent counts (donors, acceptors, rings)",
        "molar_entropy": "total entropy and the conformational part of it are different terms",
        "fukui": "the three indices describe three different attacks on the same atom",
        "category": "several independent coded facts describe one run",
        "flag": "several independent booleans describe one run",
        "log_unit": "clogp, log_d and pka are different measurements on one molecule",
        "dimensionless": "unrelated normalized quantities share this dimension by definition",
    }
    for subject_kind, names in sorted(_projected_properties_by_subject().items()):
        by_dimension: dict[str, set[str]] = defaultdict(set)
        for name in names:
            by_dimension[REGISTRY[name].dimension].add(name)
        for dimension, sharing in sorted(by_dimension.items()):
            if len(sharing) < 2 or dimension in exempt:
                continue
            pytest.fail(
                f"subject kind {subject_kind!r} carries {sorted(sharing)}, which all share "
                f"dimension {dimension!r}. Either they are one quantity under two names — a "
                "registry split, and the thing this test exists to catch — or the dimension needs "
                "an entry in `exempt` saying why several coexist."
            )


def test_every_exempted_dimension_is_actually_used() -> None:
    """An exemption that no longer applies is a hole nobody is watching.

    Same rule the deferral register follows: a reason that has outlived its subject is deleted, not
    left standing.
    """
    registered = {definition.dimension for definition in REGISTRY.values()}
    exempt = {
        "energy",
        "energy_difference",
        "orbital_energy",
        "count",
        "molar_entropy",
        "fukui",
        "category",
        "flag",
        "log_unit",
        "dimensionless",
    }
    assert exempt <= registered, (
        f"exempted dimension(s) {sorted(exempt - registered)} are no longer registered by any "
        "property; delete the exemption rather than leaving it standing"
    )
