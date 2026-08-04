"""The mcp-molfp server advertises the fingerprint capability as MCP tools (3.1).

Verifies wiring only (tool registration + schemas, and the startup report the bundle hands its
lifespan), not execution against the production store. The capability logic is proven in
`test_molfp.py`; that the empty-index signal survives the MCP round trip is proven, for the tool
that produced the live-run defect, in `test_rxnfp_server.py`.
"""

import asyncio

import pytest

from chemclaw.connectors.molfp.server import tools
from chemclaw.connectors.molfp.server.tools import server
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore


def test_server_advertises_the_capability_tools() -> None:
    """The three fingerprint tools are registered with input schemas."""
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_molecules", "substructure_matches", "index_molecule"} <= set(tools)
    # The similarity tool takes a smiles argument (the capability's entry point).
    assert "smiles" in tools["similar_molecules"].inputSchema["properties"]


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
