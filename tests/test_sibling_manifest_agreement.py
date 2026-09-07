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

#: The modules that hold those call sites. Both, because `remote.py`'s own `calculation_key` calls
#: are as hardcoded as `compose.py`'s physics ones and break the same way.
_CALLERS = (
    "src/chemclaw/connectors/calc/compose.py",
    "src/chemclaw/connectors/calc/remote.py",
)


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
    sites: list[tuple[str, ast.expr, frozenset[str], _Bindings]] = []
    for relative in _CALLERS:
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
