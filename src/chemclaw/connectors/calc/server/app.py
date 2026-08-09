"""The `calc` connector's FastAPI app — the fast calculators behind their own server.

Run it with `uvicorn chemclaw.connectors.calc.server.app:app --port 8815`, or through `make
connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.calc.server.tools import resolve_calculator_versions, server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="calc", on_start=resolve_calculator_versions)
