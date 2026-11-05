"""A background job's roles are trusted only when signed; tools a role lacks are not bound."""

from pathlib import Path
from unittest import mock

import pytest
from pydantic import SecretStr

from chemclaw.agent.langgraph_agent import bindable_capability_tools
from chemclaw.agent.profiles import DEFAULT_PROFILE
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_roles,
    reset_current_identity,
    set_current_identity,
)
from chemclaw.core.role_claim import sign_roles, verified_roles
from chemclaw.durable import report_workflow
from chemclaw.retrieval.harness import ReportSection, SectionRequest, SynthesizedSection

POLICY = """
roles:
  reader:
    tools: [tier:read]
  cook:
    tools: [tier:read, tier:write]
"""


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "role_claim_secret", SecretStr("k" * 32))


def test_a_claim_verifies_only_for_its_actor_and_roles(keyed: None) -> None:
    claim = sign_roles("alice", ["cook", "reader"])
    assert verified_roles("alice", ["reader", "cook"], claim) == {"cook", "reader"}
    assert verified_roles("bob", ["cook", "reader"], claim) == frozenset()
    assert verified_roles("alice", ["cook", "reader", "admin"], claim) == frozenset()
    assert verified_roles("alice", ["cook"], "") == frozenset()


def test_without_a_key_nothing_is_signed_or_trusted() -> None:
    assert sign_roles("alice", ["cook"]) == ""
    assert verified_roles("alice", ["cook"], "0" * 64) == frozenset()


async def test_a_report_section_binds_the_roles_it_was_signed_with(keyed: None) -> None:
    seen: list[frozenset[str]] = []

    async def _record(section: ReportSection, retrievers: object) -> SynthesizedSection:
        seen.append(get_current_roles())
        return SynthesizedSection(
            heading=section.heading, memory_layer=section.memory_layer, evidence=[]
        )

    section = ReportSection(heading="Scope", query="what is known", memory_layer="evidence")
    with mock.patch.object(report_workflow, "gather_section", _record):
        for roles, claim in (
            (["cook"], sign_roles("alice", ["cook"])),
            (["admin"], sign_roles("alice", ["cook"])),
            (["cook"], ""),
        ):
            await report_workflow.retrieve_section(
                SectionRequest(
                    section=section, requested_by="alice", requested_roles=roles, roles_claim=claim
                )
            )
    assert seen == [frozenset({"cook"}), frozenset(), frozenset()]


def test_a_tool_the_roles_do_not_hold_is_not_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy_file = tmp_path / "roles.yaml"
    policy_file.write_text(POLICY)
    monkeypatch.setattr(settings, "access_policy_file", str(policy_file))
    monkeypatch.setattr(settings, "entra_required", True)

    def bound(roles: set[str]) -> set[str]:
        tokens = set_current_identity("u", frozenset(roles))
        try:
            return {fn.__name__ for fn in bindable_capability_tools(DEFAULT_PROFILE)}
        finally:
            reset_current_identity(tokens)

    reader, cook, nobody = bound({"reader"}), bound({"cook"}), bound({"stranger"})
    assert "gather_evidence" in reader
    assert "record_knowledge_note" not in reader
    assert "record_knowledge_note" in cook
    assert nobody == set()


def test_without_a_policy_every_tool_is_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "entra_required", True)
    tokens = set_current_identity("u", frozenset())
    try:
        assert "record_knowledge_note" in {
            fn.__name__ for fn in bindable_capability_tools(DEFAULT_PROFILE)
        }
    finally:
        reset_current_identity(tokens)
