"""`tests/calc_server_fake.py`'s cache-key table, held against the server it stands in for.

Every D-011 assertion in `tests/test_calc_*.py` — "a persisted result is never recomputed", "the
composite reached the entry that conformer's own address names" — is evidence about
`calc_server_fake._KEYED`, a hand-written mirror of `Chemclaw3-mcp`'s
servers/calc/src/chemclaw_mcp_calc/engine/identity.py (unbackticked deliberately: it is that
repository's path, and `tests/test_docstring_paths.py` resolves every backticked pointer against
this one). That file's docstring says the two
properties in it "were measured against the running server", which is a claim about a commit in a
repository this one does not build, and nothing re-measured it. A fake whose key table has drifted
makes those tests pass on a design that fails in production: the two ways it can be wrong are a
`calc_type` that no longer aliases the tools the composites rely on sharing a row, and an argument
that entered (or left) the key, which is a cache hit where production recomputes or the reverse.

**So this re-measures it, in the sibling's own interpreter**, exactly as
`tests/test_context_floor.py::test_the_allowance_for_the_bundles_this_ratchet_cannot_serve_is_still_a_bound`
holds that repository's tool schemas to account — and it reuses that file's `_sibling_python`, so
where the checkout lives has one definition. What crosses the process boundary is JSON.

**What is compared is behaviour, never values.** The fake's `calc_version` is a fixture string and
its `input_hash` is its own arithmetic, so identical keys are neither expected nor wanted. What
must agree is the matrix: which `calc_type` each tool answers under, and *which arguments change
the key* — because those two are the whole of "did this hit the cache".

**A skip is not a pass.** With no sibling checkout this test skips with the reason in the message,
because a check that quietly narrows to what it can reach is worse than one that says what it did
not look at.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests.calc_server_fake import _KEYED, _UNKEYED, embed
from tests.test_context_floor import _sibling_python

#: The program run inside the sibling checkout's interpreter. It derives one identity per compute
#: tool from a fixed argument set, then re-derives it once per argument with that argument changed,
#: and reports which arguments moved the key. Written here rather than committed there for the
#: reason `_SIBLING_DUMP` gives: this is *this* repository's measurement of a contract it depends
#: on, and the sibling owes the fleet a derivation rather than a table in this shape.
_KEY_PROBE = """
import json, sys
from chemclaw_mcp_calc.engine.identity import COMPUTE_TOOLS, calculation_identity

request = json.load(sys.stdin)
variations = request["variations"]


def base(tool):
    accepts = COMPUTE_TOOLS[tool][0]
    args = {}
    for name in ("structure", "smiles", "atoms", "value"):
        if name in accepts:
            args[name] = request[name]
    return args


answers = {}
for tool in sorted(COMPUTE_TOOLS):
    accepts = sorted(COMPUTE_TOOLS[tool][0])
    entry = {"accepts": accepts}
    try:
        identity = calculation_identity(tool, base(tool))
    except Exception as error:
        entry["error"] = "%s: %s" % (type(error).__name__, error)
        answers[tool] = entry
        continue
    key = identity.key
    entry["calc_type"] = None if key is None else key.calc_type
    keyed, unmeasured = [], {}
    if key is not None:
        for name in accepts:
            if name in ("smiles", "structure"):
                continue
            if name not in variations:
                unmeasured[name] = "this test declared no second value for it"
                continue
            args = dict(base(tool))
            args[name] = variations[name]
            try:
                other = calculation_identity(tool, args).key
            except Exception as error:
                unmeasured[name] = "%s: %s" % (type(error).__name__, error)
                continue
            moved = other is None or (other.input_hash, other.params_hash) != (
                key.input_hash,
                key.params_hash,
            )
            if moved:
                keyed.append(name)
    entry["keyed"] = keyed
    entry["unmeasured"] = unmeasured
    answers[tool] = entry
