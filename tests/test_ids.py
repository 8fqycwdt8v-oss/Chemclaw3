"""Behavioral tests for the shared identity helpers (`chemclaw.core.chem`, `chemclaw.core.ids`).

Proves the two properties every content-addressed key in the system relies on:
canonicalization collapses equivalent SMILES to one key, and the hash is stable
and order-independent. These back the D-011 "compute once, never twice" guarantee.
"""

import asyncio

import pytest

from chemclaw.connectors.calc.remote import cached_remote
from chemclaw.core.chem import (
    InvalidSmilesError,
    canonical_smiles,
    require_canonical_smiles,
    require_molecule,
    require_standard_smiles,
)
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.store import CalculationKey, InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install


def test_stable_hash_is_order_independent() -> None:
    """Dict key ordering does not change the digest (canonical JSON)."""
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_stable_hash_width_is_configurable() -> None:
    """`chars` controls the digest width; the shorter is a prefix of the longer."""
    long = stable_hash({"x": 1}, chars=16)
    short = stable_hash({"x": 1}, chars=12)
    assert len(long) == 16
    assert len(short) == 12
    assert long.startswith(short)


def test_canonical_smiles_collapses_equivalent_spellings() -> None:
    """Two spellings of ethanol normalize to one canonical string."""
    assert canonical_smiles("CCO") == canonical_smiles("OCC")


def test_canonical_smiles_lenient_passes_through_unparseable() -> None:
    """The lenient form returns its input unchanged rather than raising."""
    assert canonical_smiles("not-a-molecule") == "not-a-molecule"


def test_require_canonical_smiles_rejects_unparseable() -> None:
    """The strict form raises `InvalidSmilesError` (a `ChemclawError`) on bad input."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("not-a-molecule")


def test_require_canonical_smiles_rejects_empty() -> None:
    """RDKit parses `""` to a zero-atom Mol; the strict gate rejects it instead of keying it."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("")
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("   ")


def test_require_canonical_smiles_rejects_embedded_whitespace() -> None:
    """RDKit would truncate at whitespace, silently keying a different molecule — rejected."""
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("CCO junk")
    with pytest.raises(InvalidSmilesError):
        require_canonical_smiles("C O")


def test_require_canonical_smiles_tolerates_surrounding_whitespace() -> None:
    """Leading/trailing whitespace is a copy-paste artifact, not a different molecule."""
    assert require_canonical_smiles(" CCO\n") == require_canonical_smiles("CCO")


@pytest.mark.parametrize(
    "bad",
    [
        "CCO junk",
        "CCO\tjunk",
        "",
        "   ",
        "not-a-molecule(((",
        # RDKit skips a non-ASCII run at either *edge* of the string and fails on one in the
        # middle, so these three are methane, ethane and ethane to a bare parse — the whitespace
        # truncation in another character, and a clean screen of a molecule nobody named if it
        # reaches one. Prose is where it comes from: a unit symbol, a dash or a quotation mark
        # copied in beside a structure.
        "°C",
        "CC°",
        "°CC°",
    ],
)
def test_the_three_strict_helpers_share_one_definition_of_parses(bad: str) -> None:
    """`require_molecule` is the gate; the two SMILES helpers must not have their own.

    They each spelled the same four lines out, which is how the hazard screens ended up with a
    *fifth*, weaker copy — a bare `Chem.MolFromSmiles` that accepted `"CCO junk"` as ethanol. One
    definition means adding a case to it reaches every caller, and this pins that they agree.
    """
    for helper in (require_molecule, require_canonical_smiles, require_standard_smiles):
        with pytest.raises(InvalidSmilesError):
            helper(bad)


def test_require_molecule_returns_the_molecule_the_canonical_form_is_taken_from() -> None:
    """The reason it exists: a caller needing the molecule gets the gate without a second parse.

    A SMARTS matcher works on the molecule and then echoes `Chem.MolToSmiles` of it back as the
    structure it looked at, so the two must be the same object's two faces rather than two parses
    of one string.
    """
    from rdkit import Chem

    assert str(Chem.MolToSmiles(require_molecule(" OCC\n"))) == require_canonical_smiles("CCO")


