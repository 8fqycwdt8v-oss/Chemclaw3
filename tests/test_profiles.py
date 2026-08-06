"""The named `AgentProfile` seam (config-extensibility item 3, Stage 1).

Proves the seam adds per-use-case agent configuration without weakening anything: the default
profile reproduces today's agent byte-for-byte, a profile *narrows* the advertised tools/MCP and
swaps instructions/harness, an unknown tool name fails loud (fail-fast), and — the load-bearing
invariant — a profile *attenuates but never authorizes*: the audit + authz middleware is attached
regardless of profile. See `docs/archive/audit/10-config-extensibility.md` §6/§8 (Spike 2).
"""

import pytest

from chemclaw.agent.chemclaw_agent import _INSTRUCTIONS, build_agent, connector_tools
from chemclaw.agent.plan_gate import enforce_plan_approval, gate_applies
from chemclaw.agent.profiles import (
    AgentProfile,
    get_profile,
    register_profile,
    registered_profile_names,
)
from chemclaw.core.config import settings


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
    from chemclaw.agent.repeat_guard import refuse_repeated_calls
    from chemclaw.agent.tool_authz import (
        announce_tool_failures,
        enforce_tool_authz,
        refuse_writes_on_dry_run,
    )

    agent = build_agent(
        chat_client=object(),
        profile=AgentProfile(name="tiny", tool_names=frozenset({"predict_pka"})),
    )
    middleware = list(agent.middleware or [])
    # denial + domain-error surfacing + audit + authz + dry-run + repeat guard + announcing
    assert len(middleware) == 7
    assert enforce_tool_authz in middleware
    assert refuse_writes_on_dry_run in middleware
    assert refuse_repeated_calls in middleware
    assert announce_tool_failures in middleware


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
        from chemclaw.agent.profiles import _REGISTRY

        _REGISTRY.pop("probe-profile", None)


def test_a_profiles_harness_answer_is_the_same_one_the_plan_gate_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The builder and the gate must resolve a profile's harness dimensions identically.

    They used to hold three copies of the same `override or default` rule — one in `build_agent`,
    one in `_build_harness_agent`, one in `gate_applies` — and that triplication has cost a live
    defect before: reading `settings` directly instead of the profile let a `plan_only` profile
    under a global `execute` get the gate attached while its approval was never spent, so one human
    decision authorized every later turn (DARK-1). One resolver now answers for all three, and this
    pins the agreement in the direction that matters: a profile that asks for the approval-first
    posture gets the harness *and* the middleware that gates it.
    """
    monkeypatch.setattr(settings, "harness_enabled", False)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    profile = AgentProfile(name="gxp", harness_enabled=True, harness_autonomy="plan_only")

    assert gate_applies(profile), "the deployment default must not decide this for the profile"
    agent = build_agent(chat_client=object(), profile=profile)
    assert "TodoProvider" in {type(p).__name__ for p in agent.context_providers}
    assert enforce_plan_approval in list(agent.middleware or [])


def test_a_harness_profiles_instructions_are_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness path advertises the profile's prompt, as the classic path does.

    It resolved the prompt a second time from the same rule rather than taking the one
    `build_agent` had already resolved — true by duplication, which is the state a docstring
    claiming "pre-resolved by `build_agent`" describes wrongly.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    profile = AgentProfile(name="terse-harness", instructions="Answer tersely.")
    agent = build_agent(chat_client=object(), profile=profile)
    assert "Answer tersely." in agent.default_options["instructions"]
    assert _INSTRUCTIONS not in agent.default_options["instructions"]
