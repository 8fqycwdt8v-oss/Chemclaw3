"""The fleet's manifests reach this repository through one door: the installed contracts package.

`chemclaw-contracts` (`Chemclaw3-mcp`'s `packages/chemclaw_contracts`, pinned in `pyproject.toml`)
owns every `connector.yaml` the fleet serves and the typed `calc` and `rxnlabel` request models
this tree sends. These tests need no checkout and never skip: no second copy of a manifest exists in
this tree, discovery reads the package, the validators pass against it, and every tool the package
models is requested here or declined with a reason. The fleet's own `agreement` lane runs this
module with its pull request's package installed over the pinned one, so a change this tree cannot
read fails there, before it merges.

One test still reads a checkout and skips without one (`CHEMCLAW_SIBLINGS_REQUIRED` turns the skip
into a failure in CI): the e2e lane's wiring.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, get_args

import chemclaw_contracts as contracts
import pytest
import yaml
from chemclaw_contracts.calc import CALC_REQUESTS
from chemclaw_contracts.rxnlabel import RXNLABEL_REQUESTS

from chemclaw.connectors.manifest import ConnectorManifest
from chemclaw.connectors.registry import ConnectorError, discovered, forget_discovered
from chemclaw.core.config import Settings, settings
from tests.siblings import (
    REPO_ROOT,
    SIBLING_SKIP,
    bundles_declared_here,
    sibling_root,
)

_CHART_VALUES = REPO_ROOT / "deploy" / "helm" / "chemclaw" / "values.yaml"


def _default_connectors_dir() -> str:
    """The code's own default for `connectors_dir`, whatever the environment says."""
    default = Settings.model_fields["connectors_dir"].get_default(call_default_factory=True)
    assert isinstance(default, str)
    return default


