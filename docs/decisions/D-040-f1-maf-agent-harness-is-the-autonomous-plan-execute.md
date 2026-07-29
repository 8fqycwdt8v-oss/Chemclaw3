# D-040 — F1: MAF Agent Harness is the autonomous plan/execute backbone (foundation D-020)

**Relation to D-038.** This re-integrates and supersedes the earlier harness-adoption decision
(D-038): the same `create_harness_agent` wiring, now promoted from an *optional* backbone to the
foundation's autonomous plan/execute path and refactored into `_build_harness_agent`/
`_capability_tools`/`_history_provider` (F0 options + F3 durable sessions on both paths).

**Context.** Foundations #1/#2 (an actually-run agentic loop + a visible plan/todo list) — the
Claude-Code-like experience — were absent. MAF **ships** the harness (`create_harness_agent` +
`TodoProvider`/`AgentModeProvider`/`todos_remaining`), so the decision is to *wire* it, not build it.

- **Wiring, batteries off.** `build_agent` branches on `settings.harness_enabled`; `_build_harness_agent`
  calls `create_harness_agent` over the **same** `_capability_tools()` (the full function+MCP set),
  `RoleFilteredSkillsSource`, audit middleware, and a shared `_compaction_strategy()` (extracted so
  classic and harness compaction cannot drift). MAF's generic batteries (file memory/access, web
  search, shell) are **disabled** — capability is ours (MCP servers + tools), not the harness built-ins.
- **Plan→approve→execute for free.** `AgentModeProvider` ships `plan`/`execute` modes ("present plan →
  approval → `mode_set` execute"). `harness_autonomy=plan_only` (default, pharma-safe) starts in `plan`
  and, because the loop predicate `todos_remaining(looping_modes=["execute"])` only continues in
  execute mode, the agent produces a plan and stops for approval — the pre-execution GxP gate. `execute`
  starts looping immediately, capped by `harness_max_loop_iterations` (runaway guard).
- **Classic path is the load-bearing fallback** against the harness's `[Experimental]` API — off by
  default; a test asserts it attaches no todo/mode providers.
- The completion loop is *driven* by the run service (F2); this ADR covers the wiring, proven by
  `test_agent` (todo/mode added, full toolset kept, audit kept, start-mode per autonomy).