print(json.dumps(answers))
"""

#: A second value for every argument the compute tools take. Chosen to be *valid* — the server
#: canonicalises and validates exactly as the compute path does, so a nonsense value is refused
#: rather than answered, and a refusal is recorded as unmeasured rather than read as "not keyed".
_VARIATIONS: dict[str, Any] = {
    "solvent": "water",
    "charge": 1,
    "frozen_atoms": [0],
    "mode": "electrophilic",
    "search": "protomers",
    "effort": "thorough",
    "temperature_k": 400.0,
    "ph": 5.0,
    "atoms": [0, 2],
    "value": 1.9,
}

#: The tools whose identity the sibling refuses to derive without the `crest` binary, by design
#: (`crest_search.require_crest()` — "the probe refuses precisely where the calculation would").
#: A bound rather than a list of expected refusals: a refusal from any *other* tool is a real
#: divergence and fails below, and where a checkout does have crest these two are measured like
#: everything else.
_REFUSED_WITHOUT_CREST = frozenset({"search_conformer_ensemble", "search_binding_modes"})

#: Arguments the server will not let a probe vary independently of the subject, with the reason.
#: `charge` is folded into the *structure* on that side (it embeds the molecule and refuses a
#: declared charge that disagrees with the formal one), so "does charge change the key" cannot be
#: asked without changing the molecule too; the fake puts it in `params` instead, which produces
#: the same hit/miss behaviour and is what `calc_server_fake`'s own docstring already records.
#: `atoms` names a *bond* to drive, so a second pair is a different constraint rather than a
#: different value of the same one.
_NOT_INDEPENDENTLY_VARIABLE = {("compute_xtb_energy", "charge"), ("scan_point", "atoms")}


def _sibling_identities() -> tuple[dict[str, Any], str]:
    """The sibling's per-tool identity matrix, or an empty mapping and the reason there is none.

    Raises for nothing: a checkout somebody has not built is a fact about their machine, and the
    caller decides between skipping and failing.
    """
    interpreter, reason = _sibling_python()
    if interpreter is None:
        return {}, reason
    request = {
        "smiles": "CCO",
        "structure": embed("CCO"),
        "atoms": [0, 1],
        "value": 1.6,
        "variations": _VARIATIONS,
    }
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _KEY_PROBE],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(interpreter.parents[2]),
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment
        return {}, f"could not run the sibling's interpreter: {error}"
    if completed.returncode != 0:
        return {}, f"the sibling's identity probe failed: {completed.stderr.strip()[-400:]}"
    try:
        answers: dict[str, Any] = json.loads(completed.stdout)
    except ValueError as error:  # pragma: no cover - environment
        return {}, f"the sibling's probe was not JSON: {error}"
    return answers, ""


def test_the_fake_keys_calculations_the_way_the_server_keys_them() -> None:
    """`_KEYED` names the same `calc_type` and the same keyed arguments the server derives.

    Three ways this can be wrong, and each is a class of test in `tests/test_calc_*.py` that would
    stay green while production behaved differently:

    - a tool the fake keys and the server does not (or the reverse) — a cache row that exists on
      one side only;
    - a `calc_type` that differs — the aliasing `compute_properties_at`/`compute_electronic_
      properties` and `compute_fukui_at`/`predict_site_reactivity` rely on, which is what makes
      "relax a conformer, then ask for its properties" reach the entry the conformer's own address
      names;
    - an argument in one key table and not the other — a hit where production recomputes, or a
      recompute where production would have served a stale row.
    """
    answers, reason = _sibling_identities()
    if not answers:
        pytest.skip(
            "the calc server's own key derivation was NOT measured: "
            f"{reason}. `tests/calc_server_fake._KEYED` is a hand-written mirror of it, so every "
            "D-011 'a persisted result is never recomputed' assertion in this suite is unchecked "
            "against the server in this run."
        )

    refused = {tool: entry["error"] for tool, entry in answers.items() if "error" in entry}
    assert set(refused) <= _REFUSED_WITHOUT_CREST, (
        "the sibling refused to derive an identity for "
        f"{sorted(set(refused) - _REFUSED_WITHOUT_CREST)}: {refused}. Only the CREST searches may "
        "refuse, and only for a missing binary"
    )

    # Coverage, both directions. A tool the server keys must be in the fake's table, or a fake
    # asked for its key answers "not a compute tool on this server" where production answers.
    keyed_on_server = {tool for tool, entry in answers.items() if entry.get("calc_type")}
    assert set(_KEYED) == keyed_on_server | set(refused), (
        f"the fake keys {sorted(set(_KEYED) - (keyed_on_server | set(refused)))} which the server "
        f"does not, and does not key {sorted((keyed_on_server | set(refused)) - set(_KEYED))} "
        "which it does"
    )
    # And the keyless half: `predict_logd` has a version and no key on both sides, while the two
    # geometry builders are not calculations at all — the server does not know them here.
    keyless_on_server = {
        tool
        for tool, entry in answers.items()
        if "error" not in entry and entry.get("calc_type") is None
    }
    assert keyless_on_server <= _UNKEYED, (
        f"{sorted(keyless_on_server - _UNKEYED)} have no key on the server and the fake gives them"
        " one"
    )
    assert not (_UNKEYED & set(answers)) - keyless_on_server, (
        f"{sorted((_UNKEYED & set(answers)) - keyless_on_server)} are keyed compute tools on the "
        "server and unkeyed here"
    )

    # The matrix itself, one row per tool the sibling could measure.
    divergences: list[str] = []
    for tool, (calc_type, keyed) in sorted(_KEYED.items()):
        entry = answers.get(tool)
        if entry is None or "error" in entry:
            continue
        if entry["calc_type"] != calc_type:
            divergences.append(
                f"{tool}: calc_type {calc_type!r} here, {entry['calc_type']!r} there"
            )
        unmeasured = set(entry["unmeasured"]) | {
            name for named, name in _NOT_INDEPENDENTLY_VARIABLE if named == tool
        }
        here = set(keyed) - unmeasured
        there = set(entry["keyed"]) - unmeasured
        if here != there:
            divergences.append(
                f"{tool}: this table keys on {sorted(here)}, the server on {sorted(there)} "
                f"(unmeasured: {sorted(unmeasured)})"
            )
    assert not divergences, (
        "`tests/calc_server_fake._KEYED` no longer describes the server it stands in for:\n  "
        + "\n  ".join(divergences)
        + "\nEvery cache assertion in tests/test_calc_*.py is evidence about this table, so fix "
        "the table (and check what the change means for the composites that share a row)."
    )
