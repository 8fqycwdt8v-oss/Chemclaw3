"""Behavioral tests for the shared identity helpers (`chemclaw.core.chem`, `chemclaw.core.ids`).

Canonicalization collapses equivalent SMILES to one key, and the hash is stable and
order-independent: the two properties every content-addressed key relies on.
"""

import pytest
from chemclaw_contracts.calc import CALC_REQUESTS

from chemclaw.connectors.calc.remote import cached_remote
from chemclaw.core.chem import (
    InvalidSmilesError,
    canonical_smiles,
    require_canonical_smiles,
    require_molecule,
    require_standard_smiles,
)
from chemclaw.core.ids import stable_hash
from chemclaw.core.mcp_session import WireRequest
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
        # RDKit skips a non-ASCII run at either edge of the string and fails on one in the middle,
        # so these parse as methane, ethane and ethane unless refused. They come from prose: a unit
        # symbol, a dash or a quotation mark copied beside a structure.
        "°C",
        "CC°",
        "°CC°",
    ],
)
def test_the_three_strict_helpers_share_one_definition_of_parses(bad: str) -> None:
    """The three strict helpers share one definition of "parses".

    `require_molecule` is the gate; the SMILES helpers must not keep their own weaker copy.
    """
    for helper in (require_molecule, require_canonical_smiles, require_standard_smiles):
        with pytest.raises(InvalidSmilesError):
            helper(bad)


def test_require_molecule_returns_the_molecule_the_canonical_form_is_taken_from() -> None:
    """`require_molecule` returns the molecule the canonical form is taken from.

    A SMARTS matcher echoes back the structure it looked at, so both must come from one parse.
    """
    from rdkit import Chem

    assert str(Chem.MolToSmiles(require_molecule(" OCC\n"))) == require_canonical_smiles("CCO")


def test_calc_cache_key_collapses_equivalent_smiles() -> None:
    """The calculator cache key is canonical: `CCO` and `OCC` share one key."""
    k1 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("CCO")})
    k2 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("OCC")})
    assert k1.as_str() == k2.as_str()


def _request(tool: str, smiles: str) -> WireRequest:
    """The wire request the fleet's own model defines for `tool` over `smiles`."""
    return CALC_REQUESTS[tool].model_validate({"smiles": smiles})


@pytest.mark.parametrize(
    ("tool", "pair"),
    [
        ("compute_xtb_energy", ("CCO", "OCC")),
        # pKa needs an acidic S-H/O-H site; ethanol is inert to the predictor, so use a thiol.
        ("predict_pka", ("CCS", "SCC")),
        ("predict_solubility", ("CCO", "OCC")),
    ],
)
async def test_a_calculator_serves_the_other_spelling_of_a_molecule_from_the_store(
    tool: str, pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every calculator computes once for a molecule, then serves the other spelling from the store.

    `CCO` misses and computes; `OCC` is a hit. Canonicalization happens on the calculation server,
    so this pins that the property survives the wire: a client canonicalizing differently would mint
    a second key that always misses.
    """
    server = install(monkeypatch, FakeCalcServer())

    store = InMemoryStore()
    first, second = pair
    _, cached_first = await cached_remote(store, _request(tool, first))
    _, cached_second = await cached_remote(store, _request(tool, second))
    assert cached_first is False
    assert cached_second is True

    assert server.count(tool) == 1


def test_a_value_whose_str_is_not_stable_is_refused_rather_than_hashed() -> None:
    """A value whose `str` is not stable is refused rather than hashed.

    A `set` iterates in a per-process random order, and an object without `__str__`/`__repr__`
    renders its memory address. This hash keys the calculation cache, campaign decision spaces,
    report and workflow ids, so an unstable digest would silently mint new identities. `str()` is
    kept for other types (`datetime`, `Decimal`, `Enum`, `UUID`, `Path`), where it is deterministic.
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


#: SMILES covering features RDKit releases have re-ranked: fused and bridged rings, hetero-aromatic
#: perception, stereo, charges, isotopes, a radical, a salt and a mapped atom. A corpus, because a
#: ranking change moves only some molecules.
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
    """The molecule hash this repository derives is the one the fleet writes.

    `find_calculations(smiles=…)` hashes the query the way `Chemclaw3-mcp` built the key, with a
    different RDKit pin; RDKit's canonical ranking is not stable across releases, and a divergence
    answers "nothing found" about rows that exist. A matching pin floor would not equalise versions
    or reach stored rows, so the two checkouts are compared against the fleet's own `cache_key`.
    Skips loudly without the sibling checkout (`tests/conftest.py::_report_sibling_skips`).
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
