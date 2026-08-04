"""The `rxnfp` connector's FastAPI app — the reaction capability behind its own server.

The three-way split every bundle uses: `chemclaw.science.fingerprints.rxnfp` computes, `tools.py`
beside this file advertises those functions as MCP tools, and this module gives them the transport
the connector seam expects (`/healthz` + `/mcp`), so DRFP and the reaction fingerprint table leave
the chat service's process.

Run it with `uvicorn chemclaw.connectors.rxnfp.server.app:app --port 8812`, or through `make
connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.rxnfp.server.tools import report_index_size, server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="rxnfp", on_start=report_index_size)
