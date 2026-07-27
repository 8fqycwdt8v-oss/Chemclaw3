"""The `calc` connector's FastAPI app — the fast calculators behind their own server.

Run it with `uvicorn connectors.calc.server.app:app --port 8815`, or through `make connectors`.
"""

from fastapi import FastAPI

from connectors.calc.server.tools import server
from connectors.server import connector_app

app: FastAPI = connector_app(server, name="calc")
