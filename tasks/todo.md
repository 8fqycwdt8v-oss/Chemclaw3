# Config-extensibility — implementation of the audit backlog

Source: `docs/audit/10-config-extensibility.md` §9 (prioritized, dependency-ordered backlog).
Branch: `claude/config-extensibility-plan-0ittcz`. Each item ships green under `make lint type test`
and is a separate commit. Safety rubric (§7) — GxP audit + per-tool authz + PR-gate + fail-fast
validation — must be preserved by every change.

## Items

- [x] **1. [S] Fix `.env.example` merge conflict** (lines 156/170/173). Both sides were real config
  fields (`orchestrator_max_parallel_children`, the F10-A retrieval block, `retrieval_default_confidence`)
  and non-overlapping → keep both. Done.

- [x] **2. [M] Tool registry (`@tool` + `_TOOL_REGISTRY`).** New `agents/tool_registry.py` mirroring
  `evals/metric.py`; decorate the 12 in-process capability tools with `@tool` at their definition site;
  `build_agent._capability_tools()` assembles `[*registered_tools(), *_mcp_capability_tools()]`. Populate
  the registry via side-effect module imports (the `evals/__init__.py` idiom). Preserve the
  `[audit, enforce_tool_authz]` middleware (it wraps the assembled list — unchanged). MCP servers stay
  config-driven. **KISS deviation from Spike 1:** drop the `agent_facing` flag — no hidden in-process tool
  exists today (Rule of Three); add it only when a second, non-advertised tool appears.
  - No `make tool-validate`: name-drift is already guarded by
    `tests/test_agent.py::test_instructions_only_name_available_tools` and duplicate names by the
    registration guard — a separate CLI gate would be redundant churn.

- [x] **3. [M] `AgentProfile` seam, Stage 1.** New `agents/profiles.py`: `AgentProfile` (pydantic, like
  `McpServerSpec`) + a one-entry `{name: profile}` registry whose sole `"default"` reproduces today's agent
  byte-for-byte; `build_agent(profile=…)` resolves (None→default), narrows tools + MCP by name-subsets,
  picks instructions + harness flags. **Invariant:** a profile *attenuates* (narrows), it never *authorizes*
  — audit/authz/skill-gates run after narrowing. No front-door change (Stage 2 triggers on a 2nd use case).

- [ ] **4. [M] `DataSourceSpec` discriminated union (scoped), Stage 1.** Deferred to a follow-up commit —
  larger blast radius (`sources/`), and its forcing function (the Snowflake connector) is itself deferred.
  Ship items 2–3 first; re-evaluate here.

## Verification
- `make lint type test` green after each item.
- New tests: registry populates/guards duplicates; `_capability_tools()` set unchanged vs. the old list;
  profile default == today's agent; profile narrowing attenuates but audit+authz still attach.

## Review

Landed items 1–3 of the audit backlog as three scoped commits, each green under
`make lint type test` (ruff + mypy --strict clean; full suite 606 → 613 passed, only
offline Postgres/Temporal skips):

- **1 — `.env.example` conflict** (`b07a2b2`): kept both non-overlapping real-field blocks.
- **2 — tool registry** (`76c03b2`): `agents/tool_registry.py` (`@tool` + name-keyed registry,
  mirroring `evals.metric`); 12 tools decorated at their definition sites; `_capability_tools()`
  now assembles `[*registered_tools(), *_mcp_capability_tools()]`. Audit+authz middleware and the
  MCP config-driven path unchanged. Dropped Spike 1's `agent_facing` flag (no hidden tool today —
  Rule of Three); skipped `make tool-validate` (name-drift already test-guarded).
- **3 — `AgentProfile` seam**: `agents/profiles.py` (spec + one-entry registry) + `build_agent(profile=…)`
  that resolves `None`→global default, narrows tools/MCP, swaps instructions/harness. Default is
  byte-identical to today's agent; narrowing fails fast on an unknown name; the
  *attenuate-not-authorize* invariant is test-proven.

**Deviations from the audit/spikes, with reasons:** (a) `agent_facing` flag dropped per Rule of
Three; (b) no `make tool-validate` (redundant with existing tests); (c) profile instructions read
from `default_options["instructions"]` — MAF's `Agent` has no `.instructions` attribute.

Item 4 (`DataSourceSpec` union) deliberately left for a follow-up: larger `sources/` blast radius,
and its forcing function (the Snowflake connector) is itself deferred — no trigger has fired.
