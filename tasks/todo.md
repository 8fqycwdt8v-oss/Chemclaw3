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

### M1 — the engine skeleton and the LLM seam (M2 merged in)
- [x] `agent/langgraph_agent.py` — `build_langgraph_agent()` over `create_agent`, same
      instructions/tools/profile narrowing as the MAF agent. **Not** `agent/graph.py`: in this
      codebase *the graph* is the knowledge graph (`kg/graph.py`, whose own `build_graph` builds
      the NetworkX index), and two unrelated `build_graph`s one import apart is exactly the name
      collision `ARCHITECTURE.md` exists to prevent.
- [x] `instructions_for(profile)` extracted in `chemclaw_agent.py` — third caller of a fallback
      rule the file already records as having drifted once when duplicated.
- [x] `llm_provider.build_chat_model()` beside `build_chat_client()` (M2, merged: a graph cannot
      be built without a model). Shared endpoint/credential/route/transport, shared
      `_require_anthropic_key`.
- [x] Deps into `pyproject.toml` + `_STACKS` + the `("chemclaw.agent", "langgraph")` row.
      `langchain_openai`/`langchain_anthropic` map to the **`llm`** stack, not the framework's —
      they are provider SDK wrappers, and labelling them otherwise would let any package holding
      the framework row build a model client.
- [x] `tests/test_langgraph_agent.py` — the loop runs a tool call to a final answer, the surface
      equals the registry, a profile strictly narrows.
- [ ] `agent/state.py` — **deferred to M5**, the first phase with a field to put in it
      (`plan_hash`, `approvals`). A state schema whose every field is unread is a stub.
- [ ] `agent/harness_types.py` deletion — **moved to M13**. `loop_cap.py` and `plan_gate.py` still
      import it, and those are the MAF path, which stays live until the branch is deleted.

### M3 — tool middleware chain
- [x] Six `@wrap_tool_call` wrappers, attached in the MAF chain's nesting order (converters
      outermost → audit → authz → dry-run → repeat → announce innermost).
- [x] **The decisions extracted first, the plumbing ported second.** `tool_authz.dry_run_refusal`,
      `.denial_result`, `.domain_error_result`, `.failure_detail`; `repeat_guard.count_call`;
      `audit._recording` (an `asynccontextmanager` holding the whole GxP trail — identity
      precedence, span, latency, the three outcomes, the shielded cancelled-write). Both engines'
      middlewares are wrappers over these and nothing else, so a refusal's wording, a repeat
      threshold or an audit row cannot depend on `agent_engine`.
- [x] `agent/authz.py`, `audit_store.py`, `audit_anchor.py` untouched — already framework-free.
- [x] Tests drive real turns through the compiled graph and assert what the *model* is handed;
      they deliberately do not restate the decisions, which are already pinned against the shared
      functions in `test_tool_authz.py` / `test_repeat_guard.py` / `test_audit.py`.
- [ ] `enforce_plan_approval` is the seventh and belongs to M5 — it reads plan/session state this
      engine does not have yet.

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

**M1.** The engine builds and runs; `make lint type test` green at 3921 passed, 0 failed.
Four things went differently from the plan, all of them discovered by doing it:

- **M2 merged in.** A graph cannot be compiled without a model, so the LLM seam landed here.
- **`state.py` deferred to M5, `harness_types.py` deletion moved to M13.** Both for the same
  reason the M0 deps moved: a state schema with no field anyone reads is a stub, and
  `harness_types.py` is still imported by `loop_cap.py`/`plan_gate.py` on the live MAF path.
- **Named `langgraph_agent.py`, not `graph.py`.** See above — `build_graph` was already taken by
  the knowledge graph.
- **A real mistake worth recording:** `tests/test_graph.py` already existed (23 tests for the
  NetworkX indexer) and was overwritten before the collision was noticed. Caught by comparing
  collected-test counts across the change rather than by the suite, which passed either way —
  a deleted test file cannot fail. Restored from `HEAD` and verified byte-identical; the new
  tests live in `tests/test_langgraph_agent.py`. The lesson is in `tasks/lessons.md`.

Measured while building, and both change later phases:

- `deepagents.SkillsMiddleware` registers **no** skill tools. It puts skill *paths* in the system
  prompt and expects the model to `read_file` them, so progressive disclosure depends on a
  filesystem tool over the same backend. That makes M4's custom backend load-bearing twice: it
  narrows what is *listed* and it bounds what can be *read*. An unscoped `FilesystemBackend` would
  hand a GxP system a general file-read primitive — the opposite of what D-038 bought by disabling
  MAF's file batteries. `skill_tool_names()` has no direct equivalent and must be re-derived.
- `SkillsMiddleware.before_agent` caches `skills_metadata` in state and skips the load when it is
  already present "from a prior turn or checkpointed session". With M6's checkpointer that means
  role-scoped narrowing is computed once per session — a role change mid-session would read stale.
  Needs an explicit answer in M4, not a discovery in M6.

**M3.** Six middlewares on the graph engine; `make lint type test` green at 3926 passed, 0 failed,
nothing lost (collected ids diffed again).

The shape that mattered: **extract the decision, then port the plumbing.** Every one of these
middlewares turned out to be one sentence of policy inside one framework's calling convention, and
the sentences are all load-bearing — a dry-run refusal's wording is what tells a chemist nothing
ran, the repeat threshold encodes a measured finding (7–8 identical calls, 128–142 s against
16.9 s), and the audit trail's `cancelled` outcome exists because a subtle omission in it went
unnoticed until D-130 measured it. Porting those by hand would have produced a second copy free to
drift, and an audit trail that disagrees with itself depending on a config flag is not a trail.

`audit._recording` is the one that justified the effort: ~90 lines of identity precedence, span,
latency histogram, three outcomes and a shielded write that must survive a teardown. It is now an
`asynccontextmanager` both engines wrap, and each engine supplies exactly three things — the tool's
name, its arguments, and its result.

Two differences worth recording rather than smoothing over:

- **`lg_surface_authorization_denials` exists for a different reason than its MAF twin.** MAF
  collapses every tool exception into "Function failed." with no text, so there the converter
  *recovers* a message the framework discarded. LangChain does not do that, so here the converter
  instead keeps a deliberately-worded refusal from being reported as a tool error the model might
  retry. Same behaviour, different justification; the docstring says so rather than implying the
  MAF rationale still applies.
- **A LangChain gate must echo `tool_call_id`.** An assistant `tool_use` block with no matching
  `tool_result` is a malformed exchange the provider rejects, so a refusal that forgot the id would
  turn a blocked call into a dead turn. `_refusal_message` is the one place that is handled.

Also fixed while testing: `dry_run_refusal` reads the ambient flag, so asking it *outside* the
dry-run block returns `None` — correct answer, wrong question. And most of the side-effecting
surface does not exist in the registry until something has assembled the toolset once, because
that is when the generated connector-job and template launchers register.
