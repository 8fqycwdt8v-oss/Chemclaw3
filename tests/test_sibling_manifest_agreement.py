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

   **And it compared three endpoint keys while claiming the surface.** `_SURFACE_FIELDS` was
   `("tools", "read_only", "state_changing")`, so the five keys a manifest declares *outside* its
   endpoint — `skills`, `profiles`, `note_types`, `relations`, `jobs` — were read by nothing. One of
   the five was diverging the whole time: `safety` declares `skills: [safety-screening]` here and no
   `skills:` key in the fleet, and in the wiring order both of that repository's own documents
   publish, the manifest with no skills won the name and the 132-line SKILL.md carrying *why an
   empty result is never "safe"* became unreachable — silently, with `CHEMCLAW_SKILLS_DIR` the only
   remedy and no document in either repository naming it. That is fixed at the mechanism
   (`connectors/registry._bundle_content_dirs` reads every directory carrying an enabled bundle's
   name, not only the winner's), and the comparison now covers every bundle-level key, derived from
   `ConnectorManifest` so a sixth is compared the day it is added. A divergence is either caught or
   written down in `_ARGUED_DIVERGENCES` with what makes it harmless.

2. **The backend seams** have a manifest in *neither* direction that covers them. `calc` is
   `mount: backend`, deliberately unloadable as a connector here, and this repository reaches it
   from inside `science/calc/store.py::cached_compute` — so its physics tool names and their
   argument dicts are hardcoded in `connectors/calc/compose.py` and `remote.py` with nothing
   between them and the server. The fleet records `servers/calc/tool-surface.json` precisely as the
   rename tripwire, and nothing here read it. `tests/calc_server_fake.py` is a hand-written
   reproduction of that same contract, so a fleet rename left the whole suite green and failed at
   runtime, on the seam that carries every calculation this system does.

   **There are two such seams, and for nine days this file said there was one.** `rxnlabel` is the
   other `manifests-internal/` backend — `ingest/labels/labeller.py` puts three tool names and their
   argument keys on its wire — and `_callers`' docstring gave "a different surface and **no
   `tool-surface.json`**" as the reason it was out of scope. That file has shipped in
   `servers/rxnlabel/` since 2026-09-05; the sentence was written on 2026-09-14, in a wave titled
   "the cross-repo contracts", in the file whose whole subject is
   `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it`. So the recorded surface
   existed, the reason for not reading it was false, and the seam carrying every atom map and every
   reaction name in the corpus had no tripwire. Re-measured when the reader was added: three call
   sites, every tool served, no undeclared argument, no required argument missing — sound, and
   unchecked, which is the same pair `calc` was in on 2026-09-07. A seam is a **value** now
   (`_Seam`), so the next one is a row rather than a paragraph explaining its absence.

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
written down either. A per-seam declined table is where that goes, reconciled against the derived
difference in both directions, so a tool arriving in the fleet is a decision somebody takes rather
than a silence.

**Opt-in, and it can only skip or fail.** Reading a few YAML files and one JSON file needs a
checkout and no build, which is the property that makes these plausible to run in CI where the
schema measurement in `tests/test_context_floor.py` is not. Without a checkout each skips with the
reason in the message and `tests/conftest.py::_report_sibling_skips` says how many — a skip is not
a pass, and how many it was is what the run says rather than what this paragraph does.
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

#: The endpoint fields that decide what a turn may call and how it authenticates — the ones where a
#: disagreement between the two trees changes behaviour rather than documentation.
#:
#: `url` and `health_url` are deliberately absent: hosting is a deployment fact, both manifests say
#: so in their own headers, and `CHEMCLAW_CONNECTOR_URLS` or the chart moves them. `description` is
#: absent for the same reason it is not asserted anywhere else — it is prose a model reads, and its
#: cost is what `tests/test_context_floor.py` bounds.
_SURFACE_FIELDS = ("tools", "read_only", "state_changing")


#: The three keys `_SURFACE_FIELDS` and the `auth` assertion cover, or that decide nothing. Every
#: other key of either manifest is bundle-level content and is compared.
_NOT_CONTENT = frozenset({"name", "description", "endpoint"})


def _content_keys(mine: dict[str, Any], theirs: dict[str, Any], where: str) -> tuple[str, ...]:
    """Every *bundle-level* key to compare for one pair of manifests, from two directions at once.

    `_SURFACE_FIELDS` covers the endpoint. Everything a manifest declares outside it — `skills`,
    `profiles`, `note_types`, `relations`, `jobs` — went uncompared, and one of the five was
    diverging: `safety` declares `skills: [safety-screening]` here and no `skills:` key in the
    fleet. That divergence is argued and harmless (see `_ARGUED_DIVERGENCES`), and the reason to
    compare all five anyway is that nothing could tell an argued one from an accident. A
    `note_types` or `relations` key the fleet gained and this tree did not would take a note type
    out of `make kg-validate`'s vocabulary in whichever order wins; a `jobs` divergence would move a
    durable launcher and its `connector-<name>` queue.

    **The scope is the union of what the model declares and what the two files declare, and that is
    the second version of this function.** The first read `ConnectorManifest.model_fields` alone,
    which is a derivation and was still unanchored: emptying it — or narrowing it to a tuple that
    happens to omit `skills` — turned the whole comparison, *including the stale-row half that is
    supposed to notice such a change*, into a loop over nothing, and the mutation ran green. Driven.
    Taking the keys off the files as well means the subject population is the thing under test:
    while either manifest declares `skills:`, `skills` is compared, whatever this repository's model
    says this week.

    The model half is kept because it is what makes a *newly added* key compared before either file
    uses it, and because it is the basis for refusing a key neither side's model knows — `extra=
    "forbid"` refuses one at load time here, but the fleet's copy is never loaded by this process,
    so a key nothing understands would otherwise be compared and agree.
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

    A list of strings is an allow-list and its order decides nothing — the same argument
    `test_a_bundle_declared_in_both_trees_declares_the_same_surface` makes for `tools`, where the
    two files genuinely differ in order today. A list of mappings is the `jobs:` block, keyed by
    each job's `name` because that name is the launcher's tool name and therefore its identity.
    A missing key and an empty list are the same declaration, which is what makes `skills:` absent
    comparable to `skills: []`.
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


#: The argument for every process-development bundle's `default_enabled` divergence, written once
#: because it is one argument: five rows used to carry five copies of it, all five stating that
#: `infra/live/e2e-full-stack/up.sh` lets the fleet's copy win and bind — which that script's own
#: directory order makes false. `test_the_e2e_lane_binds_no_opt_in_bundle_by_default` derives the
#: harmlessness claim from the script instead of restating it.
_OPT_IN_ARGUMENT = (
    "this tree declares `default_enabled: false` and the fleet's copy declares nothing, which "
    "means True there — and the divergence is the decision rather than a drift "
    "(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). The flag "
    "answers a question only this repository has: what an *empty* `connectors_enabled` should "
    "bind, given that every bound tool's schema is charged to `PREFIX_BOUND` and through it to "
    "both compaction defaults. The fleet publishes a capability and has no prefix to protect.\n"
    "\n"
    "What makes it harmless is that the flag is read only when `connectors_enabled` is empty "
    "(`registry.enabled`), and no wiring this repository ships reads the fleet's copy there. "
    "**Not** because this copy always wins the name: a chart release that mounts the fleet's "
    "manifest through `extraConnectors` puts it *ahead* of the shipped bundles on "
    "`CHEMCLAW_CONNECTORS_DIR`, and `registry._bundle_dirs` is first-directory-wins, so there the "
    "fleet's copy is the one loaded. A chart release never reads the flag at all, though — "
    "`chemclaw.connectorsEnabled` refuses to render an empty `CHEMCLAW_CONNECTORS_ENABLED` — so "
    "which copy wins decides nothing there. `infra/live/e2e-full-stack/up.sh` is the one wiring "
    "that leaves `connectors_enabled` empty *and* mounts the fleet's `manifests/`, and it puts "
    "this tree's connectors directory first, so this copy wins and the empty list binds none of "
    "these bundles; the server `processes.sh` starts for one runs unused. Binding one is naming it "
    "in `CHEMCLAW_CONNECTORS_ENABLED`, which overrides the flag."
)


#: Bundle-level keys that legitimately differ between the two trees, keyed `(bundle, key)`, each
#: carrying **why** and **what makes it harmless**. A divergence is therefore either caught or
#: written down — which is the property the comparison existed to have and did not, because it only
#: ever looked at three endpoint keys.
#:
#: The one row is real and was measured: every other key of every shared bundle agrees today.
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
    ("props", "default_enabled"): _OPT_IN_ARGUMENT,
    ("thermalsafety", "default_enabled"): _OPT_IN_ARGUMENT,
    ("thermalsafety", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `thermalsafety`'s tools is architecture layer 3 and lives here, and that "
        "fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `thermal-safety-assessment` is reachable in either wiring "
        "order."
    ),
    ("kinetics", "default_enabled"): _OPT_IN_ARGUMENT,
    ("kinetics", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `kinetics`'s tools is architecture layer 3 and lives here, and that fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `kinetics-and-reactor-choice` is reachable in either "
        "wiring order."
    ),
    ("unitops", "default_enabled"): _OPT_IN_ARGUMENT,
    ("unitops", "skills"): (
        "the same split `safety` above records, for the same reason and with the same remedy: the "
        "judgment about `unitops`'s tools is architecture layer 3 and lives here, and that fleet "
        "has no equivalent seam to declare it in. `_bundle_content_dirs` reads every directory "
        "carrying the bundle's name, so `unit-operation-sizing` is reachable in either wiring "
        "order."
    ),
    ("suitability", "default_enabled"): _OPT_IN_ARGUMENT,
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

    Three things are compared and the third one is new. The endpoint's `_SURFACE_FIELDS` and its
    `auth.token_env` decide what a turn may call and how it authenticates; **everything else a
    manifest declares** — `_content_keys`, derived rather than transcribed — decides what judgment,
    which agent profiles, which note types, which relations and which durable launchers a deployment
    reaches. Only the first two were read for as long as this file existed, and `safety.skills` was
    diverging the whole time.

    **As sets, not as lists, and the difference is measured rather than assumed.** `chem`'s twelve
    tools are in a different order in the two files today — `enumerate_torsions` is fifth there and
    twelfth here — and order decides nothing: the list is an allow-list, `_allowed` filters a
    served surface through it, and the model is sent whatever `tools/list` answers. A list
    comparison would have failed on that the day it was written, which is the fastest way to teach
    a reader to bump a check rather than read it.

    The bundles themselves are **derived** from the two trees rather than transcribed — the names
    both declare — so a fourth port is checked from the commit that lands it, without this file
    being taught the name.
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


#: The script whose directory order `_OPT_IN_ARGUMENT` makes a claim about.
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


def test_the_e2e_lane_binds_no_opt_in_bundle_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every bundle `_OPT_IN_ARGUMENT` excuses is unbound under `up.sh`'s own wiring.

    The rows used to say the opposite — that in the four-repo lane the fleet's copy wins the name
    and these bundles bind — and nothing checked it: `up.sh` lists this tree's connectors first and
    discovery is first-directory-wins, so this tree's `default_enabled: false` decides there too.
    Driven through `registry.enabled()` with the exported directory order and no enable-list, which
    is how `processes.sh` runs the front door.
    """
    from chemclaw.connectors import registry
    from chemclaw.core.config import settings

    fleet = _sibling_or_skip()
    opt_in = {bundle for (bundle, _), why in _ARGUED_DIVERGENCES.items() if why is _OPT_IN_ARGUMENT}
    monkeypatch.setattr(settings, "connectors_dir", _e2e_connectors_dir(fleet))
    monkeypatch.setattr(settings, "connectors_enabled", "")
    bound = {manifest.name for manifest in registry.enabled()}
    assert opt_in, "no row carries `_OPT_IN_ARGUMENT`, so this test checks nothing"
    assert not opt_in & bound, (
        f"{sorted(opt_in & bound)} bind in the e2e lane's wiring with no enable-list, so "
        "`_OPT_IN_ARGUMENT`'s harmlessness claim — this tree's copy wins the name everywhere — is "
        f"false. {_E2E_UP} has changed its CHEMCLAW_CONNECTORS_DIR order; rewrite the argument."
    )


def test_the_compared_key_set_is_anchored_in_both_the_model_and_the_two_files() -> None:
    """The bundle-level comparison's scope, pinned from both directions it is built from.

    Needs no checkout: it is about `_content_keys` rather than about the manifests, which is exactly
    why it can hold the half the real fixture cannot reach. Two independent narrownesses were
    measured as surviving mutations of the comparison above, and each has an assertion here:

    * **The model half alone is not an anchor.** Deriving the scope from
      `ConnectorManifest.model_fields` only meant a narrowed tuple emptied the comparison *and* the
      check that was supposed to notice, together. So a key a file declares is compared whatever the
      model says.
    * **The file half alone is not an anchor either**, and its own purpose is invisible to the real
      pair: taking the *intersection* of the two files rather than the union left every real
      assertion green, because the fleet's manifest is never loaded by this process and a key only
      it declares would be compared against nothing. That is what the `unknown` refusal is for,
      and the union is what feeds it.
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

    **A value rather than four module constants, because there is more than one such seam and the
    second one had no tripwire for nine days while this file's own prose said it could not have
    one** (`D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it` applied to this
    file). `_callers`' docstring said `rxnlabel` has "a different surface and **no
    `tool-surface.json`**"; that server has shipped `servers/rxnlabel/tool-surface.json` since
    2026-09-05, and the sentence was written on 2026-09-14 — in a wave titled "the cross-repo
    contracts", in the file whose subject is that ADR. The recorded surface existed and nothing read
    it, so a top-level rename on either side was caught by nobody. Making the seam a value is what
    stops the next one being described instead of checked.

    Attributes:
        name: What this seam is called, for a failure message and for the declined table beside it.
        module: The dotted module that *defines* the dispatchers. Resolved through `find_spec`, so
            renaming it fails loudly here rather than silently emptying this seam's caller set —
            `tasks/lessons.md`'s "derive the scope, do not assert that it is non-empty".
        dispatchers: The function or method names that put `(tool, arguments)` on the wire.
        surface: The fleet-relative path of the `tool-surface.json` that server records.
        declined: Tools the fleet serves that nothing here calls, each with the measured reason.
    """

    name: str
    module: str
    dispatchers: frozenset[str]
    surface: tuple[str, ...]
    declined: Mapping[str, str]


def _module_path(dotted: str) -> str:
    """One dotted module as a repository-relative path, or a failure naming what moved.

    `find_spec` rather than a path built from the dots, because that is the form a rename cannot
    survive quietly: a moved module empties a `rglob` filter and leaves every assertion downstream
    trivially satisfied, which is the hole `tasks/lessons.md` records as "derive the scope, do not
    assert that it is non-empty".
    """
    spec = importlib.util.find_spec(dotted)
    assert spec is not None and spec.origin is not None, (
        f"{dotted} does not resolve, so the seam it defines has no caller set and every check "
        "over it would pass vacuously. If the module moved, move this name with it."
    )
    return str(Path(spec.origin).relative_to(REPO_ROOT))


def _callers(seam: _Seam) -> tuple[str, ...]:
    """Every module in `src/` that imports one of `seam`'s dispatchers, plus the module defining it.

    **Derived, because the hand-kept list covered half the seam while claiming all of it**
    (`D-2026-09-14-a-tripwire-over-two-named-modules-covers-the-modules-it-names`). It read
    `compose.py` and `remote.py` — 13 call sites — and the docstring below said "every hardcoded
    `calc` call". Measured against the tree on 2026-09-14 there are **five** modules holding such
    calls: those two, plus `connectors/calc/server/tools.py` (11 sites) and
    `connectors/bo/calculators.py` (2), which were unread. Every tool name they put on the wire is
    one the fleet records today — so nothing was broken, and the tripwire for the half of the seam
    that carries `predict_pka`, `predict_solubility` and `compute_xtb_energy` had simply never
    existed.

    The import is what scopes this, not the function name — and the scoping is per seam, which is
    the whole reason a seam is a value. `ingest/labels/labeller.py` defines its own `_call`, the
    same spelling `calc` uses, against a server with an entirely different surface: matching on the
    name alone would check its three call sites against `calc`'s tools and fail on a server it never
    talks to. Each seam is therefore read against **its own** `tool-surface.json`.

    The defining module is added unconditionally, because it does not import what it defines, and
    its own call sites are as hardcoded as any importer's — `remote.py`'s `calculation_key` calls
    were the original reason this was ever a list.
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


def _typed_tools(seam: _Seam) -> dict[str, frozenset[str]]:
    """Each module-level dispatcher whose `tool` parameter is a `Literal`, with its members.

    **The resolution for a call site whose tool expression is not a literal, and the reason it is
    sound is the type checker rather than this file.** `remote_version` is reached from
    `connectors/calc/server/tools.py::_calibrated` with a value looked up in `_CALIBRATED`, which no
    AST walk can resolve without learning that one module's private table — the
    allowlist-of-its-own-exceptions shape. Typing the parameter moves the declaration onto the
    dispatcher instead: `mypy --strict` proves every value passed is a member, so the members *are*
    what that site can put on the wire, and every one of them is checked against the fleet below.

    A dispatcher that is a method (`rxnlabel`'s `_call`) or whose `tool` is a plain `str` has no
    entry, so its sites resolve as before or fail loudly as before.
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

    A site counts when its *arguments* are a dict literal, because that is what a hardcoded
    contract looks like: the keys are typed into this repository and nothing checks them. A
    dispatcher handed a caller's `arguments` parameter — `remote_compute`'s single `_call`, and
    `cached_remote`'s own body — is a pass-through and declares nothing, so it is not a site.
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

    One body over two seams, because the check is identical and a second copy is the shape that
    drifts: the `calc` half was written first and the `rxnlabel` half was, for nine days, a sentence
    in `_callers`' docstring saying no surface existed to check.

    A renamed tool or a renamed argument fails here, in the pull request that syncs the checkouts,
    rather than at runtime against a pod.
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


#: The fleet `rxnlabel` tools no hardcoded site here names, and the measured reason each is
#: declined. The same shape as `_CALC_DECLINED` and reconciled the same way, which is what makes
#: this seam's second direction an accounting rather than a silence.
#:
#: Both rows are one decision read twice, and it is `ingest/labels/labeller.py`'s own: *"The batch
#: tools are the ones the drain calls. A 13M-row corpus at one round trip per reaction is 13M round
#: trips; at `label_batch_size` it is 65,000. The single-reaction tools exist on the server for a
#: person asking about one reaction, and are not called from here."* Declining them is therefore not
#: a gap — the batch tool is strictly more general, and a caller that wanted one reaction would send
#: a batch of one.
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

#: `rxnlabel` is a backend exactly as `calc` is — `manifests-internal/`, `mount: backend`, no
#: manifest on this side — so the same argument that made the `calc` seam worth reading applies to
#: it verbatim, and it had no reader. Its dispatcher is a *method*, `RxnLabelServer._call`, which is
#: why a seam carries its defining module: `labeller.py` imports nothing and would be invisible to
#: an importer walk.
_RXNLABEL_SEAM = _Seam(
    name="rxnlabel",
    module="chemclaw.ingest.labels.labeller",
    dispatchers=frozenset({"_call"}),
    surface=("servers", "rxnlabel", "tool-surface.json"),
    declined=_RXNLABEL_DECLINED,
)


def _tools_named(seam: _Seam) -> set[str]:
    """Every tool name `seam`'s hardcoded sites put on the wire.

    A site whose tool expression cannot be resolved to literals contributes nothing here and is
    *not* reported here either: the check above fails on exactly that, and a second assertion about
    it would be one cause reported twice.
    """
    typed = _typed_tools(seam)
    named: set[str] = set()
    for _relative, dispatcher, expression, _keys, bound in _hardcoded_calls(seam):
        named |= _site_tools(typed, dispatcher, expression, bound) or frozenset()
    return named


def _assert_every_served_tool_is_called_or_declined(seam: _Seam) -> None:
    """The seam is accounted for in *both* directions, not only in the one that breaks loudly.

    The check above reads the seam from this side: every name this repository puts on the wire must
    be one the fleet serves. That direction catches a rename. It cannot catch the other thing a tool
    surface does — grow. A tool the fleet adds that nothing here calls is either a capability this
    repository is missing or a duplicate of something it already composes, and both of those are
    decisions somebody should take deliberately; today they arrive as silence.

    So the difference is derived and reconciled against the seam's declined table. Nothing here
    states how many tools are served, how many are called, or how many are declined —
    `tool-surface.json` and `_hardcoded_calls` answer the first two, and the third is what is left.
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
    """`calc`, the seam that carries every calculation this system runs.

    It is the one connector with no manifest in either direction — `mount: backend` is what makes it
    unloadable as a connector here, deliberately — so nothing in it was checked against the server
    on the other end until the fleet's `servers/calc/tool-surface.json` gained a reader.
    """
    _assert_every_call_names_a_served_tool(_CALC_SEAM)


def test_every_tool_the_calibration_table_names_is_one_the_calc_seam_checks() -> None:
    """`_CALIBRATED`'s tool names are exactly what the seam walker reads at `remote_version`.

    Needs no fleet checkout, which is the point: the two tests above skip without one, and this is
    the half that says they would have *seen* a calibrated tool had they run. The table used to put
    its names on the wire through `remote_version`, which was not a dispatcher here, with a
    tuple-unpacked local no walker could resolve — so a third calibrated row naming a tool the fleet
    does not serve was checked by nothing. Measured when that was found: both names the table held
    were also named literally at other sites, so nothing was unchecked yet and a new row would be.

    Equality rather than a subset, in both directions and for two different failures: a table row
    naming a tool outside `CalibratedTool` puts a name on the wire the walker does not attribute to
    any site, and a `CalibratedTool` member no row uses is a name the walker counts as *called* —
    which would quietly satisfy the declined-table accounting for a tool nothing reaches.
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
    """`rxnlabel`, the second backend seam, which had no reader although its surface existed.

    `servers/rxnlabel/tool-surface.json` has shipped since 2026-09-05. This file's own prose said it
    did not — written 2026-09-14, in a wave about the cross-repo contracts — so the seam carrying
    every atom map and every reaction name in the corpus was checked by nothing, and a top-level
    rename on either side would have been caught by nobody. Measured when this test was written:
    three call sites, every tool served, no undeclared argument, no required argument missing. That
    is the finding rather than a reason not to check it, exactly as it was for `calc`.
    """
    _assert_every_call_names_a_served_tool(_RXNLABEL_SEAM)


def test_every_rxnlabel_tool_the_fleet_serves_is_called_here_or_declined_with_a_reason() -> None:
    """The `rxnlabel` seam's other direction — its two single-reaction tools are declined."""
    _assert_every_served_tool_is_called_or_declined(_RXNLABEL_SEAM)


def test_the_composite_this_repository_assembles_is_not_also_served_by_the_fleet() -> None:
    """`compute_thermochemistry` is composed here out of primitives and must not be served there.

    A real invariant rather than a corrected sentence. The fleet's own rule forbids duplicating a
    Chemclaw3 capability, and a `compute_thermochemistry` appearing there would give this family
    two answers to one question — the failure that rule exists to prevent — with nothing in either
    tree noticing.

    This test used to carry a second half, asserting that `predict_logd` *is* served and composed
    here anyway. That half is now `_CALC_DECLINED`'s, where it is derived rather than named: a tool
    this repository declines to call is exactly a tool the fleet serves and nothing here reaches, so
    the
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
