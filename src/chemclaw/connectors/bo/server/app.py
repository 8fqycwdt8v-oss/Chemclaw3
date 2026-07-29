"""The `bo` connector's FastAPI app — the one-shot experiment-design tool behind its own server.

The bundle's *durable* half is not here: `chemclaw.durable.py` is served by
`chemclaw.connectors.bo.worker` on its
own Temporal queue. This app serves only the inline `suggest_next_experiment`.

Run it with `uvicorn chemclaw.connectors.bo.server.app:app --port 8816`, or through `make
connectors`.
"""

from fastapi import FastAPI

from chemclaw.connectors.bo.server.tools import server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="bo")
