"""The named `AgentProfile` seam (config-extensibility item 3, Stage 1).

Proves the seam adds per-use-case agent configuration without weakening anything: the default
profile reproduces today's agent byte-for-byte, a profile *narrows* the advertised tools/MCP and
swaps instructions/harness, an unknown tool name fails loud (fail-fast), and — the load-bearing
invariant — a profile *attenuates but never authorizes*: the audit + authz middleware is attached
regardless of profile. See `docs/archive/audit/10-config-extensibility.md` §6/§8 (Spike 2).
"""

import pytest

from chemclaw.agent.chemclaw_agent import _INSTRUCTIONS, connector_specs
from chemclaw.agent.plan_gate import (
    enforce_plan_approval,
    gate_applies,
    harness_enabled_for,
)
from chemclaw.agent.profiles import (
    AgentProfile,
    get_profile,
    register_profile,
    registered_profile_names,
)
from chemclaw.core.config import settings
from tests.surface import surface


def test_default_profile_reproduces_todays_agent() -> None:
    """`surface()` and `surface("default")` advertise the identical thing."""
    base = surface(None)
    default = surface("default")
    assert default.instructions == base.instructions
    assert default.instructions == _INSTRUCTIONS
    assert default.tool_names == base.tool_names
    assert {t.name for t in default.connectors} == {t.name for t in base.connectors}
    # And the default profile's connector set is every enabled connector, as the global agent's is.
    assert {tool.name for tool in connector_specs()} == {
        tool.name for tool in connector_specs("default")
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
    agent = surface(profile)
    assert agent.tool_names == {"gather_evidence"}
    assert agent.instructions != _INSTRUCTIONS

    connectors = connector_specs(profile)
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
    assert {tool.name for tool in connector_specs(profile)} == {"molfp"}


def test_profile_attenuates_but_audit_and_authz_always_attach() -> None:
    """The invariant: narrowing a profile never removes the audit + per-tool authz middleware.

    A narrowing profile carries **one more** than the default agent's seven, not fewer: the
    undeclared-write refusal is attached exactly when `tool_names is not None`, because that is the
    only case in which a tool can be missing from the graph on purpose rather than by mistake
    (D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only). Asserted by name rather than by
    count alone, so a future change that swapped one entry for another cannot keep this green.
    """
    from chemclaw.agent.langgraph_agent import tool_call_middleware
    from chemclaw.agent.repeat_guard import refuse_repeated_calls
    from chemclaw.agent.tool_authz import (
        announce_tool_failures,
        enforce_tool_authz,
        refuse_writes_on_dry_run,
    )

    profile = AgentProfile(name="tiny", tool_names=frozenset({"predict_pka"}))
    middleware = tool_call_middleware(object(), profile)
    assert [type(entry).__name__ for entry in middleware] == [
        "surface_authorization_denials",
        "surface_domain_errors",
        "announce_tool_failures",
        "object",  # the audit middleware, a stand-in here
        "refuse_undeclared_writes",
        "enforce_tool_authz",
        "refuse_writes_on_dry_run",
        "refuse_repeated_calls",
    ]
    assert enforce_tool_authz in middleware
    assert refuse_writes_on_dry_run in middleware
    assert refuse_repeated_calls in middleware
    assert announce_tool_failures in middleware
    # The default agent keeps the chain it had: the extra entry is the narrowing's, not everyone's.
    assert len(tool_call_middleware(object(), AgentProfile(name="wide"))) == 7


def test_unknown_tool_name_in_profile_fails_loud() -> None:
    """A profile naming a tool nothing provides is a build-time error, not a silent empty set."""
    with pytest.raises(ValueError, match="unknown tool"):
        surface(AgentProfile(name="typo", tool_names=frozenset({"predict_pkaa"})))


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

    from chemclaw.agent.langgraph_agent import tool_call_middleware

    assert gate_applies(profile), "the deployment default must not decide this for the profile"
    assert harness_enabled_for(profile), "the profile's harness override lost to the default"
    assert enforce_plan_approval in tool_call_middleware(object(), profile)


def test_a_harness_profiles_instructions_are_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness path advertises the profile's prompt, as the classic path does.

    It resolved the prompt a second time from the same rule rather than taking the one
    `build_agent` had already resolved — true by duplication, which is the state a docstring
    claiming "pre-resolved by `build_agent`" describes wrongly.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    profile = AgentProfile(name="terse-harness", instructions="Answer tersely.")
    agent = surface(profile)
    assert "Answer tersely." in agent.instructions
    assert _INSTRUCTIONS not in agent.instructions
