"""A launcher no profile names is withheld while the capability its steps call is off.

`D-2026-09-26-a-launcher-no-profile-names-is-withheld-when-its-capability-is-off` takes the narrow
rule `templates.registry.withheld_reason` implements, and these are its four edges: withheld at the
default deployment, bound the moment the bundle is enabled, bound whenever a profile names it, and
still *declared* either way so a validator and the prose contract keep reading it.

`scale-up-thermal-envelope` is the shipped subject rather than a fixture template, because the row
this closed was about its prefix cost, and a test over a synthetic file would stay green if the
real one went back to being bound on every turn.
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
    """The prefix saving itself: no launcher, so no ~560 tokens on every model call.

    Asserted on three readings of "bound" rather than one, because each is a different consumer —
    the generator, the seven-space union the validators and the mock model resolve against, and
    the surface a compiled graph binds — and a launcher surviving in any of them is paid for.
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
    """The measurement the old refusal rested on still binds wherever it applies.

    `_reject_unknown_tool_names` raises for a profile listing a name the surface lacks, so
    withholding a launcher some profile names would break every turn on that profile. With the
    bundle still off, naming the launcher in any profile keeps it bound (and refused at launch).
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

    Every other shipped template names only default-enabled tools, so if one of them is withheld
    the predicate has widened past "declared here and not bound" — for instance to "not in the
    union", which would silently drop a template with a typo instead of refusing it with the
    problem named.
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
    """The registry only grows; the surface is the rule, not the history.

    Found in CI: the full suite bound this launcher on `default` while this file alone did not,
    because an earlier build in the same process had registered it. Registered here directly, the
    way that build left it, and every reader of the surface must still leave it out.
    """
    (launcher,) = [
        tool for tool in registry.template_tools(declared=True) if tool.__name__ == _LAUNCHER
    ]
    monkeypatch.setitem(tool_registry._REGISTRY, _LAUNCHER, launcher)
    assert _LAUNCHER not in available_tool_names()
    assert _LAUNCHER not in surface(None).tool_names
