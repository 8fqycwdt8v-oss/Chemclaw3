# `mcp/` — MCP capability servers

**Responsibility:** deterministic capability ("do X"), each as a small,
self-contained MCP server in its own process. Examples planned: `mcp-molfp`
(SMILES → ECFP4 via RDKit) and `mcp-rxnfp` (reaction DRFP), each ~100 LOC.

Capability vs. judgment: an MCP server *computes a fingerprint*; the decision of
*which Tanimoto threshold counts as precedent* is a Skill (`skills/`). Keep them
separate (gate G6). Servers are also where non-Python or auth-isolated
capabilities live (see ADR-0001).

Empty until Phase 3 (plan steps 3.1, 3.4).
