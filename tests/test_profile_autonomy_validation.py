"""A profile may not spell `harness_autonomy` wrong and quietly lose the plan gate.

`AgentSettings.harness_autonomy` has always been a `Literal`, so the environment variable is
refused at startup when misspelled. `AgentProfile.harness_autonomy` was a bare `str`, so the file
was not — and the failure is not symmetrical with "the gate is not added". `autonomy_for` falls
back to the deployment default only when the profile's value is `None`, so an explicit typo
*removes* the gate the profile would otherwise have inherited from the shipped `plan_only` default,
while `TodoListMiddleware` keeps running. The harness then looks plan-gated and is not:
`GET /plan` answers `approved=false` while state-changing tools execute.

These tests assert the property (a bad value is refused, a good one is kept, an absent one
inherits) rather than the annotation, so a future refactor that widens the field fails here.
"""

import pytest
from pydantic import ValidationError

from chemclaw.agent.plan_gate import PLAN_ONLY, autonomy_for, gate_applies
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.config import settings
from chemclaw.core.config.agent import AgentSettings, HarnessAutonomy


@pytest.mark.parametrize(
    "value",
    [
        "plan-only",  # the hyphen: the exact spelling that shipped this defect
        "plan only",
        "planonly",
        "PLAN_ONLY",  # case matters; the comparison in plan_gate is exact
        "Execute",
        "",
        "yes",
    ],
)
def test_a_misspelled_autonomy_value_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
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
    """The two are one alias now; this fails if someone re-spells either one.

    The defect was not that the profile lacked validation in principle — it was that the constraint
    existed in one of the two places that needed it, so the two could disagree. Comparing the
    resolved annotations is what keeps them from drifting apart again.
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
