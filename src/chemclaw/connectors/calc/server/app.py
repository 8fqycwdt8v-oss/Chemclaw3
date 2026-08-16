"""The `calc` connector's FastAPI app — the cache, the ledger and the composites behind one server.

Run it with `uvicorn chemclaw.connectors.calc.server.app:app --port 8815`, or through `make
connectors`.

**No `on_start` hook, and its absence is the point.** There used to be one, and it existed for a
single trap: `pka_calc_version()` shelled out to `xtb --version` on the first call in a process, and
three of its call sites did not thread that subprocess off the event loop — so an ordinary first
`calculator_trust("pka")` in a fresh pod could hold this connector's one loop, every session's
stream included, for the 30 s subprocess timeout. There is no binary to ask any more
(`D-2026-08-16-the-physics-leaves-the-cache-stays`): the version comes from the calculation server
over an ordinary awaited round trip, so the blocking call the hook was hoisting no longer exists.
Keeping the hook to warm something would be a diagnostic pretending to be a guard.
"""

from fastapi import FastAPI

from chemclaw.connectors.calc.server.tools import server
from chemclaw.connectors.server import connector_app

app: FastAPI = connector_app(server, name="calc")
