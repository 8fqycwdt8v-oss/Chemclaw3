"""The mcp-molfp server advertises the fingerprint capability as MCP tools (3.1).

Verifies wiring only (tool registration + schemas), not execution — invoking a tool
would hit the production store. The capability logic is proven in `test_molfp.py`.
"""

import asyncio

from mcp_servers.molfp.server import server


def test_server_advertises_the_capability_tools() -> None:
    """The three fingerprint tools are registered with input schemas."""
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_molecules", "substructure_matches", "index_molecule"} <= set(tools)
    # The similarity tool takes a smiles argument (the capability's entry point).
    assert "smiles" in tools["similar_molecules"].inputSchema["properties"]
