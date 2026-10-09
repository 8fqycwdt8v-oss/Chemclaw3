"""The named `AgentProfile` seam.

The default profile reproduces the default agent; a profile narrows the advertised tools and
swaps instructions and harness; an unknown tool name fails loudly; and a profile attenuates but
never authorizes: audit and authz middleware attach regardless.
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

    `tool_names` spans both halves: `gather_evidence` is in-process and the two predictors are
    `calc`'s, so the in-process tools are narrowed and `calc` attaches with its allow-list cut to
    two.
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
    """Narrowing a profile never removes the audit and per-tool authz middleware.

    A narrowing profile carries one more entry, the undeclared-write refusal, attached exactly when
    `tool_names is not None`. Asserted by name so swapping one entry for another fails.
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
        # Outermost of what rewrites a result: the handle line lies outside the envelope.
        "stamp_result_handles",
        # Inside both converters and outside the trail
        # (`D-2026-08-27-a-tool-result-crosses-a-boundary-and-must-say-so`): every out-of-process
        # result is framed as data, and a refusal this system composed is not.
        "frame_connector_results",
        # Inside the framing so the envelope wraps a bounded payload, and outside the trail so the
        # audit row still records what the tool returned (`agent/tool_result_size.py`).
        "bound_tool_results",
        "announce_tool_failures",
        "object",  # the audit middleware, a stand-in here
        "refuse_undeclared_writes",
        "enforce_tool_authz",
        "refuse_writes_on_dry_run",
        "refuse_repeated_calls",
        # Innermost of the deciding gates: a mis-serialised call is promoted onto `tool_calls` so it
        # reaches this chain, and everything above must see it before it is refused.
        "refuse_unparsed_arguments",
        # The harness pair, on by default. Both nest inside the guard above:
        # `refuse_unparsed_arguments` raises before its handler, so arguments that did not parse
        # never reach the plan gate. Pinned as a relation in `tests/test_invalid_tool_calls.py`.
        "enforce_plan_approval",
        "stamp_plan_link",
        # Innermost of all: the session-ownership check next to the effect.
        "refuse_when_claim_lost",
    ]
    assert enforce_tool_authz in middleware
    assert refuse_writes_on_dry_run in middleware
    assert refuse_repeated_calls in middleware
    assert announce_tool_failures in middleware
    # The default agent keeps the chain it had: the extra entry is the narrowing's, not everyone's.
    # Expressed as a difference rather than as a literal, because the literal it used to be (7) is
    # a count of the whole chain and went stale the first time the chain grew.
    assert len(middleware) - len(tool_call_middleware(object(), AgentProfile(name="wide"))) == 1, (
        "the narrowing must add exactly one entry, and only for a profile that narrows"
    )


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
    """The builder and the plan gate resolve a profile's harness dimensions identically.

    One resolver serves `build_agent`, `_build_harness_agent` and `gate_applies`, so a profile
    asking for `plan_only` gets both the harness and the gate whose approval is spent.
    """
    monkeypatch.setattr(settings, "harness_enabled", False)
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    profile = AgentProfile(name="governed", harness_enabled=True, harness_autonomy="plan_only")

    from chemclaw.agent.langgraph_agent import tool_call_middleware

    assert gate_applies(profile), "the deployment default must not decide this for the profile"
    assert harness_enabled_for(profile), "the profile's harness override lost to the default"
    assert enforce_plan_approval in tool_call_middleware(object(), profile)


def test_a_harness_profiles_instructions_are_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness path advertises the profile's prompt, the one `build_agent` already resolved."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    profile = AgentProfile(name="terse-harness", instructions="Answer tersely.")
    agent = surface(profile)
    assert "Answer tersely." in agent.instructions
    assert _INSTRUCTIONS not in agent.instructions
