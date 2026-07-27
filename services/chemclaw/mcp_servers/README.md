# `mcp_servers/` — MCP capability servers

**Responsibility:** deterministic capability ("do X"), each as a small,
self-contained MCP server in its own process. Implemented: `molfp` (`mcp-molfp`,
SMILES → ECFP4 + similarity/substructure search), `rxnfp` (`mcp-rxnfp`,
reaction SMILES → DRFP + reaction similarity), and `calc` (`mcp-calc`, the fast
calculators — xTB energies and properties, geometry optimization,
thermochemistry, pKa, solubility). The fingerprint pair share the generic
Tanimoto store `mcp_servers/fpstore.py` (Rule-of-Three extraction); every server
file stays a thin FastMCP wrapper over a plain, testable capability module.

**Where the boundary falls, and why it is about identity rather than chemistry
(X8).** `mcp-calc` hosts the tools that *compute*. The ones that submit durable
jobs — `compute_reaction_energy`, `compare_solvents`, `scan_coordinate`,
`sample_conformers`, `run_xtb_task` — stay in-process, because submitting a job
needs `require_actor()` and `get_current_session_id()`: the turn's authenticated
user and the conversation to notify, both **ambient** and never model-supplied
(F4-T3). An MCP server has neither and could only take them as arguments, which
would make identity a model-authored value. So: **MCP carries capability, the
agent keeps identity** — and that also says what can ever move here.

Each server runs as its own pod via `CHEMCLAW_COMPONENT=mcp-<name>` (see
`deploy/entrypoint.sh`), which is the point for `mcp-calc`: RDKit, tblite, scipy
and the `xtb`/`crest` binaries, plus the CPU they need, scale independently of
the agent. Config admits an `http` transport variant, so the same server can be
reached as an already-running remote deployment instead of a subprocess.

**Why `mcp_servers/` and not `mcp/`:** the directory cannot be named `mcp` — that
package name is taken by the installed MCP SDK (`from mcp.server.fastmcp import
FastMCP`), and a local `mcp/` package shadows it and breaks the server import
(D-016).

Capability vs. judgment: an MCP server *computes a fingerprint*; the decision of
*which Tanimoto threshold counts as precedent* is a Skill (`skills/`). Keep them
separate (gate G6). Servers are also where non-Python or auth-isolated
capabilities live (see ADR-0001).
