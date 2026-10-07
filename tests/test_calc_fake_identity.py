"""`tests/calc_server_fake.py`'s cache-key table, held against the server it stands in for.

Every D-011 assertion in `tests/test_calc_*.py` is evidence about `calc_server_fake._KEYED`, a
mirror of `Chemclaw3-mcp`'s `engine/identity.py`. This re-measures it in the sibling's own
interpreter (via `_sibling_python`, as `tests/test_context_floor.py` does), exchanging JSON. What
is compared is behaviour, not values: which `calc_type` each tool answers under, and which
arguments change the key. Without a sibling checkout it skips with the reason.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests.calc_server_fake import _KEYED, _UNKEYED, embed
from tests.siblings import SIBLING_SKIP
from tests.test_context_floor import _sibling_python

#: The program run inside the sibling checkout's interpreter. It derives one identity per compute
#: tool from a fixed argument set, then re-derives it with each argument changed in turn, and
#: reports which arguments moved the key.
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

#: Tools whose identity the sibling refuses to derive when the binary behind them (`crest` or `xtb`)
#: is absent, by design: a key naming a program the pod lacks addresses a row that can never be
#: computed. A bound, not an expected list: any other refusal fails below, and where the binaries
#: exist these are measured like everything else.
_REFUSED_WITHOUT_A_BINARY = frozenset(
    {
        # `crest`
        "search_conformer_ensemble",
        "search_binding_modes",
        # the `xtb` binary, which `tblite` does not stand in for
        "compute_atomic_descriptors",
        "compute_surface_potential",
    }
)

#: Arguments the server will not let a probe vary independently of the subject. `charge` is folded
#: into the structure there (the fake puts it in `params`, with the same hit/miss behaviour), and
#: `atoms` names the bond to drive, so a second pair is a different constraint.
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

    Three ways this can be wrong while the calc tests stay green:

    - a tool keyed on one side only;
    - a different `calc_type`, breaking the aliasing (`compute_properties_at` /
      `compute_electronic_properties`, `compute_fukui_at` / `predict_site_reactivity`) that lets a
      relaxed conformer's properties reach its own entry;
    - an argument in one key table and not the other — a hit where production recomputes, or the
      reverse.
    """
    answers, reason = _sibling_identities()
    if not answers:
        pytest.skip(
            # The marker, so `tests/conftest.py`'s epilogue counts this one. It did not: the reason
            # was worded from scratch, and the epilogue that exists to say what a run is *not*
            # evidence about was silent about the most load-bearing skip in the set.
            f"{SIBLING_SKIP} the calc server's own key derivation was NOT measured: "
            f"{reason}. `tests/calc_server_fake._KEYED` is a hand-written mirror of it, so every "
            "D-011 'a persisted result is never recomputed' assertion in this suite is unchecked "
            "against the server in this run."
        )

    refused = {tool: entry["error"] for tool, entry in answers.items() if "error" in entry}
    assert set(refused) <= _REFUSED_WITHOUT_A_BINARY, (
        "the sibling refused to derive an identity for "
        f"{sorted(set(refused) - _REFUSED_WITHOUT_A_BINARY)}: {refused}. Only the tools that shell "
        "out to a binary may refuse, and only for that binary being absent"
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
