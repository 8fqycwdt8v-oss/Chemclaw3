# Rebuilding layer 1 on LangGraph

Prompted by: asking what it would cost to replace MAF with LangGraph. The answer is ~11–14 weeks,
and the reason to do it is not capability but defect load — four workarounds in this tree exist for
MAF bugs, two of which were silent (`agent_pool.py` for the 8/8 concurrent-turn failure, and the
`require_per_service_call_history_persistence` fix for the streaming defect that meant harness mode
*never worked* while every unit test passed).

Decided in [`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`](../docs/decisions/D-2026-08-10-langgraph-rebuild-of-the-conversation-layer.md)
(supersedes D-013/D-038/D-040/D-151, amends D-002's implementation) and
[`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`](../docs/decisions/D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor.md).
Full phase detail in the approved plan; this file tracks state.

**Not a port — a rebuild.** Five things that are hand-built today become framework features, each
one deleting chemclaw code (≈1,300 LOC removed against ≈3,000 rewritten). Everything lands behind
`CHEMCLAW_AGENT_ENGINE` (`maf` | `langgraph`) with both engines green, so an unfinished engine is
never what a deployment gets.

**Go/no-go is the end of M4.** M5 onward is the commitment.

## Plan

### M0 — decision record and dual-engine seam
- [x] Two ADRs + their ledger rows in `docs/decisions/README.md`.
- [x] `agent_engine: Literal["maf", "langgraph"] = "maf"` in `core/config/agent.py`, documented in
      `.env.example` (`test_config.py`'s three env-example gates).
- [x] Dependencies and the `_STACKS` rows **deferred to M1**, not skipped: `pyproject.toml` states
      deps are added "only when a module actually imports them", and
      `test_third_party_layering.py` pins rows in *both* directions
      (`test_no_declared_module_stack_is_stale`), so an allow-list row cannot precede its import.
- [x] `make lint type test` green; default engine `maf`; zero behavioural change.

### M1 — state schema and graph skeleton
- [ ] `agent/state.py` — typed state + reducers. Everything MAF kept in opaque `session.state`
      becomes a named field: `messages` (`add_messages`), `todos`, `plan_hash`, `approvals`,
      `evidence` (`operator.add`), `started_jobs`, `active_agent`, `degraded_capabilities`.
- [ ] `agent/graph.py` — the `StateGraph` (`plan`/`model`/`tools`/`verify`/`answer`), compiled with
      a checkpointer. Replaces `create_harness_agent`'s opaque loop with visible control flow.
- [ ] Deps into `pyproject.toml` + `_STACKS` + the `(package, "langgraph")` allow-list rows, all
      with the first importing module.
- [ ] **Delete** `agent/harness_types.py` and `tests/test_harness_types.py` — they exist only to
      shadow MAF private aliases.

### M2 — LLM provider seam
- [ ] `agent/llm_provider.py`: `ChatOpenAI(base_url=…)` / `ChatAnthropic` branch. Keep `-> Any`,
      `model_routes`, the private-CA TLS bundle, the eager `ANTHROPIC_API_KEY` preflight.

### M3 — tool middleware chain
- [ ] Port six `@function_middleware` to `@wrap_tool_call`. Short-circuits (authz denial, dry-run
      refusal, repeat refusal) return a `ToolMessage` instead of calling `handler`.
- [ ] `agent/authz.py`, `audit_store.py`, `audit_anchor.py` are framework-free — do not touch.

### M4 — skills · **go/no-go**
- [ ] `deepagents.middleware.skills.SkillsMiddleware` over `skills/` + each connector bundle's own.
- [ ] The three narrowings (`Enabled`/`ToolScoped`/`RoleScoped`) as a **custom backend** wrapping
      `FilesystemBackend` — the middleware reaches skills only through backend APIs.
- [ ] `skill_tool_names()` keeps reading names off the middleware's own constants (D-117).
- [ ] **Stop here and reassess if narrowing cannot be expressed at the backend.** Role-gated skill
      visibility is a security property, and this is the migration's load-bearing unknown.

### M5 — one human gate
- [ ] Collapse the harness plan gate, `interaction_tools.py` and the KG PR-gate onto `interrupt()`
      / `Command(resume=…)`.
- [ ] The `mode_set` retraction disappears: never expose the tool. Keep the plan-hash binding.
- [ ] `TodoListMiddleware` replaces `TodoSessionStore`; promote `"awaiting-job:<id>"` to a real
      state field.
- [ ] Loop cap becomes an explicit counter (also fixes the `max_loop_iterations == 1` blind spot).

### M6 — durable state on the checkpointer
- [ ] `AsyncPostgresSaver`; `session_messages` demoted to a read-model projection.
- [ ] Delete the rollback watermark, the mid-turn-resume wait loop, and `message_pairing.py`'s
      orphan repair — **after** a kill-mid-turn test proves the checkpointer never half-writes.
- [ ] Keep `SessionOwnerStore` / `SessionTurnClaims` (chemclaw policy, not framework concern).
- [ ] `agent/message_migration.py` + a per-row shape version so both forms read during rollout.
      *The one irreversible step.*
- [ ] Check whether `agent_durable_compaction_enabled` collapses into the normal path; if so delete
      it, its `.env.example` rows and D-151's `DEFERRED.md` row in the same commit.

### M7 — connectors and per-turn tools
- [ ] Re-base the degrading MCP connectors on `langchain-mcp-adapters`.
- [ ] Compile the graph **per turn** — LangGraph binds tools at construction, and a connector
      connection must belong to exactly one turn. Measure against MAF's ~90 ms baseline.
- [ ] `durable/template_activities.py`: replay through the ported `wrap_tool_call` chain.
- [ ] Delete `agent/agent_pool.py` + test + the D-123 `DEFERRED.md` row — gated on M12's probe.

### M8 — streaming and the event contract
- [ ] `graph.astream(stream_mode=["messages","updates","custom"], subgraphs=True)`.
- [ ] **Delete `core/turn_signals.py`** — the contextvar side-channel exists only because MAF had no
      custom stream.
- [ ] `api/events.py` gains agent attribution + a `handoff` event. Sequence the cross-repo change
      `Chemclaw3_mock` → `Chemclaw3` → `Chemclaw3_ui`.

### M9 — agent teams
- [ ] Five specialists (`evidence`, `computation`, `design`, `safety`, `reporting`) as profiles +
      subgraphs; supervisor routing via `Command(goto=…, graph=Command.PARENT)`.
- [ ] The four invariants of the subagent ADR, each as a test — attenuation-only, `require_actor`
      inside subagents (**verify identity propagation first**; deepagents #569), audit records the
      specialist beside the human, skills do not inherit.

### M10 — `Send` fan-out
- [ ] `gather_evidence` becomes real map-reduce, one branch per source into an `operator.add` field.
- [ ] Re-measure the retrieval balance from `D-2026-08-01-a-cap-that-starves-a-source` per branch.

### M11 — long-term memory on `BaseStore`
- [ ] Map `chemclaw/memory/` onto `BaseStore` over the deployed pgvector. `Store` is memory, not
      knowledge — the PR-gate still stands between an agent and layer 4.

### M12 — live re-validation
- [ ] Concurrency probe (8 turns × 3 configs) — gates the `agent_pool` deletion.
- [ ] Durable-launcher probe — `CapabilityDegradedEvent` still precedes the first token.
- [ ] Plan → approve → execute, live. *This is the one that historically silently did not work.*
- [ ] Team routing accuracy + per-specialist token cost vs. the single-agent baseline.
- [ ] `make eval-strict` scored against the MAF baseline.

### M13 — remove MAF and update the documents
- [ ] Drop `agent-framework-*`, the `maf` stack rows, and the `agent_engine` switch with its branch.
- [ ] ~135 mentions across maintained docs; `docs/archive/` is not maintained — leave it. ADRs are
      append-only.
- [ ] Verify whether session affinity is still required at all.

## Review

_(filled in as phases land)_

**M0.** Two ADRs, the config switch, this file. One thing changed from the approved plan: the
dependency and `_STACKS` work moved to M1. Both `pyproject.toml`'s own comment ("only when a module
actually imports them") and `test_no_declared_module_stack_is_stale` forbid declaring a stack before
the import exists — the layering policy is pinned in both directions precisely so a row cannot sit
there re-blessing an import nobody has written yet. Landing them with `agent/graph.py` is the
sequencing those two rules already required.

The gate run also surfaced an unrelated pre-existing bug (confirmed against the clean tree, not
introduced here): Hypothesis drew a note body of `"\ud800"` and `Path.write_text` raised
`UnicodeEncodeError`. An unpaired surrogate is reachable — an agent-authored note arrives as JSON,
which can carry one — and it breaks the PR-gate commit, the proposal store and the index refresh
alike, so `Note` now refuses unencodable text at the schema boundary. Fixed in its own commit
(`ca37353`) to keep this one clean. `make lint type test` green: 3913 passed, 0 failed.
