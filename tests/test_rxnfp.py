"""Behavioral tests for the mcp-rxnfp reaction capability (plan step 3.4).

Proves DRFP is deterministic, invalid reactions fail clearly, and Tanimoto ranking over
reactions returns most-similar-first — without a database. The reaction path reuses the
generic fingerprint store, so ranking correctness is already covered by test_molfp; here
we prove the DRFP-specific fingerprinting and that it plugs into the shared store.
"""

import asyncio

import pytest

from chemclaw.config import settings
from mcp_servers.fpstore import FingerprintError, InMemoryFingerprintStore
from mcp_servers.rxnfp.fingerprint import drfp_bitstring
from mcp_servers.rxnfp.search import find_similar_reactions, record_for_reaction

# Three esterifications (similar) and one unrelated halogenation.
_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_ESTER_PROPYL = "CCCO.CC(=O)O>>CCCOC(C)=O"
_ESTER_BUTYL = "CCCCO.CC(=O)O>>CCCCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


def test_drfp_is_deterministic_and_config_sized() -> None:
    """The same reaction yields the same fingerprint, sized to the configured width."""
    a = drfp_bitstring(_ESTER_ETHYL)
    assert a == drfp_bitstring(_ESTER_ETHYL)
    assert len(a) == settings.drfp_bits
    assert "1" in a  # a real reaction sets at least one bit


def test_invalid_reaction_raises() -> None:
    """A non-reaction (no `>>`) is a clear FingerprintError, not a DRFP-internal crash."""
    with pytest.raises(FingerprintError, match="unparseable reaction"):
        drfp_bitstring("CCO")


def test_empty_fingerprint_raises() -> None:
    """A degenerate reaction with no features is rejected, not stored as all-zeros."""
    with pytest.raises(FingerprintError, match="empty fingerprint"):
        drfp_bitstring(">>>")


def test_find_similar_reactions_ranks_by_tanimoto() -> None:
    """A reaction query returns the most similar reactions first, filtering the unrelated."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for rid, rxn in [
            ("ethyl", _ESTER_ETHYL),
            ("propyl", _ESTER_PROPYL),
            ("butyl", _ESTER_BUTYL),
            ("halogenation", _HALOGENATION),
        ]:
            await store.add(record_for_reaction(rid, rxn))

        hits = await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.1)
        assert hits[0].id == "ethyl"  # exact match ranks first
        assert hits[0].similarity == pytest.approx(1.0)
        assert "halogenation" not in {h.id for h in hits}  # unrelated reaction excluded
        assert all(hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1))
        assert hits[0].label == _ESTER_ETHYL  # the label carries the reaction SMILES

    asyncio.run(_run())
