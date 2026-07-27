"""The `chem` connector's FastAPI app — bench chemistry behind its own server.

Run it with `uvicorn connectors.chem.server.app:app --port 8814`, or through `make connectors`.
"""

from fastapi import FastAPI

from connectors.chem.server.tools import server
from connectors.server import connector_app

app: FastAPI = connector_app(server, name="chem")
