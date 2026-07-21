"""The `Principal` identity model (Phase-6, plan steps 6.1–6.3).

Proves the small, pure contract the rest of Phase 6 builds on: the audit actor is the Entra
`oid`, roles/groups default empty, and the identity is immutable once validated.
"""

import pytest
from pydantic import ValidationError

from chemclaw.identity import Principal


def test_actor_is_the_entra_object_id() -> None:
    """The audit trail attributes actions to the stable `oid`, not the renameable UPN."""
    principal = Principal(oid="00000000-0000-0000-0000-000000000001", upn="chemist@example.com")
    assert principal.actor == "00000000-0000-0000-0000-000000000001"


def test_roles_and_groups_default_empty() -> None:
    """An identity with no claims carries no authority — empty roles and groups."""
    principal = Principal(oid="u1")
    assert principal.roles == frozenset()
    assert principal.groups == frozenset()


def test_principal_is_immutable() -> None:
    """A validated identity cannot be mutated (no acting-as swap after the fact)."""
    principal = Principal(oid="u1", roles=frozenset({"lab"}))
    with pytest.raises(ValidationError):
        principal.oid = "u2"
