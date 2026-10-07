"""The `calc` connector's FastAPI app: the cache, the ledger and the composites behind one server.

Run it with `uvicorn chemclaw.connectors.calc.server.app:app --port 8815`, or through `make
connectors`. It needs no `on_start` hook: versions come from the calculation server over an
awaited round trip, so there is no blocking call to hoist off the event loop.
"""

from fastapi import FastAPI

from chemclaw.connectors.calc.server.tools import server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="calc")
