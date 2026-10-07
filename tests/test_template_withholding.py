"""A launcher no profile names is withheld while the capability its steps call is off.

The four edges of `templates.registry.withheld_reason`: withheld at the default deployment, bound
once the bundle is enabled, bound whenever a profile names it, and still declared either way so
validators keep reading it. The shipped `scale-up-thermal-envelope` is the subject, so the real
template regressing turns this red.
"""

import os

import pytest

from chemclaw.agent.chemclaw_agent import available_tool_names, declared_tool_names
from chemclaw.agent.profiles import _REGISTRY as PROFILE_REGISTRY
from chemclaw.agent.profiles import AgentProfile
from chemclaw.connectors.registry import enabled as enabled_connectors
from chemclaw.core import tool_registry
from chemclaw.core.config import settings
from chemclaw.templates import registry
from chemclaw.templates.manifest import Template
from tests.surface import surface

_OPT_IN = "scale-up-thermal-envelope"
_LAUNCHER = "run_scale_up_thermal_envelope"
_BUNDLE = "thermalsafety"


def _template() -> Template:
    """The shipped opt-in template, read off `data/templates/` like every other."""
    return registry.discovered()[_OPT_IN]


def _enable_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable `thermalsafety` on top of whatever the default deployment already enables.

    An explicit list overrides `default_enabled`, so the default set is spelled out beside the
    opt-in bundle — the configuration a deployment turning it on would actually write.
    """
    names = [manifest.name for manifest in enabled_connectors()]
    assert _BUNDLE not in names, "the default deployment already binds the bundle; nothing to test"
    monkeypatch.setattr(settings, "connectors_enabled", os.pathsep.join([*names, _BUNDLE]))


def test_the_default_deployment_binds_no_launcher_for_an_opt_in_capability() -> None:
    """The default deployment binds no launcher for an opt-in capability.

    Asserted on the generator, the union the validators and mock model resolve against, and the
    surface a compiled graph binds, since a launcher surviving in any of them is paid for.
    """
    assert registry.withheld_reason(_template()) == [
        "adiabatic_temperature_rise",
        "mtsr",
        "stoessel_criticality_class",
    ]
    assert _LAUNCHER not in registry.template_tool_names()
    assert _LAUNCHER not in {tool.__name__ for tool in registry.template_tools()}
    assert _LAUNCHER not in available_tool_names()
    assert _LAUNCHER not in surface(None).tool_names


def test_a_withheld_launcher_is_still_declared() -> None:
    """Withheld is not deleted: the tree still declares it, so a reference to it still validates.

    `make template-validate`, `make skill-validate` and the prose contract all ask what the *tree*
    declares, and that is the same answer they give for an opt-in bundle's own tools.
    """
    assert _LAUNCHER in registry.template_tool_names(declared=True)
    assert _LAUNCHER in {tool.__name__ for tool in registry.template_tools(declared=True)}
    assert _LAUNCHER in declared_tool_names()


def test_enabling_the_bundle_binds_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the rule: the launcher follows its capability on, with no other edit."""
    _enable_the_bundle(monkeypatch)
    assert registry.withheld_reason(_template()) == []
    assert _LAUNCHER in registry.template_tool_names()
    assert _LAUNCHER in available_tool_names()


def test_a_launcher_a_profile_names_is_never_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launcher a profile names is never withheld.

    `_reject_unknown_tool_names` raises for a profile naming an absent tool, so withholding it would
    break every turn on that profile; it stays bound and is refused at launch.
    """
    monkeypatch.setitem(
        PROFILE_REGISTRY,
        "names-the-opt-in-template",
        AgentProfile(name="names-the-opt-in-template", tool_names=frozenset({_LAUNCHER})),
    )
    assert registry.withheld_reason(_template()) == []
    assert _LAUNCHER in registry.template_tool_names()
    assert registry.unrunnable_reason(_template()), "the bound launcher must still refuse to launch"


def test_every_other_shipped_template_is_bound_by_default() -> None:
    """Only a template whose capability is off is withheld; a broken one keeps its refusal.

    If the predicate widened to "not in the union", a template with a typo would be dropped silently
    instead of refused with the problem named.
    """
    withheld = {t.name for t in registry.enabled() if registry.withheld_reason(t)}
    assert withheld == {_OPT_IN}


def test_a_template_naming_a_tool_nothing_declares_keeps_its_launcher() -> None:
    """A typo is not an opt-in capability: it keeps the launcher and the refusal that names it."""
    broken = Template.model_validate(
        {
            "name": "names-a-typo",
            "summary": "Do the thing.",
            "steps": [{"id": "one", "kind": "tool", "tool": "stoessel_criticality_clas"}],
        }
    )
    assert registry.withheld_reason(broken) == []
    assert "stoessel_criticality_clas" in registry.unrunnable_reason(broken)


def test_a_launcher_registered_under_another_configuration_is_still_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher registered by an earlier build in the process is still withheld.

    The registry only grows, so the surface, not the registration history, decides.
    """
    (launcher,) = [
        tool for tool in registry.template_tools(declared=True) if tool.__name__ == _LAUNCHER
    ]
    monkeypatch.setitem(tool_registry._REGISTRY, _LAUNCHER, launcher)
    assert _LAUNCHER not in available_tool_names()
    assert _LAUNCHER not in surface(None).tool_names
