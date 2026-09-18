"""Two repositories declare one fact three times, and until now nothing compared the copies.

`Chemclaw3-mcp`'s `manifests/README.md` bans a second copy of a declaration *inside* that fleet —
"a copy here would be a second declaration of one fact", which is why every entry there is a
symlink to the server's own `connector.yaml`. Then it ships exactly that across the repository
boundary and asserts the agreement in prose:

1. **`chem`, `rxnpredict` and `safety`** have a `connector.yaml` in *both* trees. Both declare the
   bundle's tool list, its read-only partition and its bearer token variable. Whichever directory
   comes first on `CHEMCLAW_CONNECTORS_DIR` wins the name outright — the loser is not merged, not
   warned about and not logged — so a tool the fleet adds is simply absent in the shipped-first
   order that `infra/live/e2e-full-stack/up.sh` uses, and `connector validation passed` either way.

2. **The `calc` backend seam** has a manifest in *neither* direction that covers it. `calc` is
   `mount: backend`, deliberately unloadable as a connector here, and this repository reaches it
   from inside `science/calc/store.py::cached_compute` — so its physics tool names and their
   argument dicts are hardcoded in `connectors/calc/compose.py` and `remote.py` with nothing
   between them and the server. The fleet records `servers/calc/tool-surface.json` precisely as the
   rename tripwire, and nothing here read it. `tests/calc_server_fake.py` is a hand-written
   reproduction of that same contract, so a fleet rename left the whole suite green and failed at
   runtime, on the seam that carries every calculation this system does.

Measured on 2026-09-07 before any of this was written, both contracts were **sound** — the tool
lists agreed as sets, and zero argument keys were undeclared. That is the finding, not a
counter-argument to it: an agreement nothing checks is one that holds until somebody's merge, and
the whole subject of `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it` is
that a claim about another repository has to be checked by reading that repository.

**And the seam's own tripwire covered the modules it named rather than the seam**
(`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names`). `_CALLERS` was two
paths while this docstring and the test below both said "every hardcoded `calc` call": run against
the tree on 2026-09-14, five modules hold such calls. The two unread ones were
`connectors/calc/server/tools.py` (11 sites) and `connectors/bo/calculators.py` (2) — the half of
the seam carrying `predict_pka`, `predict_solubility` and `compute_xtb_energy`. Every name they put
on the wire is one the fleet records, so nothing was broken; the tripwire simply did not exist
there. `_callers()` derives the list from who imports a dispatcher, and 13 sites became 26.

**And it read the seam in one direction only.** "Every name this repository sends is one the fleet
serves" catches a rename; it is silent about the fleet *growing*, and on 2026-09-18 two of the
tools `servers/calc/tool-surface.json` records were named by no site here. Both turned out to be
declined on purpose and for measured, structural reasons — `optimize_geometry` derives the same
cache key as `relax_structure` while returning a different payload, and `predict_logd` is the one
tool the server answers `calculation_key` with no key for — so nothing was broken and nothing was
written down either. `_DECLINED` is where that goes, reconciled against the derived difference in
both directions, so a ninth tool arriving in the fleet is a decision somebody takes rather than a
silence.

**Opt-in, and it can only skip or fail.** Reading a few YAML files and one JSON file needs a
checkout and no build, which is the property that makes these plausible to run in CI where the
schema measurement in `tests/test_context_floor.py` is not. Without a checkout each skips with the
reason in the message and `tests/conftest.py::_report_sibling_skips` says how many — a skip is not
a pass, and how many it was is what the run says rather than what this paragraph does.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.siblings import (
    REPO_ROOT,
    SIBLING_SKIP,
    bundles_declared_here,
    fleet_published_bundles,
    sibling_root,
)

#: The endpoint fields that decide what a turn may call and how it authenticates — the ones where a
#: disagreement between the two trees changes behaviour rather than documentation.
#:
#: `url` and `health_url` are deliberately absent: hosting is a deployment fact, both manifests say
#: so in their own headers, and `CHEMCLAW_CONNECTOR_URLS` or the chart moves them. `description` is
#: absent for the same reason it is not asserted anywhere else — it is prose a model reads, and its
#: cost is what `tests/test_context_floor.py` bounds.
_SURFACE_FIELDS = ("tools", "read_only", "state_changing")


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
    """The three names both repositories declare must agree on what they serve.

    **As sets, not as lists, and the difference is measured rather than assumed.** `chem`'s twelve
    tools are in a different order in the two files today — `enumerate_torsions` is fifth there and
    twelfth here — and order decides nothing: the list is an allow-list, `_allowed` filters a
    served surface through it, and the model is sent whatever `tools/list` answers. A list
    comparison would have failed on that the day it was written, which is the fastest way to teach
    a reader to bump a check rather than read it.

    The three subjects are **derived** from the two trees rather than transcribed — the names both
    declare — so a fourth port is checked from the commit that lands it, without this file being
    taught the name.
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


