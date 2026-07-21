"""Behavioral tests for the shared identity helpers (`chemclaw.chem`, `chemclaw.ids`).

Proves the two properties every content-addressed key in the system relies on:
canonicalization collapses equivalent SMILES to one key, and the hash is stable
and order-independent. These back the D-011 "compute once, never twice" guarantee.
"""

import asyncio

import pytest

from calc.pka import PkaInput, run_cached_pka
from calc.solubility import SolubilityInput, run_cached_solubility
from calc.store import CalculationKey, InMemoryStore
from calc.xtb import XtbInput, run_cached_xtb
from chemclaw.chem import (
    InvalidSmilesError,
    canonical_smiles,
    require_canonical_smiles,
)
from chemclaw.ids import stable_hash
from workflows.models import QMJobInput, qm_job_key


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


def test_qm_job_key_ignores_smiles_spelling() -> None:
    """Same molecule, different SMILES spelling → one QM workflow id (D-011)."""
    a = QMJobInput(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP")
    b = QMJobInput(molecule_smiles="OCC", method="B3LYP", basis_set="def2-SVP")
    assert qm_job_key(a) == qm_job_key(b)


def test_qm_job_key_rejects_invalid_smiles() -> None:
    """An unparseable molecule is rejected at key construction (durable boundary)."""
    with pytest.raises(InvalidSmilesError):
        qm_job_key(QMJobInput(molecule_smiles="???", method="B3LYP", basis_set="def2-SVP"))


def test_calc_cache_key_collapses_equivalent_smiles() -> None:
    """The calculator cache key is canonical: `CCO` and `OCC` share one key."""
    k1 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("CCO")})
    k2 = CalculationKey.build("xtb", "v1", inputs={"smiles": require_canonical_smiles("OCC")})
    assert k1.as_str() == k2.as_str()


@pytest.mark.parametrize(
    ("run_cached", "make_input"),
    [
        (run_cached_xtb, lambda s: XtbInput(smiles=s)),
        (run_cached_pka, lambda s: PkaInput(smiles=s)),
        (run_cached_solubility, lambda s: SolubilityInput(smiles=s)),
    ],
)
def test_run_cached_serves_equivalent_smiles_from_store(run_cached, make_input) -> None:  # type: ignore[no-untyped-def]
    """Every calculator computes once for a molecule, then serves the other spelling.

    `CCO` misses and computes; `OCC` (the same molecule) is a store hit, proving the
    canonical cache key defeats duplicate compute across SMILES spellings. Ethanethiol
    (`CCS`/`SCC`) is the pKa case — ethanol has no acidic O-H site the predictor treats.
    """

    async def _run() -> None:
        store = InMemoryStore()
        # pKa needs an acidic S-H/O-H site; ethanol is inert to it, so use a thiol.
        pair = ("CCS", "SCC") if run_cached is run_cached_pka else ("CCO", "OCC")
        first_smiles, second_smiles = pair
        _, cached_first = await run_cached(store, make_input(first_smiles))
        _, cached_second = await run_cached(store, make_input(second_smiles))
        assert cached_first is False
        assert cached_second is True

    asyncio.run(_run())
