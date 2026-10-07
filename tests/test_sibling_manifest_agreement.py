"""Cross-repository agreement between this repository and a `Chemclaw3-mcp` checkout.

1. Bundles declared in both trees must agree on every key: the endpoint's tools, read-only
   partition and token variable, and every bundle-level key (`skills`, `profiles`, `note_types`,
   `relations`, `jobs`), since discovery is first-directory-wins and the loser is dropped
   silently. Known differences live in `_ARGUED_DIVERGENCES`.
2. Backend seams (`calc`, `rxnlabel`) have no manifest here, so every hardcoded call site is
   checked against the fleet's `tool-surface.json`, in both directions: every name sent is
   served, and every served tool is called or declined with a reason.

Opt-in: each test needs a sibling checkout and skips without one; `tests/conftest.py` reports
how many skipped.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple, get_args, get_origin, get_type_hints

import pytest
import yaml

from chemclaw.connectors.manifest import ConnectorManifest
from tests.siblings import (
    REPO_ROOT,
    SIBLING_SKIP,
    bundles_declared_here,
    fleet_published_bundles,
    sibling_root,
)

#: The endpoint fields that decide what a turn may call and how it authenticates.
#:
#: `url` and `health_url` are deployment facts; `description` is prose bounded by
#: `tests/test_context_floor.py`.
_SURFACE_FIELDS = ("tools", "read_only", "state_changing")


#: The three keys `_SURFACE_FIELDS` and the `auth` assertion cover, or that decide nothing. Every
#: other key of either manifest is bundle-level content and is compared.
_NOT_CONTENT = frozenset({"name", "description", "endpoint"})


def _content_keys(mine: dict[str, Any], theirs: dict[str, Any], where: str) -> tuple[str, ...]:
    """Every bundle-level key to compare for one pair of manifests.

    The union of what `ConnectorManifest` declares and what either file declares: the model half
    covers a newly added key before either file uses it, and the file half keeps a narrowed model
    from emptying the comparison. A key neither model knows is refused, since the fleet's copy is
    never loaded here.
    """
    declared = frozenset(ConnectorManifest.model_fields) - _NOT_CONTENT
    present = (frozenset(mine) | frozenset(theirs)) - _NOT_CONTENT
    unknown = present - declared
    assert not unknown, (
        f"{where}: {sorted(unknown)} is declared in a manifest and is not a field of "
        '`ConnectorManifest`. This repository\'s model is `extra="forbid"`, so such a key fails '
        "at startup on this side and is simply unread on the other — which is a declaration one "
        "repository believes it has made and the other cannot act on."
    )
    return tuple(sorted(declared | present))


def _comparable(value: Any) -> Any:
    """One manifest value in a form two files can be compared by, ignoring what decides nothing.

    String lists are allow-lists compared as sets; `jobs:` mappings are keyed by `name`; a missing
    key equals an empty list.
    """
    if value is None:
        return frozenset()
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return frozenset(value)
        if all(isinstance(item, dict) and "name" in item for item in value):
            return {str(item["name"]): _comparable_mapping(item) for item in value}
    return value


def _comparable_mapping(mapping: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """One mapping as an order-free pair sequence, with its own list values normalised."""
    return tuple(sorted((key, _comparable(value)) for key, value in mapping.items()))


#: Bundle-level keys that legitimately differ between the two trees, keyed `(bundle, key)`, each
#: with why the difference is harmless.
_ARGUED_DIVERGENCES: dict[tuple[str, str], str] = {
    ("safety", "skills"): (
        "the fleet's manifest declares no `skills:` on purpose, and says so in its own header: a "
        "SKILL.md is architecture layer 3 in *this* repository and that fleet has no equivalent "
        "seam, so `connectors/safety/skills/safety-screening/SKILL.md` stays here. What makes it "
        "harmless is `connectors/registry._bundle_content_dirs`, which reads every directory "
        "carrying an enabled bundle's name rather than only the one whose manifest won the name — "
        "so the skill is reachable in either wiring order. Before that it was not: the order both "
        "that repository's README and its integration doc publish (`manifests/` first) dropped it "
        "with no error, no warning and no log line."
    ),
    ("thermalsafety", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `thermalsafety`'s tools is architecture layer 3 and lives here, and that "
        "fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `thermal-safety-assessment` is reachable in either wiring "
        "order."
    ),
    ("kinetics", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `kinetics`'s tools is architecture layer 3 and lives here, and that fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `kinetics-and-reactor-choice` is reachable in either "
        "wiring order."
    ),
    ("unitops", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `unitops`'s tools is architecture layer 3 and lives here, and that fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `unit-operation-sizing` is reachable in either wiring "
        "order."
    ),
    ("suitability", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `suitability`'s tools is architecture layer 3 and lives here, and that "
        "fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `system-suitability` is reachable in either wiring order."
    ),
}


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


def test_a_bundle_declared_in_both_trees_declares_the_same_surface() -> None:
    """A bundle name both trees declare must mean the same bundle in both — every key of it.

    Compares `_SURFACE_FIELDS`, `auth.token_env` and every `_content_keys` key. Compared as sets,
    since tool order differs and decides nothing. Bundles are derived from both trees, so a new
    shared bundle is checked without editing this file.
    """
    root = _sibling_or_skip()
    here = bundles_declared_here()
    there = fleet_published_bundles(root)
    shared = sorted(set(here) & set(there))
    assert shared, (
        f"no bundle name is declared in both trees ({sorted(here)} here, {sorted(there)} there), "
        "so this test now checks nothing. If the ports were withdrawn, SERVED_ELSEWHERE in "
        "tests/test_context_floor.py is charging an allowance for servers nobody serves."
    )
    for name in shared:
        mine, theirs = _manifest(here[name]), _manifest(there[name])
        assert theirs["name"] == name, (
            f"{there[name]} declares name {theirs['name']!r} in a directory called {name!r}. "
            "`registry._load_manifest` rejects that outright — the folder is authoritative — so "
            "any deployment that puts the fleet's manifests on CHEMCLAW_CONNECTORS_DIR fails at "
            "startup with a ConnectorError naming this file, not with a differently-named bundle."
        )
        # The bundle level first, because a divergence there is what nothing looked at. Read off
        # the whole manifests, before the two names are rebound to the endpoint blocks below.
        compared = _content_keys(mine, theirs, f"{here[name]} / {there[name]}")
        # What the loop below actually looked at, rather than what it was handed. The two differ by
        # exactly the mutation that emptied the loop and left the stale-row check reading the
        # *intended* scope — driven, and green.
        visited: set[str] = set()
        for field in compared:
            visited.add(field)
            argued = _ARGUED_DIVERGENCES.get((name, field))
            agrees = _comparable(mine.get(field)) == _comparable(theirs.get(field))
            if argued is not None:
                assert not agrees, (
                    f"`{name}`'s `{field}` is recorded as an argued divergence and the two trees "
                    f"now agree about it. Delete that row from `_ARGUED_DIVERGENCES`: a row that "
                    "outlives its subject reads as a live exemption, and the reason written beside "
                    f"it is about a difference that no longer exists.\n\nThe row said: {argued}"
                )
                continue
            assert agrees, (
                f"`{name}` declares a different `{field}` in the two repositories: "
                f"{mine.get(field)!r} here against {theirs.get(field)!r} in {there[name]}. First "
                "directory on CHEMCLAW_CONNECTORS_DIR wins the name outright, so one of these two "
                "declarations is simply unread in any given deployment — and the keys at this "
                "level are not the tool surface: `skills` and `profiles` decide what judgment and "
                "which agent profiles a deployment can reach, `note_types` and `relations` decide "
                "what `make kg-validate` accepts, and `jobs` decides which durable launchers and "
                "`connector-<name>` queues exist. Make them agree, or add a row to "
                "`_ARGUED_DIVERGENCES` saying why they may differ AND what makes that harmless."
            )
        # Every argued row for this bundle must have been *reached*, or the exemption is standing
        # over a key nothing looked at. This is the half the first version of this loop lacked:
        # narrowing the compared set silently retired the stale-row check along with the comparison.
        argued_here = {field for (bundle, field) in _ARGUED_DIVERGENCES if bundle == name}
        unreached = sorted(argued_here - visited)
        assert not unreached, (
            f"`{name}` has argued divergences for {unreached}, and those keys were not compared. "
            "The exemption is therefore standing over nothing — widen `_content_keys` or delete "
            "the rows."
        )
        # And the row's subject has to exist in a file. A row about a key neither manifest declares
        # any more is a reason nobody can check, kept alive by a comparison that agrees trivially.
        absent = sorted(field for field in argued_here if field not in mine and field not in theirs)
        assert not absent, (
            f"`{name}` has argued divergences for {absent}, which neither manifest declares any "
            "more. Delete those rows — the difference they excuse is gone."
        )
        mine, theirs = mine["endpoint"], theirs["endpoint"]
        for field in _SURFACE_FIELDS:
            assert set(mine.get(field) or ()) == set(theirs.get(field) or ()), (
                f"`{name}` declares a different {field} in the two repositories: "
                f"{sorted(set(mine.get(field) or ()))} here against "
                f"{sorted(set(theirs.get(field) or ()))} in {there[name]}. First directory on "
                "CHEMCLAW_CONNECTORS_DIR wins the name outright, with no merge and no warning, so "
                "one of these two surfaces is simply unreachable in any given deployment."
            )
        assert mine["auth"].get("token_env") == theirs["auth"].get("token_env"), (
            f"`{name}` reads its bearer from {mine['auth'].get('token_env')} here and "
            f"{theirs['auth'].get('token_env')} in {there[name]}. Both halves of that name matter "
            "and they are set in two different places: the server verifies one, the front door "
            "sends the other, and /healthz is unauthenticated — so a mismatch is a connector that "
            "reports healthy while every call it makes is refused."
        )


#: The script whose directory order decides which copy of an opt-in bundle's manifest is read.
_E2E_UP = REPO_ROOT / "infra/live/e2e-full-stack/up.sh"


def _e2e_connectors_dir(fleet: Path) -> str:
    """`CHEMCLAW_CONNECTORS_DIR` exactly as `up.sh` exports it, with its three variables bound.

    Read off the script rather than transcribed, because the order *is* the claim: a transcription
    would go on agreeing with itself after the script moved the fleet's `manifests/` first.
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


