"""The named `AgentProfile` seam (config-extensibility item 3, Stage 1).

Proves the seam adds per-use-case agent configuration without weakening anything: the default
profile reproduces today's agent byte-for-byte, a profile *narrows* the advertised tools/MCP and
swaps instructions/harness, an unknown tool name fails loud (fail-fast), and — the load-bearing
invariant — a profile *attenuates but never authorizes*: the audit + authz middleware is attached
regardless of profile. See `docs/audit/10-config-extensibility.md` §6/§8 (Spike 2).
"""

import pytest

from agents.chemclaw_agent import _INSTRUCTIONS, build_agent, connector_tools
from agents.profiles import (
    AgentProfile,
    get_profile,
    register_profile,
    registered_profile_names,
)
from chemclaw.config import settings


def test_default_profile_reproduces_todays_agent() -> None:
    """`build_agent()` and `build_agent(profile="default")` build the identical agent surface."""
    base = build_agent(chat_client=object())
    default = build_agent(chat_client=object(), profile="default")
    assert default.default_options["instructions"] == base.default_options["instructions"]
    assert default.default_options["instructions"] == _INSTRUCTIONS
    assert {t.name for t in default.default_options["tools"]} == {
        t.name for t in base.default_options["tools"]
    }
    assert {t.name for t in default.mcp_tools} == {t.name for t in base.mcp_tools}
    # And the default profile's connector set is every enabled connector, as the global agent's is.
    assert {tool.name for tool in connector_tools()} == {
        tool.name for tool in connector_tools("default")
    }


def test_profile_narrows_tools_and_swaps_instructions() -> None:
    """A profile advertises only its named tool subset and its own instructions.

    `tool_names` spans both halves of the surface, which is what makes a profile expressible at
    all now that the domain capabilities live behind connectors: `gather_evidence` is in-process,
    the two predictors are the `calc` connector's, and a profile naming all three must get exactly
    those — the in-process tools narrowed, and `calc` attached with its allow-list cut to two.
    """
    profile = AgentProfile(
        name="property-lookup",
        instructions="Answer physical-property questions tersely; cite computed values.",
        tool_names=frozenset({"predict_pka", "predict_solubility", "gather_evidence"}),
    )
    agent = build_agent(chat_client=object(), profile=profile)
    assert {t.name for t in agent.default_options["tools"]} == {"gather_evidence"}
    assert agent.default_options["instructions"] != _INSTRUCTIONS

    connectors = connector_tools(profile)
    assert [connector.name for connector in connectors] == ["calc"]
    assert set(connectors[0].allowed_tools or ()) == {"predict_pka", "predict_solubility"}
    # Every other connector is dropped rather than attached with an empty surface.
    assert "chem" not in {connector.name for connector in connectors}


def test_profile_can_narrow_connectors() -> None:
    """`mcp_server_names` narrows the turn's connectors to the named subset.

    Narrowing moved with the connectors themselves: they are built per turn rather than attached to
    the agent, so the profile is applied where the set is built (`connector_tools`).
    """
    profile = AgentProfile(name="mol-only", mcp_server_names=frozenset({"molfp"}))
    assert {tool.name for tool in connector_tools(profile)} == {"molfp"}


def test_profile_attenuates_but_audit_and_authz_always_attach() -> None:
    """The invariant: narrowing a profile never removes the audit + per-tool authz middleware."""
    from agents.tool_authz import enforce_tool_authz

    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(name="tiny", tool_names=frozenset({"predict_pka"})),
    )
    middleware = list(agent.middleware or [])
    assert len(middleware) == 2  # audit + authz, unchanged by the profile
    assert enforce_tool_authz in middleware


def test_unknown_tool_name_in_profile_fails_loud() -> None:
    """A profile naming a tool nothing provides is a build-time error, not a silent empty set."""
    with pytest.raises(ValueError, match="unknown tool"):
        build_agent(
            chat_client=object(),
            profile=AgentProfile(name="typo", tool_names=frozenset({"predict_pkaa"})),
        )


def test_profile_overrides_harness_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile can flip the harness on even when the global default keeps it off."""
    monkeypatch.setattr(settings, "harness_enabled", False)
    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(name="autonomous", harness_enabled=True, harness_autonomy="execute"),
    )
    provider_types = {type(p).__name__ for p in agent.context_providers}
    assert "TodoProvider" in provider_types  # the harness path was taken despite the global default


def test_get_profile_resolution_and_registration() -> None:
    """`None` resolves to default; an unknown name raises with valid keys; registration works."""
    assert get_profile(None).name == "default"
    with pytest.raises(ValueError, match="known:"):
        get_profile("nope")

    register_profile(AgentProfile(name="probe-profile"))
    try:
        assert "probe-profile" in registered_profile_names()
        with pytest.raises(ValueError, match="already registered"):
            register_profile(AgentProfile(name="probe-profile"))
    finally:
        from agents.profiles import _REGISTRY

        _REGISTRY.pop("probe-profile", None)
