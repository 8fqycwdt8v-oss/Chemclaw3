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
