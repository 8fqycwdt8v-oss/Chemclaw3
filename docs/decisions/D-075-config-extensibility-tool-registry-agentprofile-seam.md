# D-075 — Config-extensibility: `@tool` registry + `AgentProfile` seam (audit doc 10, items 2–3)

**Context.** `docs/audit/10-config-extensibility.md` found the five extension seams at wildly
different maturity: tools were the weakest (a hardcoded `_capability_tools()` list — the one seam
forcing an orchestration-code edit), and per-use-case agent configuration was absent (one global
`build_agent`). The substrate verdict was to evolve additively with existing in-repo idioms, not
adopt any out-of-tree plugin framework (entry-points/pluggy/Django-apps).

**Decision.** Two seams landed, each mirroring an idiom already in the repo:
1. **Tool registry** (`agents/tool_registry.py`): a `@tool` decorator + name-keyed `_REGISTRY` with
   a duplicate-name guard — the exact shape of `evals.metric`. Tools register at their definition
   site; `_capability_tools()` assembles `[*registered_tools(), *_mcp_capability_tools()]`. The MCP
   capability path stays config-driven, and the shared `[audit, enforce_tool_authz]` middleware
   still wraps the assembled toolset — collection changed, gating did not.
2. **`AgentProfile` seam** (`agents/profiles.py`): a small pydantic spec + one-entry `{name: profile}`
   registry (mirroring `sources.registry`/`config.McpServerSpec`). `build_agent(profile=…)` resolves
   `None`→global default, narrows the tool/MCP surface, and swaps instructions/harness. Every
   override field is `None`-defaulted so the `"default"` profile reproduces today's agent verbatim
   and `profiles.py` imports neither `chemclaw_agent` nor `settings` (no cycle, no second config).

**Invariant preserved.** A profile *attenuates, it never authorizes*: the narrowing happens before
the unconditional audit + per-tool authz middleware and the skill role-gates, so a profile can
remove capability but never bypass RBAC or the PR-gate. An unknown tool/MCP name in a profile is a
build-time error, not a silently-empty surface (fail-fast, matching the config `@model_validator`s).

**Deliberate deviations from the spikes (KISS / Rule of Three).** Spike 1's `agent_facing` flag was
dropped — no hidden in-process tool exists today, so the flag would be a speculative param; add it
when a second, non-advertised tool appears. No `make tool-validate` target was added — name drift is
already guarded by `tests/test_agent.py::test_instructions_only_name_available_tools` plus the
registration guard, so a separate CLI gate would be redundant churn.

**Staging.** Profile Stage 2 (front-door `POST /sessions` selection) and Stage 3 (filesystem-discovered
profiles) remain deferred until a **second real use case** forces them (BACKLOG). The `DataSourceSpec`
discriminated union (audit item 4) landed subsequently — see D-076.
