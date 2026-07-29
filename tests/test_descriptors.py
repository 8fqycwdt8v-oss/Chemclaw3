"""Behavioral tests for the developability descriptor panel (D-092).

Pure RDKit, deterministic; proves the panel matches known chemistry (aspirin passes Ro5/Veber,
a very large lipophilic molecule does not) and that the store integration computes once.
"""

import asyncio
from importlib.metadata import version

import pytest

from calc.descriptors import (
    DescriptorInput,
    _calc_version,
    compute_descriptor_profile,
    run_cached_descriptor_profile,
)
from calc.store import InMemoryStore

_ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"


def test_calc_version_embeds_rdkit_build() -> None:
    """The cache key carries the RDKit build (D-011): an upgrade recomputes, not a stale hit."""
    assert version("rdkit") in _calc_version()


def test_aspirin_passes_ro5_and_veber() -> None:
    """A small, well-behaved drug-like molecule has zero Ro5 violations and passes Veber."""
    profile = compute_descriptor_profile(DescriptorInput(smiles=_ASPIRIN))
    assert profile.lipinski_violations == 0
    assert profile.veber_pass is True
    assert 175 < profile.molecular_weight < 185
    assert profile.h_bond_donors == 1
    assert profile.h_bond_acceptors == 3


def test_large_lipophilic_molecule_violates_ro5() -> None:
    """A long-chain, high-MW, high-LogP molecule breaks multiple Rule-of-Five criteria."""
    long_chain = "C" * 40  # a very large, very lipophilic alkane
    profile = compute_descriptor_profile(DescriptorInput(smiles=long_chain))
    assert profile.lipinski_violations >= 2
    assert profile.molecular_weight > 500
    assert profile.clogp > 5


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast (gate G4) rather than returning a bogus panel."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        compute_descriptor_profile(DescriptorInput(smiles="%%%not-a-mol%%%"))


def test_cached_descriptor_profile_computes_once() -> None:
    """A repeat request for the same molecule is served from the store."""

    async def _run() -> None:
        store = InMemoryStore()
        first, first_cached = await run_cached_descriptor_profile(
            store, DescriptorInput(smiles=_ASPIRIN)
        )
        assert first_cached is False
        second, second_cached = await run_cached_descriptor_profile(
            store, DescriptorInput(smiles=_ASPIRIN)
        )
        assert second_cached is True
        assert second == first

    asyncio.run(_run())


def test_descriptor_profile_is_independent_of_smiles_spelling() -> None:
    """Equivalent spellings of one molecule share a cache entry (D-011 determinism)."""

    async def _run() -> None:
        store = InMemoryStore()
        canonical, _ = await run_cached_descriptor_profile(store, DescriptorInput(smiles="CCO"))
        respelled, was_cached = await run_cached_descriptor_profile(
            store, DescriptorInput(smiles="OCC")
        )
        assert was_cached is True
        assert respelled.molecular_weight == canonical.molecular_weight

    asyncio.run(_run())
