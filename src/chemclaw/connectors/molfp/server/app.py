"""The `molfp` connector's FastAPI app: the molecule capability behind its own server.

`chemclaw.science.fingerprints.molfp` computes, `tools.py` advertises it over MCP, and this module
adds the transport (`/healthz` + `/mcp`), keeping `rdkit` and the fingerprint tables out of the
chat service's process.

Run it with `uvicorn chemclaw.connectors.molfp.server.app:app --port 8811`, or through `make
connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.molfp.server.tools import report_index_size, server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="molfp", on_start=report_index_size)