def test_calc_cache_key_collapses_equivalent_smiles() -> None:
    """The calculator cache key is canonical: `CCO` and `OCC` share one key."""
    k1 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("CCO")})
    k2 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("OCC")})
    assert k1.as_str() == k2.as_str()


@pytest.mark.parametrize(
    ("tool", "pair"),
    [
        ("compute_xtb_energy", ("CCO", "OCC")),
        # pKa needs an acidic S-H/O-H site; ethanol is inert to the predictor, so use a thiol.
        ("predict_pka", ("CCS", "SCC")),
        ("predict_solubility", ("CCO", "OCC")),
    ],
)
def test_a_calculator_serves_the_other_spelling_of_a_molecule_from_the_store(
    tool: str, pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every calculator computes once for a molecule, then serves the other spelling.

    `CCO` misses and computes; `OCC` (the same molecule) is a store hit, proving the canonical cache
    key defeats duplicate compute across SMILES spellings. Since
    `D-2026-08-16-the-physics-leaves-the-cache-stays` the canonicalization happens where the key is
    derived — on the calculation server — so what this pins now is that the property **survived the
    wire**: two spellings still reach one row, and a client that canonicalized differently on this
    side would produce a second key that misses forever with nothing raising.
    """
    server = install(monkeypatch, FakeCalcServer())

    async def _run() -> None:
        store = InMemoryStore()
        first, second = pair
        _, cached_first = await cached_remote(store, tool, {"smiles": first})
        _, cached_second = await cached_remote(store, tool, {"smiles": second})
        assert cached_first is False
        assert cached_second is True

    asyncio.run(_run())
    assert server.count(tool) == 1


def test_a_value_whose_str_is_not_stable_is_refused_rather_than_hashed() -> None:
    """`default=str` is documented as making non-JSON values "serialize deterministically".

    It does not, for the two classes whose `str()` is a property of the *run* rather than of the
    value, and both were measured rather than reasoned about:

    - a `set` iterates in an order Python randomises per process, so `stable_hash({'s': {...}})`
      over one six-element set gave five different digests under five `PYTHONHASHSEED` values;
    - an object whose type overrides neither `__str__` nor `__repr__` renders its **memory
      address**, so the same class in three processes gave `668cc845d7aa6ec5`, `f5fdffea01ba84c2`
      and `f4a76c115ad11869`.

    Neither is on a live path today — every caller reaches this with JSON-parsed data — which is
    exactly what makes it a trap rather than an outage: the sentence in the docstring invites the
    call, and this hash keys the calculation cache, a campaign's decision space, a report id and a
    workflow id. A campaign id is a hash of its decision space, and the failure `canonical_text`
    already records for a re-cased label is worse there than a duplicate run: a *new campaign with
    no history*, minted silently.

    `str()` is kept for everything else rather than replaced by an allow-list, because it is
    deterministic for every value type that actually reaches this — `datetime`, `Decimal`, `Enum`,
    `UUID`, `Path` — and an allow-list would refuse those to close a hole neither of them is in.
    """

    class Marker:
        """A type that overrides neither `__str__` nor `__repr__`, so `str()` is its address."""

    for value in ({"a", "b"}, frozenset({"a"}), Marker()):
        with pytest.raises(TypeError) as caught:
            stable_hash({"v": value})
        assert "stable" in str(caught.value).lower(), (
            f"the refusal does not say why {type(value).__name__} cannot be hashed: {caught.value}"
        )


def test_the_value_types_that_do_reach_this_still_hash() -> None:
    """The refusal above must not narrow what already works.

    `default=str` exists for these, and every one has a `__str__` that is a property of the value.
    """
    from datetime import UTC, datetime
    from decimal import Decimal
    from enum import Enum
    from pathlib import Path
    from uuid import UUID

    class Colour(Enum):
        RED = "red"

    payload = {
        "when": datetime(2026, 9, 9, tzinfo=UTC),
        "amount": Decimal("1.50"),
        "colour": Colour.RED,
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "path": Path("/tmp/x"),
    }
    assert stable_hash(payload) == stable_hash(dict(reversed(list(payload.items()))))


#: SMILES chosen for the features an RDKit release has historically re-ranked — fused and
#: bridged rings, aromatic perception on hetero rings, stereocentres and double-bond geometry,
#: charges, isotopes, a radical, a salt and an explicitly-mapped atom. A corpus rather than one
#: molecule, because a canonical-ranking change moves *some* molecules: ethanol agreeing proves
#: almost nothing on its own.
_CANONICALISATION_CORPUS = (
    "CCO",
    "OCC",
    "c1ccccc1O",
    "CC(=O)Oc1ccccc1C(=O)O",
    "N[C@@H](C)C(=O)O",
    "C/C=C/C(=O)OCC",
    "c1ccc2c(c1)cccn2",
    "C1CC2CCC1CC2",
    "CC(=O)[O-].[Na+]",
    "[13CH4]",
    "[CH3]",
    "c1cc[nH]c1",
    "O=S(=O)(O)c1ccc(N)cc1",
    "CN1CCC[C@H]1c1cccnc1",
)

_FLEET_MOLECULE_HASHES = """
import json, sys
from chemclaw_mcp_calc.engine.descriptors import DescriptorInput, cache_key as descriptors_key
from chemclaw_mcp_calc.engine.solubility import SolubilityInput, cache_key as solubility_key

print(json.dumps({
    smiles: [
        descriptors_key(DescriptorInput(smiles=smiles)).input_hash,
        solubility_key(SolubilityInput(smiles=smiles)).input_hash,
    ]
    for smiles in sys.argv[1:]
}))
"""


def test_the_molecule_hash_this_repo_derives_is_the_one_the_fleet_writes() -> None:
    """`molecule_hash` re-derives somebody else's `input_hash`, and nothing checked that it does.

    `find_calculations(smiles=…)` cannot scan — `input_hash` is not reversible — so the browse
    hashes the query molecule the way a key was built and compares. The rows were keyed in
    `Chemclaw3-mcp`, by `CalculationKey.build(inputs={"smiles": require_canonical_smiles(...)})`,
    in a different image with a different RDKit pin: `rdkit>=2026.3.4` here against
    `rdkit>=2024.3.1` there. The *shape* agrees; the **canonicalizer** is a `>=` on both sides,
    so nothing makes the
    two images run one version, and RDKit's canonical ranking is not contractually stable across
    releases. A divergence answers "nothing found" about rows that exist — the same silent failure
    `connectors/calc/remote.py` forbids for `calc_version` and `structure_id`, reintroduced one
    function away by the one value this repository does still derive locally.

    **A matching pin floor is not the fix and was rejected.** Two `>=` floors do not equalise two
    images' versions, and no floor reaches a row already on disk, written by an image that has
    since been upgraded. What is checkable is what the two *checkouts* do, so that is what is
    checked — against the fleet's own `cache_key` functions rather than against a re-derivation of
    them here, which would compare this file's idea of the fleet with this repository's.

    Skips **loudly** where there is no sibling checkout (`tests/conftest.py::_report_sibling_skips`
    counts it), because a check that quietly shrinks is worse than one that says what it did not
    look at.
    """
    import json
    import subprocess

    from chemclaw.science.calc.store import molecule_hash
    from tests.siblings import SIBLING_SKIP, sibling_python

    interpreter, reason = sibling_python("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if interpreter is None:
        pytest.skip(
            f"{SIBLING_SKIP} the fleet's molecule hashes were NOT compared: {reason}. Whether "
            "`molecule_hash` still derives the `input_hash` the calculation server writes is "
            "unchecked in this run."
        )
    completed = subprocess.run(
        [str(interpreter), "-c", _FLEET_MOLECULE_HASHES, *_CANONICALISATION_CORPUS],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(interpreter.parents[2]),
    )
    assert completed.returncode == 0, (
        f"the fleet's key derivation could not be run: {completed.stderr.strip()[-400:]}"
    )
    theirs = json.loads(completed.stdout)

    disagree = {
        smiles: (molecule_hash(smiles), hashes)
        for smiles, hashes in theirs.items()
        if {molecule_hash(smiles)} != set(hashes)
    }
    assert not disagree, (
        f"this repository and Chemclaw3-mcp derive different molecule `input_hash` values, as "
        f"(ours, theirs): {disagree}. `find_calculations(smiles=…)` will answer 'nothing found' "
        "about rows that exist for these molecules. The two canonicalizers have parted — compare "
        "the RDKit versions the two images install, not the pins they declare."
    )
