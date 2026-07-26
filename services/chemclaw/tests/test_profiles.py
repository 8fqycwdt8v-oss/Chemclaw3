"""The named `AgentProfile` seam (config-extensibility item 3, Stage 1).

Proves the seam adds per-use-case agent configuration without weakening anything: the default
profile reproduces today's agent byte-for-byte, a profile *narrows* the advertised tools/MCP and
swaps instructions/harness, an unknown tool name fails loud (fail-fast), and — the load-bearing
invariant — a profile *attenuates but never authorizes*: the audit + authz middleware is attached
regardless of profile. See `docs/audit/10-config-extensibility.md` §6/§8 (Spike 2).
"""

import pytest

from agents.chemclaw_agent import _INSTRUCTIONS, build_agent
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


def test_profile_narrows_tools_and_swaps_instructions() -> None:
    """A profile advertises only its named tool subset, across both transports.

    The profile names three capabilities; two of them (`predict_pka`,
    `predict_solubility`) live on `mcp-calc` since X8 and one is in-process. A profile is
    a statement about *capabilities*, not about which process hosts them, so naming a
    calculator must keep working — and the attenuation must be exact: the server is
    attached with its allowed set intersected down to the two asked for, not with all
    seven it can serve.
    """
    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(
            name="property-lookup",
            instructions="Answer physical-property questions tersely; cite computed values.",
            tool_names=frozenset({"predict_pka", "predict_solubility", "gather_evidence"}),
        ),
    )
    assert {t.name for t in agent.default_options["tools"]} == {"gather_evidence"}
    assert {t.name for t in agent.mcp_tools} == {"mcp-calc"}
    allowed = agent.mcp_tools[0].allowed_tools
    assert allowed is not None
    assert set(allowed) == {"predict_pka", "predict_solubility"}
    assert agent.default_options["instructions"] != _INSTRUCTIONS


def test_narrowing_to_one_tool_does_not_attach_the_rest_of_its_server() -> None:
    """Naming one tool of a server grants that tool, never the server.

    The failure this guards is silent widening: `mcp-calc` serves seven calculators, and a
    profile scoped to one of them getting all seven would be an attenuation mechanism that
    quietly does not attenuate. Servers with nothing asked for are not attached at all.
    """
    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(name="pka-only", tool_names=frozenset({"predict_pka"})),
    )
    assert {t.name for t in agent.mcp_tools} == {"mcp-calc"}
    allowed = agent.mcp_tools[0].allowed_tools
    assert allowed is not None
    assert list(allowed) == ["predict_pka"]
    assert agent.default_options["tools"] == []


def test_profile_can_narrow_mcp_servers() -> None:
    """`mcp_server_names` narrows the attached MCP capability servers to the named subset."""
    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(name="mol-only", mcp_server_names=frozenset({"mcp-molfp"})),
    )
    assert {t.name for t in agent.mcp_tools} == {"mcp-molfp"}


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
