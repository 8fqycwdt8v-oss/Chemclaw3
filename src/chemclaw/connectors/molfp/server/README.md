# `connectors/molfp/server` — the `molfp` bundle's MCP tool surface

`app.py` builds the `FastMCP` application; `tools.py` exposes the search over molecular
fingerprints. The ranking, the fingerprinting and the backends are all in
`science/fingerprints/molfp` and `science/fingerprints/store` — this package binds them to tool
names and validates what a caller sent.
