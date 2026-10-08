"""The fleet's manifests reach this repository through one door: the installed contracts package.

`chemclaw-contracts` (`Chemclaw3-mcp`'s `packages/chemclaw_contracts`, pinned in `pyproject.toml`)
owns every `connector.yaml` the fleet serves. These tests need no checkout and never skip: no second
copy exists in this tree, discovery reads the package, and the validators pass against it. The
fleet's own `agreement` lane runs this module with its pull request's package installed over the
pinned one, so a manifest this tree cannot read fails there, before it merges.

Two things still read a checkout and skip without one (`CHEMCLAW_SIBLINGS_REQUIRED` turns the skip
into a failure in CI): the `calc` and `rxnlabel` call sites against the fleet's `tool-surface.json`,
and the e2e lane's wiring.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple, get_args, get_origin, get_type_hints

import chemclaw_contracts as contracts
import pytest
import yaml

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


def _manifest(path: Path) -> dict[str, Any]:
    """One `connector.yaml`, parsed."""
    declared: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return declared


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
# The `calc` seam: a contract with no manifest on either side.
# ---------------------------------------------------------------------------------------------

#: Where this repository's own package lives, so the callers below are found rather than listed.
_SRC = REPO_ROOT / "src"


class _Seam(NamedTuple):
    """One MCP server this repository calls with tool names and argument keys typed into `src/`.

    A value, so each seam is a row checked against its own `tool-surface.json`.

    Attributes:
        name: What this seam is called, for a failure message and for the declined table beside it.
        module: The dotted module that defines the dispatchers, resolved through `find_spec` so a
            rename fails loudly rather than emptying the caller set.
        dispatchers: The function or method names that put `(tool, arguments)` on the wire.
        surface: The fleet-relative path of the `tool-surface.json` that server records.
        declined: Tools the fleet serves that nothing here calls, each with the reason.
    """

    name: str
    module: str
    dispatchers: frozenset[str]
    surface: tuple[str, ...]
    declined: Mapping[str, str]


def _module_path(dotted: str) -> str:
    """One dotted module as a repository-relative path, or a failure naming what moved.

    `find_spec`, so a moved module fails rather than emptying a filter.
    """
    spec = importlib.util.find_spec(dotted)
    assert spec is not None and spec.origin is not None, (
        f"{dotted} does not resolve, so the seam it defines has no caller set and every check "
        "over it would pass vacuously. If the module moved, move this name with it."
    )
    return str(Path(spec.origin).relative_to(REPO_ROOT))


def _callers(seam: _Seam) -> tuple[str, ...]:
    """Every module in `src/` that imports one of `seam`'s dispatchers, plus the module defining it.

    Derived from imports, not names, so another module's same-named `_call` is not checked against
    the wrong server. The defining module is added because it does not import what it defines.
    """
    definer = _module_path(seam.module)
    found = [definer]
    for path in sorted(_SRC.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        if relative == definer:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == seam.module:
                if any(alias.name in seam.dispatchers for alias in node.names):
                    found.append(relative)
                    break
    return tuple(sorted(found))


_Bindings = dict[str, frozenset[str] | None]


def _literal_strings(node: ast.AST, bound: _Bindings) -> frozenset[str] | None:
    """The string values an expression can take, or `None` when that is not decidable here.

    Decidable shapes: a literal, a ternary of literals, and a name bound to one of those. `None`
    makes the caller fail rather than skip the site.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.IfExp):
        body = _literal_strings(node.body, bound)
        orelse = _literal_strings(node.orelse, bound)
        return None if body is None or orelse is None else body | orelse
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def _bindings(tree: ast.Module) -> _Bindings:
    """Every name in one module assigned a decidable set of tool-name strings.

    Module-wide: an undecidable assignment anywhere maps the name to `None`, so ambiguity fails.
    """
    bound: _Bindings = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        values = _literal_strings(node.value, {})
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if values is None or bound.get(target.id, values) is None:
                bound[target.id] = None
            else:
                bound[target.id] = (bound.get(target.id) or frozenset()) | values
    return bound


def _typed_tools(seam: _Seam) -> dict[str, frozenset[str]]:
    """Each module-level dispatcher whose `tool` parameter is a `Literal`, with its members.

    For call sites whose tool expression is a table lookup, `mypy --strict` proves the value is a
    member, so the members are what the site can send. Methods and plain-`str` dispatchers have no
    entry.
    """
    module = importlib.import_module(seam.module)
    typed: dict[str, frozenset[str]] = {}
    for name in sorted(seam.dispatchers):
        dispatcher = getattr(module, name, None)
        if dispatcher is None:
            continue
        annotation = get_type_hints(dispatcher).get("tool")
        if get_origin(annotation) is Literal:
            typed[name] = frozenset(get_args(annotation))
    return typed


def _site_tools(
    typed: Mapping[str, frozenset[str]], dispatcher: str, expression: ast.expr, bound: _Bindings
) -> frozenset[str] | None:
    """The tool names one call site can send: its literals, else its dispatcher's `Literal` type."""
    literal = _literal_strings(expression, bound)
    return literal if literal is not None else typed.get(dispatcher)


