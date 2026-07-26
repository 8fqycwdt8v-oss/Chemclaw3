"""The `molfp` connector's FastAPI app — the molecule capability behind its own server.

The capability itself is unchanged and stays where it was tested: `mcp_servers/molfp/` holds the
fingerprinting and search logic and the `FastMCP` instance advertising it. This module only gives it
the transport the connector seam expects (`/healthz` + `/mcp` over streamable HTTP), which is what
takes `rdkit` and the fingerprint tables out of the chat service's process.

Run it with `uvicorn connectors.molfp.server.app:app --port 8811`, or (with every other local
connector, on one port) through `make connectors`.
"""

from fastapi import FastAPI

from connectors.server import connector_app
from mcp_servers.molfp.server import server

app: FastAPI = connector_app(server, name="molfp")
