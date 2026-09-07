# `connectors/calc/server` — the `calc` bundle's MCP tool surface

`app.py` builds the `FastMCP` application; `tools.py` holds the tools a chemist reaches inside one
turn — the questions that answer in seconds, beside the durable jobs in `connectors/calc/connector.yaml` that do
not.

Nothing here computes chemistry. The calls go out through `connectors/calc/remote.py` to `Chemclaw3-mcp`'s
`servers/calc`, and every result passes the D-011 cache on the way back, so a repeat is a lookup.
