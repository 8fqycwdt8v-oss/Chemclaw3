# D-038 — MAF Agent Harness as an optional third reasoning backbone

The reasoning layer (§1) had two building blocks — plain `Agent` and (planned) MAF graph
workflows. The installed `agent-framework-core` 1.11 ships a third, the **Agent Harness**
(`create_harness_agent`): a self-managed todo list (`TodoProvider`) + explicit plan/execute
mode (`AgentModeProvider`) that lets the agent decompose an open, multi-step request into a
visible, checkable plan and work through it autonomously. (Not to be confused with the Phase-5b
*report* harness, D-020 — that is a deterministic synthesis pipeline over retrievers; this is
the MAF conversation agent's own planning loop.) `build_agent` wires it behind `harness_enabled`
(default off) over the **same** tools, skills, history, compaction (D-025), and audit middleware
(D-027); the classic `Agent` stays the tested default and the one-switch fallback (the harness
API is `[Experimental]`). `harness_autonomy` gates the completion loop: `plan_only` stays
interactive; `execute` loops the agent through its todos but **only in execute mode**
(`todos_remaining(looping_modes=["execute"])`), so a plan is made — and can be approved — in
plan mode first. The loop is hard-capped by `harness_max_loop_iterations`.

This **refines D-002, it does not overturn it**: the harness is strictly MAF-internal and holds
only lightweight conversation state (the todo list); it adds **no** new durability. Long/expensive
work still hands off fire-and-forget to Temporal, which remains the only durable execution system.
The generic file-memory/file-access/shell/web-search batteries `create_harness_agent` enables by
default are turned **off** — Chemclaw's capability is its explicit tools/skills, not a generic
filesystem or shell (§6, G6). Our own deterministic compaction (D-025) replaces the harness's
default (passed as the last context provider, preserving the history→skills→compaction order).

**Does it replace the graph-based approaches? No.** It replaces neither Temporal (durability) nor
MAF graph workflows (fixed, deterministic reasoning flows). Phase 5b's report pipeline landed as a
source-agnostic pure-function core + Temporal `report_workflow` — no MAF graph-workflow code
exists in the repo, so nothing is replaced in code. The three are complementary: Temporal =
durable execution · graph workflow/deterministic pipeline = fixed flows · agent harness = open
dynamic multi-step planning. See `docs/harness-konzept.md`.
