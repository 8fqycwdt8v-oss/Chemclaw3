"""The `rxnfp` connector's FastAPI app — the reaction capability behind its own server.

The capability is unchanged and stays where it was tested (`mcp_servers/rxnfp/`); this module only
gives it the transport the connector seam expects (`/healthz` + `/mcp`), so DRFP and the reaction
fingerprint table leave the chat service's process.

Run it with `uvicorn connectors.rxnfp.server.app:app --port 8812`, or through `make connectors`.
"""

from fastapi import FastAPI

from connectors.server import connector_app
from mcp_servers.rxnfp.server import server

app: FastAPI = connector_app(server, name="rxnfp")
