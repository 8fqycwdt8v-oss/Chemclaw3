# `mcp_servers/` — MCP capability servers

**Responsibility:** deterministic capability ("do X"), each as a small,
self-contained MCP server in its own process. Implemented: `molfp` (`mcp-molfp`,
SMILES → ECFP4 + similarity/substructure search) and `rxnfp` (`mcp-rxnfp`,
reaction SMILES → DRFP + reaction similarity). Both share the generic Tanimoto
store `mcp_servers/fpstore.py` (Rule-of-Three extraction), so each server file
stays a thin FastMCP wrapper over a plain, testable capability module.

**Why `mcp_servers/` and not `mcp/`:** the directory cannot be named `mcp` — that
package name is taken by the installed MCP SDK (`from mcp.server.fastmcp import
FastMCP`), and a local `mcp/` package shadows it and breaks the server import
(D-016).

Capability vs. judgment: an MCP server *computes a fingerprint*; the decision of
*which Tanimoto threshold counts as precedent* is a Skill (`skills/`). Keep them
separate (gate G6). Servers are also where non-Python or auth-isolated
capabilities live (see ADR-0001).
