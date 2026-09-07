# `connectors/bo/server` — the `bo` bundle's MCP tool surface

`app.py` builds the `FastMCP` application the pod serves; `tools.py` holds the tools themselves —
framing a vague "optimize this reaction" into a concrete decision space, suggesting the next
experiment, predicting an outcome at conditions the chemist names, and reading a campaign's
progress back.

These are the **inline** half of this bundle. A question answered inside one turn lives here; a
campaign that outlives a turn is `connectors/bo/workflows.py`. Every tool declared in `connectors/bo/connector.yaml`
resolves to a function in this package, and `connector-validate` fails if one does not.
