"""The `bo` connector's FastAPI app, serving the inline `suggest_next_experiment` tool.

The durable half is served by `chemclaw.connectors.bo.worker` on its own Temporal queue.
Run with `uvicorn chemclaw.connectors.bo.server.app:app --port 8816`, or `make connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.bo.server.tools import server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="bo")