# ---------------------------------------------------------------------------------------------
# The `calc` seam: a contract with no manifest on either side.
# ---------------------------------------------------------------------------------------------

#: The functions in `connectors/calc/` that put a tool name and an argument dict on the wire.
_DISPATCHERS = frozenset({"cached_remote", "remote_call", "remote_compute", "_call"})

#: Where this repository's own package lives, so the callers below are found rather than listed.
_SRC = REPO_ROOT / "src"


def _callers() -> tuple[str, ...]:
    """Every module in `src/` that imports a calc dispatcher, as repository-relative paths.

    **Derived, because the hand-kept list covered half the seam while claiming all of it**
    (`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names`). It read
    `compose.py` and `remote.py` — 13 call sites — and the docstring below said "every hardcoded
    `calc` call". Measured against the tree on 2026-09-14 there are **five** modules holding such
    calls: those two, plus `connectors/calc/server/tools.py` (11 sites) and
    `connectors/bo/calculators.py` (2), which were unread. Every tool name they put on the wire is
    one the fleet records today — so nothing was broken, and the tripwire for the half of the seam
    that carries `predict_pka`, `predict_solubility` and `compute_xtb_energy` had simply never
    existed.

    The import is what scopes this, not the function name. `ingest/labels/labeller.py` defines its
    own `_call` — the same spelling as one of `_DISPATCHERS` — against the **rxnlabel** server,
    which has a different surface and no `tool-surface.json`; matching on the name alone would
    check its three call sites against `calc`'s tools and fail on a server it never talks to.
    """
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _DISPATCHER_MODULE:
                if any(alias.name in _DISPATCHERS for alias in node.names):
                    found.append(str(path.relative_to(REPO_ROOT)))
                    break
    return tuple(found)


#: The module the dispatchers are defined in. A caller is a module that imports one from *here* —
#: which is also where `remote.py`'s own `calculation_key` calls live, and they are as hardcoded as
#: `compose.py`'s physics ones and break the same way.
_DISPATCHER_MODULE = "chemclaw.connectors.calc.remote"


_Bindings = dict[str, frozenset[str] | None]


