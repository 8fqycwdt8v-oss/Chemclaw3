"""A profile may not spell `harness_autonomy` wrong and quietly lose the plan gate.

An explicit value overrides the deployment default, so a typo would remove the inherited
`plan_only` gate while `TodoListMiddleware` keeps running, and the harness would look plan-gated
while state-changing tools execute. The tests assert the property (bad refused, good kept, absent
inherits), not the annotation.
"""

import pytest
from pydantic import ValidationError

from chemclaw.agent.plan_gate import PLAN_ONLY, autonomy_for, gate_applies
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.config.agent import AgentSettings, HarnessAutonomy

# Pydantic's message for a missed `Literal` arm, up to the value it was handed. Quoted rather than
# derived from `get_args(HarnessAutonomy)`, which would agree with whatever the field says.
_LITERAL_ERROR = (
    r"harness_autonomy\n  Input should be 'plan_only' or 'execute' \[type=literal_error"
)


@pytest.mark.parametrize(
    ("value", "fires"),
    [
        # the hyphen: the exact spelling that shipped this defect
        ("plan-only", _LITERAL_ERROR + r", input_value='plan-only'"),
        ("plan only", _LITERAL_ERROR + r", input_value='plan only'"),
        ("planonly", _LITERAL_ERROR + r", input_value='planonly'"),
        # case matters; the comparison in plan_gate is exact
        ("PLAN_ONLY", _LITERAL_ERROR + r", input_value='PLAN_ONLY'"),
        ("Execute", _LITERAL_ERROR + r", input_value='Execute'"),
        ("", _LITERAL_ERROR + r", input_value=''"),
        ("yes", _LITERAL_ERROR + r", input_value='yes'"),
    ],
)
def test_a_misspelled_autonomy_value_is_refused(value: str, fires: str) -> None:
    """A bad value is refused by the autonomy field, naming the value it was handed.

    `match=` pins which field refused; a bare `pytest.raises(ValidationError)` would pass on any
    validation failure, including one from an unrelated field.
    """
    with pytest.raises(ValidationError, match=fires):
        # Deliberately outside the Literal — mypy is right to object, and that it objects is half
        # the point: the annotation now rejects statically what pydantic rejects at runtime. A
        # profile arrives from a YAML file, where neither check has run, which is why both matter.
        AgentProfile(name="typo", harness_autonomy=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["plan_only", "execute"])
def test_the_two_real_values_are_accepted(value: HarnessAutonomy) -> None:
    assert AgentProfile(name="ok", harness_autonomy=value).harness_autonomy == value


def test_an_unset_autonomy_still_inherits_the_deployment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inheritance path is what a typo used to break, so pin it explicitly."""
    monkeypatch.setattr(settings, "harness_autonomy", PLAN_ONLY)
    monkeypatch.setattr(settings, "harness_enabled", True)
    profile = AgentProfile(name="inherits")
    assert profile.harness_autonomy is None
    assert autonomy_for(profile) == PLAN_ONLY
    assert gate_applies(profile) is True


def test_an_explicit_plan_only_profile_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case a typo silently converted into an ungated one."""
    monkeypatch.setattr(settings, "harness_autonomy", "execute")
    monkeypatch.setattr(settings, "harness_enabled", True)
    profile = AgentProfile(name="explicit", harness_autonomy=PLAN_ONLY)
    assert autonomy_for(profile) == PLAN_ONLY
    assert gate_applies(profile) is True


def test_the_settings_field_and_the_profile_field_accept_the_same_set() -> None:
    """The settings field and the profile field accept the same set.

    They share one alias; comparing resolved annotations keeps them from drifting apart.
    """
    from typing import get_args, get_type_hints

    settings_values = set(get_args(get_type_hints(AgentSettings)["harness_autonomy"]))
    # The profile's is `HarnessAutonomy | None`; drop the None arm before comparing.
    profile_arg = get_type_hints(AgentProfile)["harness_autonomy"]
    profile_values = {a for arm in get_args(profile_arg) for a in get_args(arm)}
    assert settings_values == profile_values, (
        f"settings accepts {sorted(settings_values)} but a profile accepts "
        f"{sorted(profile_values)}; the two must be the same set or a profile can ask for an "
        f"autonomy the deployment would refuse"
    )
