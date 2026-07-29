# `chemclaw.mcp` — standalone MCP capability servers

**Responsibility:** deterministic capability ("do X") as plain, testable modules, wrapped by a thin
FastMCP server. Two live here: `molfp` (SMILES → ECFP4 + similarity/substructure search) and `rxnfp`
(reaction SMILES → DRFP + reaction similarity). They share the generic Tanimoto store
`mcp_servers/fpstore.py` (a Rule-of-Three extraction).

**This is not a second way to add a tool.** Every capability the agent reaches is a **connector
bundle** (`connectors/`, D-109) — that is the one registration mechanism, and `make
connector-validate` is what enforces it. These two modules are simply where the fingerprint
capability's *code* has always lived; `connectors/molfp/server/app.py` and its `rxnfp` twin import
the `server` object from here and serve it. Moving the bodies into the bundles would be churn with
no behavioural change, so it has not been done — but a **new** capability's code goes in its bundle,
not here.

`mcp_servers/calc/` used to be a third server. It duplicated the `calc` bundle's tool surface — two
live definitions of `predict_pka`, differing in one of them — which is the failure this directory
must not reproduce. D-113 decided to delete it; **it was actually deleted in D-117**, and the gap
between those two facts is the point: this paragraph asserted the deletion across four ADRs while
the file was still tracked, still built into the image by
`deploy/Containerfile`, and still dispatchable as `CHEMCLAW_COMPONENT=mcp-calc`. A README is not a
gate. `tests/test_deploy_chart.py` now asserts both directions of the chart↔entrypoint
correspondence, which is what would have caught it.

**Why `mcp_servers/` and not `mcp/`:** the directory cannot be named `mcp` — that package name is
taken by the installed MCP SDK (`from mcp.server.fastmcp import FastMCP`), and a local `mcp/`
package shadows it and breaks the server import (D-016).

Capability vs. judgment: an MCP server *computes a fingerprint*; the decision of *which Tanimoto
threshold counts as precedent* is a Skill (`skills/`, or the bundle's own). Keep them separate
(gate G6).
