"""The `bo` connector's FastAPI app — the one-shot experiment-design tool behind its own server.

The bundle's *durable* half is not here: `workflows.py` is served by `connectors.bo.worker` on its
own Temporal queue. This app serves only the inline `suggest_next_experiment`.

Run it with `uvicorn connectors.bo.server.app:app --port 8816`, or through `make connectors`.
"""

from fastapi import FastAPI

from connectors.bo.server.tools import server
from connectors.server import connector_app

app: FastAPI = connector_app(server, name="bo")
