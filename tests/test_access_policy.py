"""The access policy grants by role, from a file, and names only what the deployment has."""

from pathlib import Path

import pytest

from chemclaw.agent.authz import AuthorizationError, authorize_tool
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.api.access_check import refuse_an_unknown_access_policy
from chemclaw.core.access_policy import AccessPolicyError, active_policy, parse_policy
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

SHIPPED = Path(__file__).resolve().parent.parent / "data" / "access" / "roles.yaml"

POLICY = """
roles:
  reader:
    tools: [tier:read]
    sources: [graph]
    profiles: [default]
  cook:
    tools: [tier:read, tier:write, connector:kitchen]
    deny: [burn_it]
    sources: ["*"]
    profiles: ["*"]
  boss:
    tools: ["*"]
    admin: true
"""


def test_a_new_role_is_a_new_block_and_grants_are_unions() -> None:
    policy = parse_policy(POLICY + "  intern:\n    tools: [look]\n")
    assert policy.permits_tool({"intern"}, "look", connector=None, writes=False)
    assert not policy.permits_tool({"intern"}, "cut", connector=None, writes=False)
    assert policy.permits_tool({"intern", "reader"}, "look", connector=None, writes=False)


def test_tiers_connectors_and_deny() -> None:
    policy = parse_policy(POLICY)
    assert policy.permits_tool({"reader"}, "find", connector=None, writes=False)
    assert not policy.permits_tool({"reader"}, "save", connector=None, writes=True)
    assert policy.permits_tool({"cook"}, "save", connector=None, writes=True)
    assert not policy.permits_tool({"cook"}, "burn_it", connector="kitchen", writes=True)
    # A deny subtracts from its own block, not from another role the person holds.
    assert policy.permits_tool({"cook", "boss"}, "burn_it", connector="kitchen", writes=True)


def test_unlisted_roles_hold_nothing() -> None:
    policy = parse_policy(POLICY)
    assert not policy.permits_tool({"stranger"}, "find", connector=None, writes=False)
    assert not policy.permits_source({"stranger"}, "graph")
    assert not policy.permits_profile(set(), "default")
    assert not policy.is_admin({"cook"}) and policy.is_admin({"boss"})


def test_sources_and_profiles_by_role() -> None:
    policy = parse_policy(POLICY)
    assert policy.permits_source({"reader"}, "graph")
    assert not policy.permits_source({"reader"}, "eln-json")
    assert policy.permits_source({"cook"}, "eln-json")
    assert not policy.permits_profile({"reader"}, "design")


@pytest.mark.parametrize(
    "text", ["roles: {a: {tools: ['tier:writes']}}", "roles: {a: {toools: []}}", "{}", "["]
)
def test_a_malformed_policy_is_refused(text: str) -> None:
    with pytest.raises(AccessPolicyError):
        parse_policy(text)


def test_unknown_references_are_reported() -> None:
    policy = parse_policy(
        "roles: {a: {tools: [nope, 'connector:ghost'], sources: [x], profiles: [y]}}"
    )
    problems = policy.unknown_references(tools=[], connectors=[], sources=[], profiles=[])
    assert len(problems) == 4


def test_the_shipped_policy_names_only_what_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "access_policy_file", str(SHIPPED))
    load_profiles()
    refuse_an_unknown_access_policy()


def test_authorize_tool_follows_the_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy_file = tmp_path / "roles.yaml"
    policy_file.write_text(POLICY)
    monkeypatch.setattr(settings, "access_policy_file", str(policy_file))
    monkeypatch.setattr(settings, "entra_required", True)
    assert active_policy() is not None
    tokens = set_current_identity("u1", frozenset({"reader"}))
    try:
        authorize_tool("gather_evidence")
        with pytest.raises(AuthorizationError):
            authorize_tool("record_knowledge_note")
    finally:
        reset_current_identity(tokens)
    tokens = set_current_identity("u2", frozenset({"cook"}))
    try:
        authorize_tool("record_knowledge_note")
    finally:
        reset_current_identity(tokens)
