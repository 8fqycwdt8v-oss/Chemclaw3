"""The mcp-rxnfp server advertises the reaction capability as MCP tools (3.4).

Verifies wiring only (tool registration + schemas), not execution — invoking a tool would
hit the production store. The capability logic is proven in `test_rxnfp.py`.
"""

import asyncio

from mcp_servers.rxnfp.server import server


def test_server_advertises_the_reaction_tools() -> None:
    """The two reaction tools are registered with input schemas."""
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert {"similar_reactions", "index_reaction"} <= set(tools)
    assert "reaction_smiles" in tools["similar_reactions"].inputSchema["properties"]