_Site = tuple[str, str, ast.expr, frozenset[str], _Bindings]


def _hardcoded_calls(seam: _Seam) -> list[_Site]:
    """Each `(module, dispatcher, tool expression, argument keys, name bindings)` written there.

    A site counts when its arguments are a dict literal; a pass-through of a caller's `arguments`
    declares nothing.
    """
    sites: list[_Site] = []
    for relative in _callers(seam):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        bound = _bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", None)
            if name not in seam.dispatchers:
                continue
            for index, argument in enumerate(node.args[:-1]):
                following = node.args[index + 1]
                if not isinstance(following, ast.Dict):
                    continue
                keys = frozenset(
                    key.value
                    for key in following.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
                sites.append((relative, name, argument, keys, bound))
    return sites


def _recorded_surface(root: Path, seam: _Seam) -> dict[str, dict[str, Any]]:
    """The `tool-surface.json` `seam`'s server records, from a `tools/list` against itself."""
    path = root.joinpath(*seam.surface)
    assert path.is_file(), (
        f"Chemclaw3-mcp holds no {'/'.join(seam.surface)}, so the {seam.name} seam has no recorded "
        "surface to check against. If that file moved, move this path with it — a missing surface "
        "must not read as a seam with nothing to say."
    )
    recorded: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return recorded


def _assert_every_call_names_a_served_tool(seam: _Seam) -> None:
    """Every hardcoded call on `seam` names a tool, and only arguments, its server declares.

    A rename on either side fails here rather than at runtime against a pod.
    """
    root = _sibling_or_skip()
    surface = _recorded_surface(root, seam)
    sites = _hardcoded_calls(seam)
    assert sites, (
        f"no hardcoded {seam.name} call site was found, so this check is now vacuous. Either the "
        f"dispatchers moved out of {seam.module} or they stopped taking a literal tool name."
    )
    typed = _typed_tools(seam)
    for relative, dispatcher, expression, keys, bound in sites:
        tools = _site_tools(typed, dispatcher, expression, bound)
        assert tools is not None, (
            f"{relative}:{expression.lineno} passes a tool expression this check cannot resolve to "
            "string literals. Either name the tool literally, type the dispatcher's `tool` "
            "parameter as a `Literal`, or teach `_literal_strings` the shape — passing over it "
            "would leave the call unchecked while the file reported green."
        )
        for tool in sorted(tools):
            assert tool in surface, (
                f"{relative}:{expression.lineno} calls `{tool}`, which Chemclaw3-mcp's "
                f"{'/'.join(seam.surface)} does not record serving: {sorted(surface)}."
            )
            declared = surface[tool]
            assert keys <= set(declared), (
                f"{relative}:{expression.lineno} passes {sorted(keys - set(declared))} to "
                f"`{tool}`, which declares {sorted(declared)}. FastMCP rejects an undeclared "
                "argument, so this is a refused call at runtime and nothing else in this "
                "repository would have said so."
            )
            required = {name for name, spec in declared.items() if spec.get("required")}
            assert required <= keys, (
                f"{relative}:{expression.lineno} calls `{tool}` without "
                f"{sorted(required - keys)}, which the server declares required."
            )


#: The fleet `calc` tools no hardcoded site here names, and the reason each is declined.
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


#: The fleet `rxnlabel` tools no hardcoded site here names, reconciled like `_CALC_DECLINED`.
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


#: The two seams, each read against the surface its own server records.
_CALC_SEAM = _Seam(
    name="calc",
    module="chemclaw.connectors.calc.remote",
    # `remote_version` puts a tool name on the wire too — inside `calculation_key`'s arguments —
    # and was outside this set, so the calibration table's names reached the server unchecked.
    dispatchers=frozenset(
        {"cached_remote", "remote_call", "remote_compute", "remote_version", "_call"}
    ),
    surface=("servers", "calc", "tool-surface.json"),
    declined=_CALC_DECLINED,
)

#: `rxnlabel` is a backend like `calc`. Its dispatcher is a method, `RxnLabelServer._call`, which
#: is why a seam names its defining module: `labeller.py` imports nothing an importer walk could
#: see.
_RXNLABEL_SEAM = _Seam(
    name="rxnlabel",
    module="chemclaw.ingest.labels.labeller",
    dispatchers=frozenset({"_call"}),
    surface=("servers", "rxnlabel", "tool-surface.json"),
    declined=_RXNLABEL_DECLINED,
)


def _tools_named(seam: _Seam) -> set[str]:
    """Every tool name `seam`'s hardcoded sites put on the wire.

    Unresolvable sites contribute nothing; the check above already fails on them.
    """
    typed = _typed_tools(seam)
    named: set[str] = set()
    for _relative, dispatcher, expression, _keys, bound in _hardcoded_calls(seam):
        named |= _site_tools(typed, dispatcher, expression, bound) or frozenset()
    return named


def _assert_every_served_tool_is_called_or_declined(seam: _Seam) -> None:
    """Every tool the fleet serves on `seam` is called here or declined with a reason.

    The other direction catches renames; this one catches the fleet growing, so a new tool is a
    deliberate decision rather than silence.
    """
    root = _sibling_or_skip()
    surface = _recorded_surface(root, seam)
    unreached = set(surface) - _tools_named(seam)
    assert unreached == set(seam.declined), (
        f"{sorted(unreached - set(seam.declined))} are served by Chemclaw3-mcp's {seam.name} "
        "server and named by no hardcoded call site here, with no reason recorded — call them, or "
        f"add a row to the {seam.name} declined table saying why not. And "
        f"{sorted(set(seam.declined) - unreached)} are recorded as declined while that is no "
        "longer the state: either this repository now calls one (delete its row) or the fleet has "
        "withdrawn one (the reason written beside it is about a tool that no longer exists, and "
        "whatever else that reason justified needs re-reading)."
    )


def test_the_calc_seam_calls_only_tools_the_fleet_records_serving() -> None:
    """The `calc` seam calls only tools the fleet records serving."""
    _assert_every_call_names_a_served_tool(_CALC_SEAM)


def test_every_tool_the_calibration_table_names_is_one_the_calc_seam_checks() -> None:
    """`_CALIBRATED`'s tool names are exactly what the seam walker reads at `remote_version`.

    Needs no checkout. Equality in both directions: an unlisted row would send an unchecked name,
    and an unused `CalibratedTool` member would count as called and satisfy the declined accounting.
    """
    from chemclaw.connectors.calc.server.tools import _CALIBRATED

    typed = _typed_tools(_CALC_SEAM)
    resolved = {
        tool
        for _relative, dispatcher, expression, _keys, bound in _hardcoded_calls(_CALC_SEAM)
        if dispatcher == "remote_version"
        for tool in _site_tools(typed, dispatcher, expression, bound) or ()
    }
    table = {tool for tool, _unit in _CALIBRATED.values()}

    assert resolved, (
        "no `remote_version` call site resolved to a tool name, so the calibration table's names "
        "reach the fleet through a call this file does not check — `remote_version` left "
        "`_CALC_SEAM.dispatchers`, or its `tool` parameter stopped being a `Literal`"
    )
    assert table == resolved, (
        f"`_CALIBRATED` names {sorted(table - resolved)} that the seam walker does not attribute "
        f"to `remote_version`, and the walker attributes {sorted(resolved - table)} that no table "
        "row names. Keep `remote.CalibratedTool` and the table's tool column the same set."
    )


def test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason() -> None:
    """The `calc` seam's other direction — a tool the fleet adds is a decision, not a silence."""
    _assert_every_served_tool_is_called_or_declined(_CALC_SEAM)


def test_the_rxnlabel_seam_calls_only_tools_the_fleet_records_serving() -> None:
    """`rxnlabel`, the second backend seam, calls only tools the fleet records serving."""
    _assert_every_call_names_a_served_tool(_RXNLABEL_SEAM)


def test_every_rxnlabel_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason() -> None:
    """The `rxnlabel` seam's other direction — its two single-reaction tools are declined."""
    _assert_every_served_tool_is_called_or_declined(_RXNLABEL_SEAM)


def test_the_composite_this_repository_assembles_is_not_also_served_by_the_fleet() -> None:
    """`compute_thermochemistry` is composed here out of primitives and must not be served there.

    A fleet copy would give the family two answers to one question.
    """
    root = _sibling_or_skip()
    surface: dict[str, dict[str, Any]] = json.loads(
        (root / "servers" / "calc" / "tool-surface.json").read_text(encoding="utf-8")
    )

    assert "compute_thermochemistry" not in surface, (
        "Chemclaw3-mcp now serves `compute_thermochemistry`, which this repository composes from "
        "separately keyed primitives. Two live definitions of one calculation is the duplication "
        "both repositories' rules forbid — decide which one answers before either ships."
    )


def test_the_fake_calc_server_serves_exactly_the_surface_the_fleet_records() -> None:
    """`tests/calc_server_fake.py` serves exactly the surface the fleet records.

    The fake cannot notice the real server changing, so the two are compared. `_KEYED` and
    `_UNKEYED` are the fake's declaration of what it serves.
    """
    root = _sibling_or_skip()
    surface = json.loads(
        (root / "servers" / "calc" / "tool-surface.json").read_text(encoding="utf-8")
    )
    from tests.calc_server_fake import _KEYED, _UNKEYED

    fake = set(_KEYED) | set(_UNKEYED) | {"calculation_key"}
    assert fake == set(surface), (
        f"the fake serves {sorted(fake - set(surface))} that Chemclaw3-mcp's calc server does not "
        f"record, and not {sorted(set(surface) - fake)} that it does. A fake that has drifted from "
        "the server proves the suite runs, not that the seam works."
    )