@pytest.fixture
def default_path(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point discovery at the default path with an empty enable-list, and drop its cache."""
    default = _default_connectors_dir()
    monkeypatch.setattr(settings, "connectors_dir", default)
    monkeypatch.setattr(settings, "connectors_enabled", "")
    forget_discovered()
    return default


def test_no_second_copy_of_a_fleet_manifest_exists_in_this_tree() -> None:
    """A bundle directory here never carries the name of a connector the package publishes.

    A copy would be a collision at discovery (`registry._bundle_dirs`) and, before that, two files
    that could disagree unseen: the defect the package was introduced to end. The package's
    internal manifests (`calc`, `rxnlabel`) are not connectors and are deliberately outside this:
    this tree's own `calc` bundle shares a name with the `calc` backend, and `mount: backend` is
    what keeps the two apart (`test_a_backend_manifest_cannot_be_mounted`).
    """
    owned = set(contracts.manifest_names())
    assert owned, "the installed package declares no manifest; has its layout changed?"
    copies = sorted(set(bundles_declared_here()) & owned)
    assert not copies, (
        f"{copies} are declared by the fleet's package and by a connector.yaml in this tree. "
        "Delete the copy (keep its skills/ beside a README); the package supplies the manifest."
    )


def test_every_connector_the_package_declares_is_discovered_from_the_package(
    default_path: str,
) -> None:
    """Each manifest the package publishes is the one discovery loads, field for field.

    Read back through `ConnectorManifest`, so a key the model refuses fails here and not in a pod,
    and a `contract_version` the package declares is the value the model holds.
    """
    del default_path
    found = discovered()
    for name in contracts.manifest_names():
        assert name in found, f"{name} is in the package and discovery did not find it"
        bundle, manifest = found[name]
        assert bundle.parent == contracts.manifests_dir(), (
            f"{name} was discovered in {bundle.parent}, not in the installed package"
        )
        from_package = ConnectorManifest.model_validate(
            yaml.safe_load(contracts.manifest_path(name).read_text(encoding="utf-8"))
        )
        assert manifest == from_package
        assert manifest.contract_version == contracts.contract_version(name)


def test_the_connector_validators_pass_against_the_installed_package(default_path: str) -> None:
    """`connector-validate`, `skill-validate` and `template-validate`, over the default path.

    The three the fleet's `agreement` lane runs against its pull request's package: a manifest that
    names a tool no validator can resolve, or a skill whose declared tools the package does not
    serve, fails here.
    """
    del default_path
    from chemclaw.cli.validate_connectors import validate_connectors
    from chemclaw.cli.validate_skills import validate_skills
    from chemclaw.cli.validate_templates import validate_templates
    from chemclaw.connectors.registry import declared_skills_dirs

    assert validate_connectors() == []
    assert validate_skills([*settings.skills_dirs, *declared_skills_dirs()]) == []
    assert validate_templates() == []


@pytest.mark.parametrize("name", contracts.manifest_names(internal=True))
def test_a_backend_manifest_cannot_be_mounted(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The package's internal manifests (`mount: backend`) are refused by this tree's model.

    Those servers are addressed by `CHEMCLAW_CALC_SERVER_URL` / `CHEMCLAW_RXNLABEL_SERVER_URL`;
    mounting their directory would put internal primitives in the prompt.
    """
    monkeypatch.setattr(settings, "connectors_dir", str(contracts.internal_manifests_dir()))
    forget_discovered()
    with pytest.raises(ConnectorError, match="mount") as raised:
        discovered()
    assert f"{name}/connector.yaml" in str(raised.value) or "mount" in str(raised.value)


def test_every_fleet_server_has_an_egress_port_and_a_token_slot_in_the_chart() -> None:
    """Every server the package declares has an egress port and a token slot in the chart.

    A NetworkPolicy restricts by port independently of its destinations, and without a
    `secrets.optionalKeys` slot the bearer has nowhere to come from. Derived from the package, which
    names both the port and `auth.token_env` of every connector and backend.
    """
    values = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8"))
    ports = {int(port) for port in values["networkPolicy"]["egressPorts"].values()}
    slots = set(values["secrets"]["optionalKeys"].values())
    names = [*contracts.manifest_names(), *contracts.manifest_names(internal=True)]
    assert names, "the installed package declares no manifest"
    missing: list[str] = []
    for name in names:
        endpoint = yaml.safe_load(contracts.manifest_path(name).read_text(encoding="utf-8"))[
            "endpoint"
        ]
        port = int(endpoint["url"].rsplit(":", 1)[1].split("/", 1)[0])
        if port not in ports:
            missing.append(f"{name}: port {port} is not in networkPolicy.egressPorts")
        token_env = (endpoint.get("auth") or {}).get("token_env")
        if token_env and token_env not in slots:
            missing.append(f"{name}: {token_env} has no secrets.optionalKeys slot")
    assert not missing, "\n".join(missing)


def _sibling_or_skip() -> Path:
    """The fleet checkout, or a skip naming what went unread."""
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(
            f"{SIBLING_SKIP} the declarations were NOT read: {reason}. Nothing in this run is "
            "evidence about whether the two repositories still declare the same surface."
        )
    return root


#: The script whose directory order decides which manifests the four-repo lane reads.
_E2E_UP = REPO_ROOT / "infra/live/e2e-full-stack/up.sh"


def _e2e_connectors_dir(fleet: Path) -> str:
    """`CHEMCLAW_CONNECTORS_DIR` exactly as `up.sh` exports it, with its three variables bound.

    Read off the script rather than transcribed, because the wiring *is* the claim: a transcription
    would go on agreeing with itself after the script changed.
    """
    import chemclaw.connectors

    exports = [
        line.split("=", 1)[1].strip().strip('"')
        for line in _E2E_UP.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("export CHEMCLAW_CONNECTORS_DIR=")
    ]
    assert len(exports) == 1, f"{_E2E_UP} exports CHEMCLAW_CONNECTORS_DIR {len(exports)} times"
    bindings = {
        "$own_connectors": str(Path(chemclaw.connectors.__file__).resolve().parent),
        "$MCP_REPO": str(fleet),
        "$HARNESS_DIR": str(_E2E_UP.parent),
    }
    value = exports[0]
    for variable, path in bindings.items():
        value = value.replace(variable, path)
    assert "$" not in value, f"{_E2E_UP} names a variable this test does not bind: {value}"
    return value


def test_the_e2e_lane_reads_every_fleet_connector_and_binds_no_opt_in_one_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `up.sh`'s own wiring the registry discovers what the package declares, and only that.

    The lane puts the fleet *checkout's* `manifests/` on the path rather than the installed package,
    so the manifests it reads and the servers it starts are one revision. Every connector the
    package publishes must still be discovered there, and the ones the fleet declares
    `default_enabled: false` must stay unbound with no enable-list: the lane's enable-list is
    derived from discovery, and a lane that binds an opt-in bundle by silence pays its schemas on
    every call.
    """
    from chemclaw.connectors import registry

    fleet = _sibling_or_skip()
    opt_in = {
        name
        for name in contracts.manifest_names()
        if yaml.safe_load(contracts.manifest_path(name).read_text(encoding="utf-8")).get(
            "default_enabled", True
        )
        is False
    }
    assert opt_in, "no package manifest declares `default_enabled: false`, so this checks nothing"
    monkeypatch.setattr(settings, "connectors_dir", _e2e_connectors_dir(fleet))
    monkeypatch.setattr(settings, "connectors_enabled", "")
    forget_discovered()
    assert set(contracts.manifest_names()) <= set(registry.discovered()), (
        f"the lane's wiring does not discover {sorted(set(contracts.manifest_names()))} "
        "from the fleet checkout"
    )
    bound = {manifest.name for manifest in registry.enabled()}
    assert not opt_in & bound, (
        f"{sorted(opt_in & bound)} bind in the e2e lane's wiring with no enable-list, although "
        f"the fleet declares them `default_enabled: false`. {_E2E_UP} has changed its "
        "CHEMCLAW_CONNECTORS_DIR."
    )


# ---------------------------------------------------------------------------------------------
# The `calc` and `rxnlabel` seams: typed requests from the same package.
# ---------------------------------------------------------------------------------------------

#: Where this repository's own package lives, so the callers below are found rather than listed.
_SRC = REPO_ROOT / "src"


#: The fleet `calc` tools no request built here names, and the reason each is declined.
#:
#: Reconciled in both directions against the derived difference, so a stale, missing or orphaned
#: row fails.
#:
#: * `optimize_geometry` derives the same `calc_key` as `relax_structure` but returns a different
#:   payload, so caching either under that key would poison the other; the local tool composes
#:   `embed_structure` and `relax_structure` instead.
#: * `predict_logd` returns no key from `calculation_key`, so `cached_remote` refuses it; the local
#:   tool calls the cached `predict_pka` and finishes locally.
_CALC_DECLINED: dict[str, str] = {
    "optimize_geometry": (
        "shares `relax_structure`'s cache key while returning a different payload, so this "
        "repository composes `embed_structure` + `relax_structure` and stores the one payload "
        "shape that key may hold"
    ),
    "predict_logd": (
        "the server derives no cache key for it, so `cached_remote` refuses it; the composite is "
        "assembled here from a cached `predict_pka` plus a local Crippen sum"
    ),
}


#: The fleet `rxnlabel` tools no request built here names, reconciled like `_CALC_DECLINED`.
#:
#: The drain calls only the batch tools; the single-reaction tools exist for interactive use, and a
#: batch of one covers them.
_RXNLABEL_DECLINED: dict[str, str] = {
    "name_reaction": (
        "the single-reaction form of `name_reactions`, which is what the drain calls: one round "
        "trip per reaction is 13M of them on a Pistachio-scale corpus, and a caller wanting one "
        "reaction sends a batch of one"
    ),
    "represent_reaction": (
        "the single-reaction form of `represent_reactions`, declined for the same reason — and it "
        "additionally defaults its `species` list out of the reaction SMILES, which the batch form "
        "refuses to do because a stored species' ordinal comes from `OrdReaction.compounds()` and "
        "the two orders are not the same"
    ),
}


@cache
def _names_used_in_src() -> frozenset[str]:
    """Every identifier or attribute `src/` reads anywhere, so a class is "used" only if it is."""
    names: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
    return frozenset(names)


def _unbuilt(requests: Mapping[str, type[Any]]) -> set[str]:
    """The tools whose request model nothing in `src/` ever names."""
    used = _names_used_in_src()
    return {tool for tool, model in requests.items() if model.__name__ not in used}


def test_every_calc_tool_the_fleet_serves_is_requested_here_or_declined_with_a_reason() -> None:
    """A tool the fleet adds to `calc` is a decision here, not a silence.

    The package's request models are the fleet's tool list. Sites that send one name its model, so
    a tool no source line names is either declined below with a reason or newly served and unread.
    """
    unbuilt = _unbuilt(CALC_REQUESTS)
    assert unbuilt == set(_CALC_DECLINED), (
        f"{sorted(unbuilt - set(_CALC_DECLINED))} are served by Chemclaw3-mcp's calc server and "
        "named by no request built here, with no reason recorded: call them, or add a row to "
        f"_CALC_DECLINED. And {sorted(set(_CALC_DECLINED) - unbuilt)} are recorded as declined "
        "while some source line now names their request model (delete the row)."
    )


def test_every_rxnlabel_tool_the_fleet_serves_is_requested_here_or_declined_with_a_reason() -> None:
    """The `rxnlabel` seam's same accounting: its two single-reaction tools are declined."""
    unbuilt = _unbuilt(RXNLABEL_REQUESTS)
    assert unbuilt == set(_RXNLABEL_DECLINED), (
        f"{sorted(unbuilt - set(_RXNLABEL_DECLINED))} are served by Chemclaw3-mcp's rxnlabel "
        "server and named by no request built here, with no reason recorded. And "
        f"{sorted(set(_RXNLABEL_DECLINED) - unbuilt)} are recorded as declined while a source "
        "line now names their request model."
    )


def test_the_calibration_table_names_only_requests_remote_version_accepts() -> None:
    """`_CALIBRATED` rows and `CalibratedRequest` are one set, so neither can outgrow the other.

    An unlisted request type would be a version probe the type checker does not see; an unused
    union member would count as called.
    """
    from chemclaw.connectors.calc.remote import CalibratedRequest
    from chemclaw.connectors.calc.server.tools import _CALIBRATED

    table = {request for request, _unit in _CALIBRATED.values()}
    assert table == set(get_args(CalibratedRequest))
    assert {request.tool_name for request in table} <= set(CALC_REQUESTS)


def test_the_composite_this_repository_assembles_is_not_also_served_by_the_fleet() -> None:
    """`compute_thermochemistry` is composed here out of primitives and must not be served there.

    A fleet copy would give the family two answers to one question.
    """
    assert "compute_thermochemistry" not in CALC_REQUESTS, (
        "Chemclaw3-mcp now serves `compute_thermochemistry`, which this repository composes from "
        "separately keyed primitives. Two live definitions of one calculation is the duplication "
        "both repositories' rules forbid — decide which one answers before either ships."
    )


def test_the_fake_calc_server_serves_exactly_the_surface_the_fleet_models() -> None:
    """`tests/calc_server_fake.py` serves exactly the tools the package defines requests for.

    The fake cannot notice the real server changing, so the two are compared. `_KEYED` and
    `_UNKEYED` are the fake's declaration of what it serves; the arguments it accepts are the
    package's own models (`FakeCalcServer.call_tool`).
    """
    from tests.calc_server_fake import _KEYED, _UNKEYED

    fake = set(_KEYED) | set(_UNKEYED) | {"calculation_key"}
    assert fake == set(CALC_REQUESTS), (
        f"the fake serves {sorted(fake - set(CALC_REQUESTS))} that the fleet's package does not "
        f"model, and not {sorted(set(CALC_REQUESTS) - fake)} that it does. A fake that has "
        "drifted from the server proves the suite runs, not that the seam works."
    )