def _literal_strings(node: ast.AST, bound: _Bindings) -> frozenset[str] | None:
    """The string values an expression can take, or `None` when that is not decidable here.

    Three shapes appear at these call sites and all three are decidable: a plain literal; the
    ternary `"compute_fukui_at" if prop == "fukui" else "compute_properties_at"`; and a name bound
    to one of those a few lines above the call. Anything else returns `None` and the caller
    *fails* rather than skipping it — a checker that quietly passes over the call site it cannot
    parse is the shape this whole file exists to end.
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

    Module-wide rather than per-scope, and deliberately so: a name assigned an undecidable value
    *anywhere* maps to `None`, so an ambiguity anywhere makes the call site that uses that name
    fail loudly rather than resolve against a binding from another function.
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


def _hardcoded_calls() -> list[tuple[str, ast.expr, frozenset[str], _Bindings]]:
    """Each `(module, tool expression, argument keys, name bindings)` written literally there.

    A site counts when its *arguments* are a dict literal, because that is what a hardcoded
    contract looks like: the keys are typed into this repository and nothing checks them. A
    dispatcher handed a caller's `arguments` parameter — `remote_compute`'s single `_call`, and
    `cached_remote`'s own body — is a pass-through and declares nothing, so it is not a site.
    """
    callers = _callers()
    # `remote.py` defines the dispatchers rather than importing them, so it is added by name — the
    # one module the derivation above cannot see, and the one whose `calculation_key` call sites
    # were the original reason for a list.
    definer = "src/chemclaw/connectors/calc/remote.py"
    if definer not in callers:
        callers = (*callers, definer)
    sites: list[tuple[str, ast.expr, frozenset[str], _Bindings]] = []
    for relative in sorted(callers):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        bound = _bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", None)
            if name not in _DISPATCHERS:
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
                sites.append((relative, argument, keys, bound))
    return sites


def test_the_calc_seam_calls_only_tools_the_fleet_records_serving() -> None:
    """Every hardcoded `calc` call must name a tool, and only arguments, the server declares.

    `calc` is the one connector with no manifest in either direction — `mount: backend` is what
    makes it unloadable as a connector here, deliberately — so nothing in the seam that carries
    *every* calculation this system runs was checked against the server on the other end. The
    fleet records `servers/calc/tool-surface.json` for exactly this, from its own running server's
    `tools/list`, and this is the reader it never had.

    A renamed tool or a renamed argument fails here, in the pull request that syncs the checkouts,
    rather than at runtime against a pod.
    """
    root = _sibling_or_skip()
    surface: dict[str, dict[str, Any]] = json.loads(
        (root / "servers" / "calc" / "tool-surface.json").read_text(encoding="utf-8")
    )
    sites = _hardcoded_calls()
    assert sites, "no hardcoded calc call site was found; this test now checks nothing"
    for relative, expression, keys, bound in sites:
        tools = _literal_strings(expression, bound)
        assert tools is not None, (
            f"{relative}:{expression.lineno} passes a tool expression this check cannot resolve to "
            "string literals. Either name the tool literally or teach `_literal_strings` the "
            "shape — passing over it would leave the call unchecked while the file reported green."
        )
        for tool in sorted(tools):
            assert tool in surface, (
                f"{relative}:{expression.lineno} calls `{tool}`, which Chemclaw3-mcp's "
                f"servers/calc/tool-surface.json does not record serving: {sorted(surface)}."
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


#: The fleet `calc` tools no hardcoded site here names, and the measured reason each is declined.
#:
#: **A table, because this one can be reconciled and `_CALLERS` could not.** The tuple that
#: `_callers()` replaced was a list of caller modules with nothing on the other side of it: a ninth
#: caller was simply absent, and absence is what no assertion can see. The set of *declined tools*
#: is the opposite shape — the fleet publishes what it serves and `_hardcoded_calls()` derives what
#: is called, so this table is subtracted from a derived difference in both directions on every
#: run. It cannot silently gain a stale row (a tool that starts being called fails), lose a needed
#: one (a tool the fleet adds and nothing calls fails), or outlive its subject (a tool the fleet
#: withdraws fails). The reason string is the part a machine cannot check, which is exactly the
#: part worth writing down.
#:
#: Both reasons are structural rather than preferential, and both were measured on this commit
#: against the fleet checkout rather than read off a comment:
#:
#: * `optimize_geometry` derives the **same** `calc_key` as `relax_structure` —
#:   `identity.COMPUTE_TOOLS` routes both through `_from_spec` with an `OptSpec`, and the key does
#:   not carry the tool name. Driven for `CCO`, `optimize_geometry({"smiles": "CCO"})` and
#:   `relax_structure` on the structure `optimization_inputs` embeds from it produce one identical
#:   string. The two return different payloads — a summary without coordinates, and the full result
#:   with them — so caching either under that key poisons the other, and
#:   `connectors/calc/server/tools.py::optimize_geometry` composes `embed_structure` plus
#:   `relax_structure` instead.
#: * `predict_logd` answers `calculation_key` with **no key at all** (`calc_key=None`, plus a
#:   caveat naming the pKa to key instead), so `cached_remote` refuses it outright as a miswiring
#:   rather than recomputing it forever. `connectors/calc/server/tools.py::predict_logd` calls the
#:   cached `predict_pka` and finishes locally.
_DECLINED: dict[str, str] = {
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


def _tools_named() -> set[str]:
    """Every `calc` tool name the hardcoded sites put on the wire.

    A site whose tool expression cannot be resolved to literals contributes nothing here and is
    *not* reported here either: `test_the_calc_seam_calls_only_tools_the_fleet_records_serving`
    fails on exactly that, and a second assertion about it would be one cause reported twice.
    """
    named: set[str] = set()
    for _relative, expression, _keys, bound in _hardcoded_calls():
        named |= _literal_strings(expression, bound) or frozenset()
    return named


def test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason() -> None:
    """The seam is accounted for in *both* directions, not only in the one that breaks loudly.

    `test_the_calc_seam_calls_only_tools_the_fleet_records_serving` reads the seam from this side:
    every name this repository puts on the wire must be one the fleet serves. That direction
    catches a rename. It cannot catch the other thing a tool surface does — grow. A tool the fleet
    adds that nothing here calls is either a capability this repository is missing or a duplicate
    of something it already composes, and both of those are decisions somebody should take
    deliberately; today they arrive as silence.

    So the difference is derived and reconciled against `_DECLINED`. Nothing here states how many
    tools are served, how many are called, or how many are declined — `tool-surface.json` and
    `_hardcoded_calls()` answer the first two, and the third is whatever is left over.
    """
    root = _sibling_or_skip()
    surface: dict[str, dict[str, Any]] = json.loads(
        (root / "servers" / "calc" / "tool-surface.json").read_text(encoding="utf-8")
    )
    unreached = set(surface) - _tools_named()
    assert unreached == set(_DECLINED), (
        f"{sorted(unreached - set(_DECLINED))} are served by Chemclaw3-mcp's calc server and "
        "named by no hardcoded call site here, with no reason recorded — call them, or add a row "
        f"to `_DECLINED` saying why not. And {sorted(set(_DECLINED) - unreached)} are recorded as "
        "declined while that is no longer the state: either this repository now calls one (delete "
        "its row) or the fleet has withdrawn one (the reason written beside it is about a tool "
        "that no longer exists, and whatever else that reason justified needs re-reading)."
    )


def test_the_composite_this_repository_assembles_is_not_also_served_by_the_fleet() -> None:
    """`compute_thermochemistry` is composed here out of primitives and must not be served there.

    A real invariant rather than a corrected sentence. The fleet's own rule forbids duplicating a
    Chemclaw3 capability, and a `compute_thermochemistry` appearing there would give this family
    two answers to one question — the failure that rule exists to prevent — with nothing in either
    tree noticing.

    This test used to carry a second half, asserting that `predict_logd` *is* served and composed
    here anyway. That half is now `_DECLINED`'s, where it is derived rather than named: a tool this
    repository declines to call is exactly a tool the fleet serves and nothing here reaches, so the
    fleet withdrawing it fails
    `test_every_calc_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason` with the
    reason string it invalidated. Two assertions about one fact is one cause reported twice.
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
    """`tests/calc_server_fake.py` reproduces the real server's surface, so the two are compared.

    The fake is what makes the cache, the composites and the ledger testable without a quantum
    chemistry program, and its own docstring is careful about reproducing properties measured
    against the running server. What it could not do is notice the running server changing: a tool
    renamed in `Chemclaw3-mcp` leaves every test here green and the deployment broken, which is the
    failure mode a hand-written stand-in always has and the reason this assertion is cheap.

    `_KEYED` and `_UNKEYED` are the fake's own declaration of what the server serves — the two
    tables `calculation_key` dispatches on — so they are the honest basis, not the `_<name>`
    methods, which include private helpers and omit the tools an override supplies.
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
