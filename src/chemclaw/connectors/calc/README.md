# `connectors/calc` — the semiempirical calculation bundle

Everything a chemist asks of GFN2-xTB and CREST, as durable jobs. There is **no DFT and no cluster**
(`D-2026-08-26-semiempirical-is-the-whole-tier`): the physics itself answers from `Chemclaw3-mcp`'s
`servers/calc` pod, addressed by `CHEMCLAW_CALC_SERVER_URL`.

| file | what it is |
|---|---|
| `connector.yaml` | every job this bundle declares, each going down one durable path |
| `remote.py` | the client that reaches the calculation server — the only place this repo dials it |
| `compose.py` | composition over the server's primitives, so every nested step is separately cached |
| `activities.py` / `workflows.py` | `CalcJobWorkflow` and the activities it runs |
| `specs.py`, `results.py` | the typed job inputs and the projection of what comes back |
| `server/` | the inline MCP tools beside the durable jobs |

The cache, the calibration ledger and the statistical mechanics stayed one layer down in
`science/calc`, because a cache and an engine want to live on opposite sides of a wire. When a
decision turns on a difference inside GFN2-xTB's error bar, say so and propose an experiment —
there is no tier to escalate to.
