"""The derived species vocabulary, and the one coupling it cannot check for itself.

`chemclaw.science.labels.vocabulary` names the *recorded* roles as plain strings, because
`science/` may import `chemclaw.core` and nothing else (`tests/test_layering.py`) and `Role` lives
in `ingest/`. That is a deliberate, documented duplication with exactly one hazard: a sixth `Role`
member landing without a mapping, which `species_role_from`'s lenient fallback would then turn into
`UNKNOWN` for every species of that role, silently. This file is where that hazard is closed — the
same idiom `tests/test_upstream_surface.py` uses for the shapes upstream never promised.
"""

import pytest

from chemclaw.ingest.eln.ord import Role
from chemclaw.science.labels.policy import LabelPolicy
from chemclaw.science.labels.vocabulary import (
    LabelGroup,
    SpeciesRole,
    recorded_roles,
    species_role_from,
)


def test_the_derived_vocabulary_maps_every_recorded_role() -> None:
    """A `Role` member with no mapping fails here rather than becoming `UNKNOWN` in production.

    Both directions: an unmapped `Role` is the hazard, and a mapping for a role that no longer
    exists is a line nothing reads.
    """
    assert recorded_roles() == {role.value for role in Role}


def test_every_recorded_role_maps_to_a_distinct_derived_role() -> None:
    """The coarse map is injective — it refines, it never merges two recorded roles into one."""
    derived = [species_role_from(role.value) for role in Role]
    assert len(set(derived)) == len(derived)


def test_an_unrecognised_role_is_unknown_rather_than_an_error() -> None:
    """A corpus with one odd role string is indexed with that species unknown, not refused.

    This runs on the ingest path for every species of every reaction; a raise here would let one
    unexpected column value abort a whole drain.
    """
    assert species_role_from("bystander") is SpeciesRole.UNKNOWN


def test_unknown_is_a_member_not_a_none() -> None:
    """`UNKNOWN` ("a labeller looked and could not decide") is not NULL ("nothing has looked").

    Conflating them would make an unlabelled corpus and an unclassifiable one report identical
    coverage, which is the one number a precedent answer is obliged to be honest about.
    """
    assert SpeciesRole.UNKNOWN.value == "unknown"
    assert None not in set(SpeciesRole)


def test_a_policy_may_not_override_a_group_it_does_not_provide() -> None:
    """Overriding an absent group changes nothing and reads as a policy — so it is refused."""
    with pytest.raises(ValueError, match="override"):
        LabelPolicy(override=frozenset({LabelGroup.NAMED_REACTION}))


def test_the_merge_rule_has_one_expression() -> None:
    """`provides` is never a skip: a group the source declares is still derived where it is empty.

    This is the whole of "the database will not have all these labels in the beginning" — Pistachio
    ships NameRxn names for part of its corpus, not all of it, and the rows it left empty must be
    filled rather than trusted as answered.
    """
    policy = LabelPolicy(provides=frozenset({LabelGroup.NAMED_REACTION}))
    assert policy.derives(LabelGroup.NAMED_REACTION, has_value=False) is True
    assert policy.derives(LabelGroup.NAMED_REACTION, has_value=True) is False

    distrusted = LabelPolicy(
        provides=frozenset({LabelGroup.SPECIES_ROLES}),
        override=frozenset({LabelGroup.SPECIES_ROLES}),
    )
    assert distrusted.derives(LabelGroup.SPECIES_ROLES, has_value=True) is True