def _opt_in_here() -> set[str]:
    """Every bundle this tree's own manifest declares `default_enabled: false`.

    Read from this repository, which owns the opt-in decision.
    """
    return {
        name
        for name, path in bundles_declared_here().items()
        if _manifest(path).get("default_enabled", True) is False
    }


def test_the_e2e_lane_binds_no_opt_in_bundle_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every bundle this tree declares opt-in is unbound under `up.sh`'s own wiring.

    `up.sh` lists this tree's connectors first and discovery is first-directory-wins. Driven through
    `registry.enabled()` with the exported order and no enable-list.
    """
    from chemclaw.connectors import registry
    from chemclaw.core.config import settings

    fleet = _sibling_or_skip()
    opt_in = _opt_in_here()
    monkeypatch.setattr(settings, "connectors_dir", _e2e_connectors_dir(fleet))
    monkeypatch.setattr(settings, "connectors_enabled", "")
    bound = {manifest.name for manifest in registry.enabled()}
    assert opt_in, "no manifest here declares `default_enabled: false`, so this test checks nothing"
    assert not opt_in & bound, (
        f"{sorted(opt_in & bound)} bind in the e2e lane's wiring with no enable-list, so "
        "the claim that this tree's copy wins the name in the lane — and with it this tree's "
        f"`default_enabled: false` — is false. {_E2E_UP} has changed its CHEMCLAW_CONNECTORS_DIR "
        "order."
    )


def test_the_compared_key_set_is_anchored_in_both_the_model_and_the_two_files() -> None:
    """The compared key set is anchored in both the model and the two files.

    Needs no checkout. A model-only scope can be narrowed to nothing, and an intersection of the
    files would miss a key only the fleet declares; the union feeds the unknown-key refusal.
    """
    known = sorted(frozenset(ConnectorManifest.model_fields) - _NOT_CONTENT)
    assert known, "ConnectorManifest declares no bundle-level content keys; the scope is now empty"

    # A key only *one* side declares is still compared — the safety/skills shape.
    one_sided = _content_keys({"name": "x", known[0]: ["a"]}, {"name": "x"}, "one-sided")
    assert known[0] in one_sided

    # A key this repository's model does not declare is refused rather than compared and agreed.
    # `extra="forbid"` catches it on this side at load; the fleet's copy is never loaded here.
    with pytest.raises(AssertionError, match="ConnectorManifest"):
        _content_keys({"name": "x"}, {"name": "x", "mount": "backend"}, "unknown key")

    # And the normaliser's own two rules, which decide whether a difference is one at all.
    assert _comparable(None) == _comparable([]), (
        "an absent key and an empty list are one declaration"
    )
    assert _comparable(["b", "a"]) == _comparable(["a", "b"]), (
        "an allow-list's order decides nothing"
    )
    assert _comparable(["a"]) != _comparable(["a", "b"]), "a longer allow-list is a different one"
    assert _comparable([{"name": "j", "queue": "q"}]) != _comparable(
        [{"name": "j", "queue": "r"}]
    ), "two jobs of one name on different queues must not compare equal"


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


#: `deploy/helm/chemclaw/values.yaml`, read for the two maps a fleet server needs to be reachable.
_CHART_VALUES = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "chemclaw" / "values.yaml"


def test_every_fleet_server_has_an_egress_port_and_a_token_slot_in_the_chart() -> None:
    """Every fleet server has an egress port and a token slot in the chart.

    A NetworkPolicy restricts by port independently of its destinations, and without a
    `secrets.optionalKeys` slot the bearer has nowhere to come from. Derived from the fleet's
    `manifests/` and `manifests-internal/`, which name both the port and `auth.token_env`.
    """
    root = _sibling_or_skip()
    values = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8"))
    ports = {int(port) for port in values["networkPolicy"]["egressPorts"].values()}
    slots = set(values["secrets"]["optionalKeys"].values())
    manifests = sorted(root.glob("manifests/*/connector.yaml")) + sorted(
        root.glob("manifests-internal/*/connector.yaml")
    )
    assert manifests, f"{root} holds no fleet manifest; the derivation is broken"
    missing: list[str] = []
    for path in manifests:
        endpoint = _manifest(path)["endpoint"]
        port = int(endpoint["url"].rsplit(":", 1)[1].split("/", 1)[0])
        if port not in ports:
            missing.append(f"{path.parent.name}: port {port} is not in networkPolicy.egressPorts")
        token_env = (endpoint.get("auth") or {}).get("token_env")
        if token_env and token_env not in slots:
            missing.append(f"{path.parent.name}: {token_env} has no secrets.optionalKeys slot")
    assert not missing, "\n".join(missing)
