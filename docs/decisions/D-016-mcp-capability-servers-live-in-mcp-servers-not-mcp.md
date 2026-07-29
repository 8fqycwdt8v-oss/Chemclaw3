# D-016 — MCP capability servers live in `mcp_servers/`, not `mcp/`

The plan named the capability-server directory `mcp/`, but that package name is taken by the
installed MCP SDK (`from mcp.server.fastmcp import FastMCP`). A local top-level `mcp/` package
shadows the SDK on `sys.path`, so `mcp.server` becomes unreachable and no FastMCP server can be
built. The directory is therefore `mcp_servers/`. This is a naming-only deviation from the plan;
the responsibility (deterministic capability, one small server per concern) is unchanged.
