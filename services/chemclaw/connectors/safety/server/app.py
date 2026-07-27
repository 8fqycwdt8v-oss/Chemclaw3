"""The `safety` connector's FastAPI app — the hazard screen behind its own server.

Run it with `uvicorn connectors.safety.server.app:app --port 8813`, or through `make connectors`.
"""

from fastapi import FastAPI

from connectors.safety.server.tools import server
from connectors.server import connector_app

app: FastAPI = connector_app(server, name="safety")
