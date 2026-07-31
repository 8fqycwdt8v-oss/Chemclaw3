"""The `molfp` connector's FastAPI app — the molecule capability behind its own server.

The three-way split every bundle uses: `chemclaw.science.fingerprints.molfp` computes (no MCP, no
FastAPI), `tools.py` beside this file advertises those functions as MCP tools, and this module gives
them the transport the connector seam expects (`/healthz` + `/mcp` over streamable HTTP) — which is
what takes `rdkit` and the fingerprint tables out of the chat service's process.

Run it with `uvicorn chemclaw.connectors.molfp.server.app:app --port 8811`, or (with every other
local connector, on one port) through `make connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.molfp.server.tools import server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="molfp")
