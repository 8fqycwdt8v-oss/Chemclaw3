"""The mcp-molfp server advertises the fingerprint capability as MCP tools (3.1).

Wiring (tool registration + schemas, and the startup report the bundle hands its lifespan), plus
the two behaviours a chemist's answer actually rests on — that a similarity search ranks and that a
substructure search filters — exercised over a substituted in-memory store, never the production
one. Those two came from `test_search_tools.py` when D-2026-08-05 deleted the in-process wrapper
they used to cover: the assertions were sound and the subject was not, since the wrapper was not
the surface any turn calls and had drifted from the one that is. The capability logic itself is
proven in `test_molfp.py`; that the empty-index signal survives the MCP round trip is proven, for
the tool that produced the live-run defect, in `test_rxnfp_server.py`.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.molfp.server import tools
from chemclaw.connectors.molfp.server.tools import server
from chemclaw.science.fingerprints.molfp.search import record_for
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore


def test_server_advertises_the_capability_tools() -> None:
    """The three fingerprint tools are registered with input schemas."""
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_molecules", "substructure_matches", "index_molecule"} <= set(tools)
    # The similarity tool takes a smiles argument (the capability's entry point).
    assert "smiles" in tools["similar_molecules"].inputSchema["properties"]


def _seeded_store() -> InMemoryFingerprintStore:
    """Four molecules: ethanol, its propanol analog, a boronic acid, and plain benzene."""
    store = InMemoryFingerprintStore()
    for smiles in ("CCO", "CCCO", "OB(O)c1ccccc1", "c1ccccc1"):
        asyncio.run(store.add(record_for(smiles, smiles)))
    return store


def _call(name: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Invoke one molfp tool over the seeded store and return the payload MCP sends back."""
    monkeypatch.setattr(tools, "_store", _seeded_store())
    _content, structured = asyncio.run(server.call_tool(name, args))
    assert isinstance(structured, dict)
    return structured


def test_similar_molecules_ranks_the_identical_structure_above_its_analog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact match leads at Tanimoto 1.0 and the close analog is still retrieved.

    Ranking is the whole content of a similarity answer: a hit list in any other order tells the
    chemist the wrong molecule is the precedent.
    """
    payload = _call("similar_molecules", {"smiles": "CCO"}, monkeypatch)

    assert payload["hits"][0]["smiles"] == "CCO"
    assert payload["hits"][0]["similarity"] == 1.0
    assert "CCCO" in {hit["smiles"] for hit in payload["hits"]}  # the propanol analog


def test_substructure_matches_returns_only_molecules_bearing_the_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SMARTS query is an exact match, not a ranking — so it filters and carries no score."""
    payload = _call("substructure_matches", {"query": "OB(O)"}, monkeypatch)

    assert {hit["smiles"] for hit in payload["hits"]} == {"OB(O)c1ccccc1"}
    assert all(hit["similarity"] is None for hit in payload["hits"])


def test_the_bundle_reports_its_index_size_for_the_startup_hook(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bundle's lifespan wires this in, so an unbuilt index is loud before a chemist asks.

    See `src/chemclaw/connectors/molfp/server/app.py` for the wiring.

    Called directly rather than through the app's lifespan on purpose: `FastMCP.session_manager`
    is single-use per process, and the bundle exports one module-level `app` that
    `test_connector_transport.py` already serves — entering its lifespan here would break that.
    """
    monkeypatch.setattr(tools, "_store", InMemoryFingerprintStore())
    with caplog.at_level("WARNING"):
        asyncio.run(tools.report_index_size())
    assert any("molecule fingerprint index is EMPTY" in r.getMessage() for r in caplog.records)
