"""Front-door run service (plan Phase F2): the ASGI app that actually runs the Chemclaw agent.

The agent was previously only ever *built* (in tests); this package is the missing caller its
docstring describes. `create_app` (in `chemclaw.api.app`) exposes a browser chat surface and a turn
API;
`chemclaw.api.runner` owns the per-turn lifecycle — build/resolve the agent, open the MCP tool
contexts,
run the (optionally harness-driven) turn, and stream typed events (`chemclaw.api.events`) back.
Identity
(Entra OIDC) is layered on in F4; a durable session store and job→session push-back arrive in F3.
"""
