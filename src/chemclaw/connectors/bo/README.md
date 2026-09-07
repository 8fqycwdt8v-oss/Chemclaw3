# `connectors/bo` — the Bayesian-optimization bundle

The reference **connector-owned durable capability**, and the only bundle that carries all four
halves at once: a manifest, an MCP tool server, Temporal work of its own, and a worker to host it.

| file | what it is |
|---|---|
| `connector.yaml` | the declaration four validators resolve tool names, params models and preconditions through |
| `server/` | the MCP tool surface — the inline questions a chemist asks mid-conversation |
| `workflows.py` | `BoCampaignWorkflow`: the durable, resumable ask/tell loop, and the only loop that ships |
| `activities.py` | the two BoFire calls a round is made of, each threaded off the event loop and heartbeating |
| `calculators.py` | the binding that hands `science/bo` its calculator seam, which `science/` may not import |
| `knowledge.py` | a finished campaign, written up as a `bo-candidate` note a chemist can act on |
| `worker.py` | the process that hosts the workflow and the activities on this bundle's own queue |

The engine underneath is `science/bo` — BoFire, the problem types, the campaign record — and the
split is the one `ARCHITECTURE.md` calls a pair rather than a duplication: pure computation there,
its durable and MCP wrappers here. Core reaches the workflow by type name across
`connector-bo`, so adding a capability here is adding a directory and a manifest, never a core edit.
