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

### M4 — skills · **go/no-go: GO**
- [x] **The load-bearing unknown is resolved.** Narrowing *is* expressible at the backend, and the
      read half genuinely closes. `SkillsMiddleware` discovers via `backend.ls()` and bodies are
      read via `backend.read()`, so one predicate bounds both.
- [x] `agent/skill_backend.py::NarrowedSkillsBackend` — `ls`/`read`/`glob`/`grep` all gated, the
      write half refused outright, `virtual_mode=True`.
- [x] `skill_access.skill_permits()` — the three narrowings as one engine-neutral predicate; the
      MAF `SkillsSource` decorators and this backend call the same three `permits` methods.
- [x] `langgraph_agent.skills_backend()` / `_skills_middleware()`, multi-root via `CompositeBackend`
      (the configured tree *plus* every connector bundle's own, D-118).
- [x] `tests/test_skill_backend.py` — eight tests, each asking "is it hidden" *and* "is it
      unreachable"; plus a cross-engine test that both engines narrow identically.
- [x] **The read tool.** One hand-written `read_file` bound to the profile's narrowed backend, not
      deepagents' `FilesystemMiddleware` — that would have registered write/edit/glob/grep/execute,
      a general filesystem capability acquired as a side effect of wanting to read a `SKILL.md`,
      every name of which `available_tool_names()`, the prose contract, `tool_role_gates` and the
      safety rubric would then have to answer for. Progressive disclosure needs one verb.
- [x] **The name is pinned to deepagents' own prompt**, not chosen: `SKILLS_SYSTEM_PROMPT` tells
      the model to "use `read_file` on the path shown", so any other name leaves every skill
      advertised and unloadable — a failure indistinguishable from a model declining to use skills.
- [x] **`skills_metadata` staleness fixed** by `ReloadingSkillsMiddleware`, which hides the cached
      key from the state upstream's `before_agent` reads. (The first attempt — a hook returning
      `{"skills_metadata": None}` — left the *key* present and rendered an empty list from turn two
      on; see the review section.) Proven with a real `InMemorySaver` across two turns of one
      `thread_id`, and proven load-bearing by mutation. This is what makes the engine match MAF,
      where `RoleScopedSkillsSource._permits` is consulted on every `get_skills`.
- [x] `skill_tool_names()` returns **both engines' names unioned**, not a branch on
      `agent_engine`: its four callers are validators asking a deployment-wide question, so a
      branch would make `make prose-validate` pass or fail depending on which engine happened to be
      configured (D-117 is the precedent — three validators once unioned two of four name spaces).
- [x] `build_langgraph_agent(checkpointer=...)` accepted early, because the behaviour under test
      only exists once state survives a turn. A fix whose proof waits for a later phase is a fix
      nobody has checked.

### M5 — the plan gate
- [x] `agent/state.py` — `ChemclawState(PlanningState)` with `awaiting_jobs`. `Todo` is
      `{content, status}` with **no description field**, which is where MAF's `awaiting-job:`
      marker lived, so promoting it is forced rather than merely tidier — and the gate's exclusion
      becomes structural instead of a string parse.
- [x] `TodoListMiddleware` attached; `write_todos` is the plan the gate reads.
- [x] The decision extracted: `plan_gate.plan_identity`, `.approval_stands`,
      `.plan_approval_refusal`, `.gated_call`. Both engines bind an approval to the same hash —
      the one divergence that would be *retroactive*, silently invalidating decisions a chemist has
      already recorded.
- [x] `lg_enforce_plan_approval` reads the plan from `request.state["todos"]` rather than an
      ambient session object, so it asks the plan as it stands at this instant — the property
      `plan_is_approved` had to arrange deliberately under MAF.
- [x] `harness_tool_names()` — a fifth name space in `available_tool_names()`, derived from
      `TodoListMiddleware().tools` rather than spelled out (D-117 again).
- [x] The `mode_set` retraction is simply absent: the tool is never exposed, so there is nothing
      to retract. `harness_mode.py`'s subclass-and-mutate has no counterpart here.
- [x] **Loop cap as an explicit counter.** `ChemclawState.model_calls` + `lg_loop_cap`
      (`@before_model`), which both enforces the cap and records it; `loop_cap.loop_capped` reads
      the record. Fixes MAF's blind spot at `harness_max_loop_iterations == 1`, tested at exactly
      that value and mutation-checked.
- [x] `interrupt()` — **dropped from M5 with reasons; see the corrections below.** Neither
      remaining "gate" is in-turn, so there is nothing left for it to unify here.

### M6 — durable state on the checkpointer
- [x] `agent/message_migration.py` — the pure MAF-payload → LangChain conversion, tested against
      payloads produced by MAF itself rather than hand-written dicts. Refuses an unknown role or a
      result with no `call_id` instead of guessing.
- [x] `infra/sql/043_session_message_shape.sql` + the inventory row — a per-row `message_shape`
      stamp defaulting to `maf`, so a non-atomic rollout has no ambiguous rows and the original
      stays readable until the conversion is trusted on real data.
- [x] `message_migration.convert_stored_messages()` — the resumable pass. Selects only rows still
      stamped `maf`, so a second run converts nothing and an interrupted one continues; a refused
      row is left untouched *with its stamp* and reported by id, because aborting the pass would let
      one unreadable message block every row after it.
- [x] **The rehearsal is done, against a real table.** Rows are seeded through
      `PostgresHistoryProvider` rather than by `INSERT`, so what is converted is what the production
      writer actually stores. Asserts the call/result *pairing* survives — the thing a per-row
      conversion cannot show one row at a time — plus idempotence and the refusal path.
- [x] **`agent/checkpointer.py`** — `AsyncPostgresSaver` on its **own** autocommit pool. Not the
      shared one, for three measured reasons: `setup()`'s `CREATE INDEX CONCURRENTLY` cannot run
      inside a transaction and `db._pool_for` builds pools without autocommit; one `asyncio.Lock`
      per saver serializes every checkpointer statement, with `alist` yielding *inside* both the
      lock and the borrowed connection; and the saver enters pipeline mode on whatever connection
      it borrows.
- [x] **The GDPR gap is closed.** `leaver.py::_ERASE` now reaches `checkpoints`,
      `checkpoint_blobs` and `checkpoint_writes`, scoped by `thread_id` through `session_owners`,
      before the ownership row goes. Proven end to end: seed a turn, erase, count zero.
- [x] Turn state survives a new process over the same database (two agents, one `thread_id`, the
      saver dropped between them).
- [x] `SessionOwnerStore` / `SessionTurnClaims` untouched — chemclaw policy, not framework concern.
- [ ] `session_messages` as a read-model projection; deleting the rollback watermark, the
      mid-turn-resume loop and the orphan repair; the `rollback_to`-vs-fork decision;
      `agent_durable_compaction_enabled`. **All moved to M8/M13 — see the correction below.**

### M7 — connectors and per-turn tools
- [x] Re-based on `langchain-mcp-adapters`. `ConnectorSpec` + `HeldConnectorSession` in
      `connectors/transport.py`, `mcp_connections()`/`open_connector_specs()` in
      `connectors/registry.py`, `chemclaw_agent.connector_specs(profile)` as the narrowed twin of
      `connector_tools(profile)`.
- [x] **The decision extracted first**, as in M3: `transport.absorb_connect_failure` is the whole
      degrade-unless-really-cancelled policy, and both engines' plumbing calls it. A dead connector
      cannot cost the turn on one engine and only its tools on the other.
- [x] **Each session is held on its own task**, and this is the phase's real finding — see below.
- [x] Compiled **per turn**, measured at **60 ms** against MAF's ~90 ms agent build: the thing that
      replaces a process-lived agent is cheaper than the agent was.
- [x] `tests/test_langgraph_connectors.py` — seven tests against a live uvicorn MCP server, the
      task-affinity one mutation-verified (the naive shape raises, the test catches it).
- [ ] `durable/template_activities.py`: replay through the ported `wrap_tool_call` chain —
      **moved to M8**, which is where the engine branch it must dispatch on comes into existence.
- [ ] Delete `agent/agent_pool.py` + test + the D-123 `DEFERRED.md` row — gated on M12's probe.

### M8 — streaming and the event contract
- [x] `api/graph_stream.py` — `astream(stream_mode=["messages","updates","custom"], subgraphs=True)`
      translated into the `Event` union. Tokens from `messages`, calls/results/plan from `updates`,
      the same signal-drain-first ordering rule the MAF loop obeys.
- [x] **The engine switch is real.** `graph_engine_selected()` replaces the refusal; exactly two
      branch points read it (`run_turn` builds the graph, `FrontDoor.turn_agent` declines to lease
      a pooled agent for it). A whole turn now serves end to end under
      `CHEMCLAW_AGENT_ENGINE=langgraph`.
- [x] `ToolCallTrace.issued`/`.returned` extracted — the announce and result *decisions*, with
      MAF's reassembly in front of them and nothing in front of them on the graph path.
- [x] `runner_usage.graph_usage_tokens` — the same `TurnUsage` arithmetic off LangChain's
      `usage_metadata`, subtracting the cache counts from `input` because that adapter includes
      them where MAF's does not (a double-count that would overstate exactly the deployments that
      cache best).
- [x] `api/events.py` gains `agent` attribution (on the three events a specialist can raise) and a
      `handoff` event, with the dev page's switch updated so `test_dev_page_events.py` still pins
      the union in both directions.
- [x] `tests/test_langgraph_stream.py` — twelve tests, the conformance one comparing the whole
      event sequence rather than membership.
- [ ] **`core/turn_signals.py` deletion — moved to M13**, with the MAF branch it still serves.
- [ ] The cross-repo sequence `Chemclaw3_mock` → `Chemclaw3` → `Chemclaw3_ui` for the two contract
      additions — **not started**; both are additive and defaulted, so no consumer is broken yet.
- [ ] `durable/template_activities.py` replay through the ported chain (inherited from M7).
- [ ] `_resume` (mid-turn job resume) on the graph path — off by default, needs its own decision.

### M9 — agent teams
- [x] Five specialists as `data/profiles/*.yaml` — `evidence`, `computation`, `design`, `safety`,
      `reporting`. A specialist is a profile plus a compiled subgraph and **not a new concept**,
      which is why `agent/team.py` is short: delegation needed the existing security model enforced
      one level down, not a new one.
- [x] `agent/team.py` + `settings.agent_teams_enabled` (off by default, per the ADR).
- [x] **Invariant 1 — attenuation only.** `team.reject_widening` compares *advertised* names of
      parent and child. New code, and `test_the_check_that_already_existed_would_not_have_caught_it`
      pins why: `_reject_unknown_tool_names` asks whether a name exists deployment-wide, so an
      escalating specialist passed it cleanly.
- [x] **Invariant 2 — `require_actor` inside a subagent.** Verified first, as the ADR demanded, and
      the answer did not depend on deepagents #569 at all — see below.
- [x] **Invariant 4 — skills do not inherit.** Falls out of building each specialist the ordinary
      way; asserted by listing `safety`'s skill tree against the supervisor's.
- [x] `safety` is **not attenuable away** — a team omitting it is refused at build time. The one
      rule here that is not attenuation.
- [x] `tests/test_agent_team.py` — 16 tests.
- [x] **Invariant 3 — the trail names the specialist beside the human.** `AuditEvent.agent`, a
      `current_specialist` contextvar, `infra/sql/044_audit_agent.sql`, `CHAIN_VERSION` 3. Not
      `actor` (the human's oid — overloading it is D-040 repeated) and not `purpose` (reserved and
      deliberately empty): two questions, two columns.
- [x] The chain-version switch became a **version→shape table**. `version < CHAIN_VERSION` was
      correct while exactly one superseded shape existed; bumping to 3 with it would have hashed
      every v2 row under v1's eight fields and reported the middle of the trail as tampered with.
      Falsified by removing the v2 row from the map and watching exactly the five v2 tests fail.
- [ ] Supervisor routing measured. `SubAgentMiddleware`'s `task` tool is the delegation path;
      `Command(goto=…, graph=Command.PARENT)` and a routing *node* are the alternative the ADR
      prefers for trace legibility, and choosing between them is an M12 measurement, not a guess.
- [ ] Emitting `HandoffEvent` from the stream — the event exists and `graph_stream` already
      attributes by subgraph namespace; nothing raises the handoff itself yet.

### M10 — `Send` fan-out
- [x] `chemclaw/retrieval/fanout.py` — one `Send` branch per source into an `operator.add` field,
      invoked by `gather_evidence`. Runs on **both** engines: a compiled graph always has a
      runtime, so the branches execute identically under MAF and only the visibility differs.
- [x] **The fan-in re-orders by source index**, because `operator.add` appends in completion order
      and both merge modes read the lists positionally (RRF takes a note's representative chunk
      from the first list that found it; the round-robin interleaves in list order). Completion
      order would make one sweep's evidence differ from the next — a reproducibility defect.
- [x] Each branch reports what it contributed: a labelled counter
      (`chemclaw_evidence_source_chunks_total{source}`) and a per-branch stream event, translated
      into the new `EvidenceSourceEvent` by `api/graph_stream`. **Zero is the signal**, and it was
      previously unobservable.
- [x] A branch that raises costs its own source, not the sweep — the connector-degradation trade,
      one layer down. Two retrievers' docstrings corrected: both justified "never raises" by
      `gather_evidence` having no `return_exceptions`, which stopped being the mechanism.
- [x] **Re-measured**: 45 graph / 8 lexical / 7 dense against the 40-chunk cap now yields
      **25 / 8 / 7**. The ADR recorded 38/0/2 under the flat union and 40/0/0 with the score sort
      removed — both starved legs now contribute every hit they had.
- [x] `tests/test_evidence_fanout.py` — seven tests, including the measurement and an end-to-end
      one driving a real turn so the report has to cross both the tool and `Send` boundaries.
- [ ] Solvent screens and conformer sweeps — the plan's other named fan-out candidates. Untouched;
      unlike the evidence sweep they really do serialize, so they are the case where `Send` would
      buy latency rather than visibility.

### M11 — long-term memory on `BaseStore` · **not adopted**
- [x] Investigated, and **closed having built nothing** — see
      [`D-2026-08-10-basestore-is-not-where-this-systems-memory-lives`](../docs/decisions/D-2026-08-10-basestore-is-not-where-this-systems-memory-lives.md).
      The phase's premise did not survive contact with the package: `chemclaw/memory/` is fourteen
      modules of which **one** touches a database, and the other thirteen emit Markdown notes that
      go to Git through the PR-gate. There is no cross-session-recall plumbing to replace.
- [x] The decisive finding is a **false green**, not a missing feature. `store`'s columns are
      `prefix, key, value, created_at, updated_at, expires_at, ttl_minutes` — none of them an actor
      column — so `tests/test_leaver.py`'s *derived* GDPR check would pass while a departing
      person's memories stayed. M6 hit this trap with the checkpointer and caught it because
      `checkpoints` at least had `thread_id` to join through. A safety net that returns a false
      green is worse than none, because it is trusted.
- [x] `create_agent(store=…)` stays unset in `agent/langgraph_agent.py` — now a decision with a
      reason rather than an omission.

### M12 — live re-validation · **harnesses built, three runs owed a credential**
- [x] `make eval-strict` — runs offline, exit 0, 25 metrics, 0 regressions.
- [x] **`make eval-baseline-check`** — the gap nobody had noticed: `--strict` gates on
      `regressions()` and `inert_demonstrations()` and **never reads `baseline.json`**, which only a
      Temporal workflow disabled by default consumed. Now a command. Measured: 0 of 13 metrics
      worsened; a case-set mismatch refuses to compare at all and exits 1.
- [x] **Metrics gained a `Direction`**, which is what made "worse" definable. Half of them are
      ungated (`passed is None`), so the pass threshold — the only other place a direction is
      implied — does not exist for them, and 0.9 is a good `f1` and a bad `prediction_error`.
- [x] Probe harnesses built for plan→approve→execute (multi-turn `Turn`/`Intervention`, including
      the DARK-1 re-gate), `CapabilityDegradedEvent` ordering, and team routing accuracy +
      per-specialist token cost. Kept in `data/evals/probes/m12/` — a subdirectory, because
      `load_probes` globs one level, so the 190-question corpus run is unchanged.
- [x] **D-123's mechanism verified absent from the replacement** by reading: five instance-state
      sites in `agent_framework_anthropic` against **zero** `self.<attr> =` assignments in
      `langchain_anthropic/chat_models.py`.
- [x] **The degradation probe was actually executed** — 3/3, exit 0, against a real front door
      with Temporal deliberately stopped and the local mock LLM: `capability_degraded
      ['durable-jobs (Temporal)']` at event 1, first token at event 3, the launcher reached. Zero
      LLM calls. The ordering assertion is measured, not merely built.
- [x] **FIXED — the loop cap now reaches the runner.** `loop_cap.record_loop_cap()` marks the
      ambient watch when `lg_loop_cap` caps, so `loop_hit_cap()` — the one reader `run_turn`
      already calls on both paths — answers for both engines. Marking the watch rather than adding
      a branch in the runner is what keeps it one number: the count still lives in `model_calls`.
      Pinned at a cap of 1, the value MAF's inference was blind at, asserting **both** records.
- [x] **FIXED, and it was the sharper half of the defect.** `@before_model` builds its conditional
      edge *from the hook's `can_jump_to` declaration*, and `lg_loop_cap` had none — so the hook ran,
      counted correctly, decided correctly and returned `{"jump_to": "end"}` on every call past the
      limit, while the graph went on looping because there was no edge to jump along. Measured at a
      cap of 1: five hook calls, four "end" verdicts, four further model/tool round-trips completed.
      `@before_model(can_jump_to=["end"])` connects it.

      **This is why the unit test passed and the turn did not.** Calling a hook proves the decision;
      only a compiled graph proves the decision is wired to anything — the same shape as the
      `to_regclass` guard M6 nearly shipped, which also ran, also answered correctly, and was
      attached to nothing. `test_a_capped_turn_actually_stops_and_says_so` drives a whole turn and
      asserts both observable facts (one tool call, not four; and `loop_cap_reached` emitted),
      mutation-verified by removing the declaration.
- [ ] ~~OPEN: the cap may not fire end to end.~~ Driving
      `run_turn` with `harness_max_loop_iterations=1` and a scripted model, the loop made at least
      four model calls and never capped — the turn died on `StopIteration` when the script ran out,
      not on the cap. `lg_loop_cap.before_model` caps correctly when called directly (the test
      above), so the gap is between the middleware and the compiled loop: either `model_calls` is
      not accumulating across `before_model` invocations, or `{"jump_to": "end"}` is not honoured
      from that hook. **Not resolved**, and it is a runaway guard, so it should be next.
      Repro: `harness_enabled=True`, `harness_max_loop_iterations=1`, a `ScriptedChatModel` of four
      tool calls, through `run_turn` with `agent_engine=langgraph`.
- [ ] ~~A second M8 defect: the loop cap is unobservable on the graph engine.~~ `run_turn` decides whether to emit `loop_cap_reached` and increment
      `chemclaw_turn_loop_caps_total` by calling `loop_hit_cap()`, which reads the `_watch`
      contextvar — written *only* by `observe_loop_cap`, the MAF half. The graph engine's
      `lg_loop_cap` instead records `model_calls` in graph state, and nothing reads it back:
      `loop_capped(state)` has no caller on the turn path. So a capped turn on the graph engine is
      externally identical to a finished one. That is precisely the defect `lg_loop_cap`'s own
      docstring says it was written to fix — "MAF's cap fired inside `create_harness_agent` where
      nothing could observe it" — reintroduced one layer up by wiring the runner to the wrong
      reader. Fix with the metering defect, before the default flips.
- [x] **FIXED — token metering on the graph engine.** Root cause: `ChatOpenAI` default-enables
      `stream_usage` only when *no* custom base URL **and** no custom HTTP client are set, and
      `_openai_compatible_model` sets both (the internal endpoint, and a client for the private-CA
      bundle). So the endpoint was never asked to report usage and `graph_usage_tokens` correctly
      read nothing. Now passed explicitly from `settings.llm_stream_usage` (default on; a setting
      because upstream's caution is real — some endpoints reject `stream_options`). Pinned by a
      test on the built model, since the defect is a construction argument.
- [ ] ~~The probe's original finding.~~ The team arm's 15 turns wrote
      `turn_costs` rows with **zero** tokens while the MAF arm wrote 2040 per session — so
      `runner_usage.graph_usage_tokens` read nothing from the `openai_compatible` endpoint. LangChain
      reports `usage_metadata` on the *final* aggregated message, and the mock may report it in a
      shape that reader misses. **Token metering is therefore unverified on the graph engine**, which
      is precisely the failure `usage_tokens`'s own docstring records: 50 turns of 15,000 real tokens
      booked as zero while the budget guard went on allowing the next one. Fix before M13 flips the
      default — a runaway-cost guard that meters zero is disarmed.
- [ ] **Not run: the concurrency probe, plan→approve→execute, and team routing.** All three need a
      live model and this environment has no `ANTHROPIC_API_KEY`. Their harnesses ran end to end
      against the mock, which proves the *plumbing* and measures nothing: the plan-gate suite scored
      0/5 because the mock produces no todos, so the hash was `EMPTY_PLAN_HASH` and the decision was
      correctly refused 409; the team arm delegated nothing because the mock never calls the `task`
      tool. **Neither the gate nor routing accuracy has been measured**, and no number from those
      runs should be cited as if they had.
- [ ] `agent_pool.py`'s deletion stays gated on the concurrency probe *being run*, not on the
      reading above — a structural argument is not a measurement.

### M13 — remove MAF and update the documents · **scoped and started, not finished**
- [x] **Session affinity verified — and the plan's hypothesis is false.** Both Helm comments
      justified affinity partly by "the harness todo list lives in MAF `session.state`". Of the
      three things they named, two were framework state and are gone; the third — a conversation's
      uploaded **attachments** — is session-scoped and in memory *by design*, with no table
      anywhere in `infra/sql`. It never had anything to do with the framework. Affinity stays; the
      way to remove it is to give attachments a durable home.
- [x] Scoped exhaustively: **25 `agent_framework` import sites across 16 modules**, ~50 test files,
      **~166** doc mentions (the plan's "~135" was low, and the miss is concentrated in the two
      files that need real rewrites). Ordered demolition plan below.
- [ ] Steps 0–10 below. **Three of them are new code, not deletion**, which the plan did not say.

**Two plan assumptions that are wrong, found by scoping:**

- **`core/turn_signals.py` is not a MAF module and cannot simply be deleted.** This file said it
  dies "with the MAF branch it still serves". It has a *live LangGraph consumer*:
  `api/graph_stream.py` imports `drain` and calls it twice per turn. So M13's job is a **port** —
  eight writer call sites become `get_stream_writer()` — and only then can the contextvar go.
- **`agent_pool.py`'s probe gate is moot.** M12 left it "gated on the concurrency probe being run".
  But D-123's defect is in `agent_framework_anthropic`'s streaming parser, and once the dependency
  is uninstalled that parser is not in the tree. The pool's factory is `build_agent`, which is
  deleted regardless. It cannot survive the branch, probe or no probe.

**And one risk nobody had listed.** `core/logging.py` uses MAF's `configure_otel_providers`, and
`pyproject.toml` says the OTel SDK is a direct dependency *because* "via agent-framework … is the
import that resolves". Removing MAF removes the tracing bootstrap **for the whole process**, and no
test would notice. It needs a hand-written replacement, and it is isolated into its own step for
exactly that reason.

**The ordered demolition, and what sets the order.** `test_third_party_layering.py`'s ratchet is
bidirectional — a declared row with no import fails, and an import with no row fails — so
`_STACKS["agent_framework"]` and the last import must die in the *same* commit. Everything else is
arranged to keep that commit small and the suite green at each step.

- [ ] **Step 0 — flip the default** (2 files). `agent_engine = "langgraph"`. **This is the real
      proof gate**, and M12 left three probes unrun: say so rather than let the demolition imply
      they passed. **Attempted 2026-08-11 with Step 3 and stopped without a line of either landing:
      it is not a 2-file change, and what blocks it is Step 7.** The switch itself is trivial —
      `settings.agent_engine` is read nowhere in `src/` but `graph_engine_selected`, and
      `.env.example` parity is two lines. What is not trivial is that **the MAF `agent` argument is
      the injection seam the test suite runs on.** Sixteen test files drive `run_turn` — directly,
      or through the front door with a fake `agent_factory` — by handing it a fake MAF agent. On
      the graph branch that argument is ignored and `graph_factory` is called instead, and its
      default is the real `build_langgraph_agent`, which needs a live model. Exactly **two** tests
      in the tree pass a `graph_factory`, both in `test_langgraph_stream.py`.

      **Measured rather than argued**, with `CHEMCLAW_AGENT_ENGINE=langgraph` and nothing else
      changed (same tree, same Postgres, `--timeout=60` because the flip makes some tests hang
      rather than fail):

      - Whole suite: **67 failed, 4164 passed, 36 skipped**, against the 4231 / 36 baseline.
      - 64 of the 67 sit in twelve files — `test_runner` 22, `test_service` 9, `test_turn_signals`
        8, `test_turn_cancellation` 8, `test_mid_turn_resume` 5, `test_turn_observability` 3,
        `test_review_2026_08_05` / `test_dialogue` / `test_autonomy_eval` 2 each, and one each in
        `test_session_context`, `test_service_events`, `test_rollback_watermark_guard`.
      - The failure is the same everywhere: `RuntimeError: ANTHROPIC_API_KEY is not set`, raised
        out of `run_turn → build_langgraph_agent → build_chat_model`.
      - It is not only failures. The stall-and-cancel tests wait on an agent that is now never run,
        so the flipped suite does not terminate on its own; under `--timeout=60` it costs 12:51
        against a 7:33 baseline, and an early un-timed subset had to be killed at 15 minutes.

      **So Step 7's test re-point has to come before Step 0, not after Step 3** — and it is 67
      tests, not the ~315–420 Step 7 budgets for the middleware halves. The order below is wrong in
      exactly that one place and nowhere else this attempt could see.

      **Step 0's prerequisite landed on 2026-08-11 (Step 7a below); the flip itself has not.**
      What remains before the switch can move is Step 4's `_resume` (done), the connector
      representation (Step 3a, done) and Step 3 itself.
- [x] **Step 7a — the graph path's injection seam and the 67-test re-point.** The seam is
      `create_app(graph_factory=…)` → `app.state.graph_factory` → `FrontDoor.graph_factory` →
      `run_turn(graph_factory=…)`, deliberately the same shape `agent_factory` and
      `connector_factory` already have, so the two engines' injection points sit beside each other
      and neither is the one a test forgot. The test side is `tests/fakes_turn.ScriptedTurn`: a
      turn's behaviour written **once**, as an async generator of streamed pieces, exposed as both
      `run` (MAF) and `graph_factory` (a real `build_langgraph_agent` over a model that replays
      those pieces). A test passes the same object to both parameters and needs no branch on
      `agent_engine`.

      Result: `pytest -q` **4232 passed / 37 skipped / 0 failed** (6:57), and
      `CHEMCLAW_AGENT_ENGINE=langgraph pytest -q` **4220 passed / 49 skipped / 0 failed** (7:14,
      against the flipped run's previous 12:51 — it no longer hangs, because nothing waits on an
      agent that is never run). Collected ids: 4267 → 4269, nothing removed. `make cov` 85.81 %
      against the 84 % floor.

      Thirteen tests carry `maf_engine_only`, each naming the MAF-only subject it pins and where
      the graph engine's equivalent lives: the plan read through `runner.todo_titles` /
      `_PlanEmitter` (6), the `open_reachable` connector lifecycle (3, since reduced to 1 by
      Step 3a), MAF's
      `function_approval_request` content (1), streamed-argument reassembly and the closing
      `tool_trace.flush()` (1), the MAF tool-call event sequence (1), and the `awaiting-job:` todo
      residue (1). They go with the branch in Step 3, which is what the mark is for. One skip runs
      the other way — the graph engine's own resume test below — so the flipped run's skip count is
      37 − 1 + 13 = 49.

      **The marks are not weakened tests.** Where a behaviour genuinely exists on both engines the
      test was re-pointed rather than marked, including the two that needed a real tool node
      (`_CitingAgent` overrides `graph_factory` to compile a graph with one result-returning tool,
      because a tool *result* cannot be narrated into existence on this engine). Exactly one
      assertion was relaxed, with the measurement in its docstring:
      `test_signals_are_ordered_between_the_tokens_around_them` now pins the invariant (signals in
      recorded order, not batched at the end) rather than the exact transcript, because a fake
      model that never suspends fills `astream`'s queue before the consumer is scheduled once —
      measured at four event-loop hops — which is a property of the stream's buffering and not of
      the drain-first rule both engines implement.

      Two findings, both recorded against the steps they belong to (3a and 4 below).
- [ ] **Step 1 — `harness_types.py`** (6 files). Not free: its importers are the MAF halves of
      `loop_cap` and `plan_gate`, so it lands with them.
- [ ] **Step 2 — port `turn_signals` to the stream writer** (~18 files). Before the runner, so both
      engines stay green.
- [ ] **Step 3 — the switch and the runner's MAF branch** (~8 files). The checkpoint that proves
      the graph engine carries production alone. Two things verified while it was attempted with
      Step 0, both of which survive the re-ordering:

      - **`graph_engine_selected`'s "exactly two branch points" invariant still holds.**
        `settings.agent_engine` is read nowhere in `src/` except that one predicate, and the
        predicate has exactly two callers — `api/runner.run_turn` and
        `api/state.FrontDoor.turn_agent`. Nothing has grown a third branch since M8.
      - **`FrontDoor.turn_agent` collapses; it does not survive as an unconditional method.** With
        only the graph engine left it is `yield None` and nothing else, which takes
        `FrontDoor.agent_pool`'s only caller with it and leaves `run_turn`'s `agent` parameter with
        no consumer but `_resume`. It should be deleted and `api/routes/turns.py` should pass
        `None`, rather than kept as a context manager that yields a constant.
      - **`cli/chat.py` never reaches `run_turn`, so the engine switch does not reach `make chat`**
        (found 2026-08-11 while wiring Step 3a; not in the plan anywhere). `_run` builds a MAF
        agent with `build_agent`, opens `connector_tools()` with `open_reachable`, and takes each
        turn with a bare `agent.run` inside `converse` — the front door's whole turn lifecycle
        (budget, degradation event, audit correlation, answer gate) is reimplemented in four lines
        there and always was. It is not a connector problem and Step 3a deliberately did not touch
        it: the CLI needs a turn path, and the honest one is to route it through `run_turn` rather
        than to grow it a second engine branch. Sized here because Step 3 deletes what it calls.
- [x] **Step 3a — wire each engine's connector representation to its engine.** Found while
      re-pointing the tests (Step 7a), and it was the *fourth* live defect the flip would expose:
      `run_turn` took one `connectors` list, opened it with `open_reachable`, and handed it to
      `graph_factory`. Both of those are MAF's — `open_reachable` enters MAF tool objects as
      context managers, and `build_langgraph_agent` wants LangChain tools — while the graph
      engine's own path (`chemclaw_agent.connector_specs` + `registry.open_connector_specs`) was
      **called from nowhere in `src/`**. Measured before the fix: a graph turn with any real
      connector died at graph construction with `ValueError: The first argument must be a string or
      a callable with a __name__ … Got <class
      'chemclaw.connectors.transport.DegradingHttpConnector'>`. Every graph test that passed did so
      with `connectors=[]`.

      **The decision: the factory becomes engine-aware; `run_turn` does not grow a branch.**
      `chemclaw_agent.turn_connectors(profile)` picks the representation (specs on the graph
      engine, tool objects on MAF) and is the front door's `connector_factory` default;
      `registry.open_turn_connectors(stack, connectors)` opens whichever it is handed and returns
      the pair both engines need — the tools the model should see, and the names that did not come
      up. The runner has one connector path, not two.

      Both halves dispatch where the difference actually is. `turn_connectors` asks the engine
      because the engine is what decides; `open_turn_connectors` dispatches on the *type* it was
      handed rather than asking again, so a caller that built one representation cannot open the
      other — a mismatch that would surface as an empty toolset instead of an error. Each is one
      line from deleting at Step 3: `turn_connectors` collapses to `connector_specs`,
      `open_turn_connectors` to `open_connector_specs`, and no caller names either engine.

      **On the "exactly two branch points" invariant this step owns:** `graph_engine_selected` now
      has three callers, and the invariant is restated rather than broken. Each caller is one
      *construction* the engine choice determines — the agent, the graph, and the connectors they
      hold. What the predicate must not decide is anything else about a turn: the budget ledger,
      the degradation event, the rollback gate, the audit trail and the metrics stay engine-neutral
      by construction, which is precisely what putting the choice in `turn_connectors` preserved
      and what a third `if` inside `run_turn` would have given up.

      Verified end to end, not by inspection:
      `tests/test_langgraph_connectors.py::test_a_real_turn_reaches_a_real_connector_on_the_graph_engine`
      drives `run_turn` with its own default connector path against a live uvicorn MCP server and
      asserts the connector's output text (`echoed:hi`) arrives as a `ToolResultEvent`.
      Mutation-checked: restoring the old `connectors=list(turn_connectors())` argument to the
      graph factory turns that turn into `['capability_degraded', 'error']`.

      Two of the three `maf_engine_only` connector skips came back as **both**-engine tests with
      it: `tests/test_capability_degradation.py`'s dark-connector announcement and degrade-not-fail
      pair now build the dark connector per engine (`_dark_connector`), the graph half pointing at
      a closed port so the failure is the real open path rather than a stub reporting itself
      unreachable. The third stays marked, with its reason rewritten — it said the graph path was
      "called from nowhere in `src/`", which this step made false — because `_SpyMcpTool` counts
      its *own* `__aenter__`/`__aexit__` and the graph engine has no per-turn context manager of
      ours to count; the once-per-turn property is pinned in `tests/test_langgraph_connectors.py`
      instead.

      Both suites on the finished tree: `pytest -q` **4233 passed / 37 skipped / 0 failed** (7:12)
      and `CHEMCLAW_AGENT_ENGINE=langgraph pytest -q` **4223 passed / 47 skipped / 0 failed**
      (7:20). Against Step 7a's 4232/37 and 4220/49 the arithmetic is exact: collected 4269 → 4270
      (the one new test), flipped skips 49 → 47 (the two re-pointed), flipped passes +3.

      **A run reporting 4087 passed / 183 skipped came first, and it was the environment, not the
      code.** This container's clone had rolled back and the `chemclaw` role and database did not
      exist, so every Postgres-dependent test skipped itself by design — `psql` said `FATAL:
      password authentication failed for user "chemclaw"`. Provisioning the role and database and
      running `make db-migrate` (44 migrations) produced the numbers above from the same tree.
      Recorded because the failure looks exactly like a green suite: exit 0, nothing failed.
- [ ] **Step 4 — the M6-deferred subtractive half** (~14 files): rollback watermark, durable
      compaction, orphan repair, `PostgresHistoryProvider`. Keep `message_migration.py`.

      **`_resume` is done (2026-08-11), and it was *ported*, not removed.** The crash was real:
      `turn_agent` yields `None` on the graph engine and mid-turn resume called `agent.run` on it,
      so a turn launching a durable job under `CHEMCLAW_MID_TURN_RESUME_ENABLED=true` died with
      `AttributeError` — off by default, covered by no test, and an operator-settable knob.

      *Why ported rather than deleted*, since this step's own heading says "subtractive": removing
      it would have been a product decision smuggled in as migration cleanup. AGT-2 is a shipped
      capability with config keys, `.env.example` rows, documentation, and a metering fix of its
      own from the 2026-08-05 review; nothing about replacing the agent framework argues against
      "compute this, then reason about the result" being one exchange. And the port is not a design
      question after all — the note that its graph form is "an `interrupt()` design decision"
      overstated it. MAF's resume is a second `agent.run` on the same session; the graph's is a
      second `graph_events` over the same compiled graph and the same `thread_id`. Same shape, same
      conversation, ~15 lines. `interrupt()` would be a *different* feature (pausing the graph mid
      turn instead of re-entering it), and that remains available later without this crash sitting
      in the tree meanwhile.

      What the port moved: `_job_results_message` extracts the framing and the wording — the
      *decision*, in M3's sense — so a chemist cannot get a differently-worded continuation
      depending on `agent_engine`. `_resume` keeps only MAF's plumbing and dies with the branch.
      Covered by `test_the_graph_resume_never_reaches_for_the_turns_agent`, which drives the
      production shape (`agent=None`) and reproduces the exact `AttributeError` when the runner
      change is reverted.
- [ ] **Step 5 — the harness surface** (~12 files), including a **rewrite of `api/routes/plan.py`
      onto graph state** — new code.
- [ ] **Step 6 — `template_activities.py` onto `wrap_tool_call`** (~3 files). Two workarounds go
      away free: `skip_parsing=True` and most of `_serializable`.
- [ ] **Step 7 — the middleware MAF halves** (~50 files). **The turn-level test re-point is done —
      it is Step 7a above, and it moved before Step 0 rather than after Step 3.** What is left here
      is the middlewares' own MAF halves and the direct `lg_*` coverage that today exists only
      through whole-turn tests. The ~315–420 estimate covered both halves; 67 of them were the
      turn-level ones and they are green on both engines now.
- [ ] **Step 8 — OTel** (~4 files). Isolated: the only item that can break observability in
      production with no test noticing.
- [ ] **Step 9 — the dependency and the layering rows** (3 files, atomic per the ratchet). Verify
      by uninstalling `agent-framework-core` and running the suite green.
- [ ] **Step 10 — docs** (~15 files). `docs/reference/architektur.md` (48 mentions) is a full
      rewrite — MAF is its thesis, not a mention. **`docs/guides/harness-konzept.md` needs a human
      decision**: it is a *proposal document for a MAF feature that was built and has since been
      replaced*, so archiving is the honest answer rather than rewriting it.

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

**M4 — go/no-go: GO, with two pieces still open.** `make lint type test` green at 3938 passed, 0
failed, nothing lost.

**The question M4 existed to answer.** Can the three narrowings — deployment enablement, capability
scoping, role gating — be expressed against a backend rather than a `SkillsSource`? Yes.
`SkillsMiddleware` discovers skills with `backend.ls(source)` and their bodies are read with
`backend.read(...)`, so a single `permits(skill_name)` predicate bounds both halves.

**And the reason it had to be the backend rather than the listing.** Under MAF, narrowing the
advertised list *was* the gate — `SkillsProvider` was the only route to a body. deepagents puts
each skill's *path* into the system prompt and expects an ordinary filesystem tool to fetch it, so
a listing-only filter would hide a role-gated skill and then hand it over to anyone who guessed
`/tree/<name>/SKILL.md` — a shape the prompt has already taught the model. Four reach paths (`ls`,
`read`, `glob`, `grep`) plus their async twins had to be closed, not one.

Three things measured rather than assumed:

- **`virtual_mode=False` is deepagents' default and its own warning says it "allows absolute paths
  and `'..'` to bypass `root_dir`".** In a GxP system that is a general file-read primitive handed
  to a model. The backend sets `virtual_mode=True`; `test_path_traversal_is_refused` pins it.
- **The async twins need no override** — `FilesystemBackend` implements them as
  `asyncio.to_thread(self.read, ...)`, so they dispatch through the subclass. The gate depends on
  that upstream detail completely, so it is a test rather than a comment.
- **`test_every_reach_path_the_protocol_exposes_is_gated` probes the protocol rather than a written
  list**, so a future release adding a reach path fails here instead of opening a quiet hole.

The refusal deliberately does not distinguish a gated skill from a nonexistent one — otherwise the
gate is an enumeration oracle.

**M4 closed.** `make lint type test` green at 3942 passed, 0 failed; `make prose-validate` and
`make skill-validate` both pass. Nothing lost.

The read tool turned out to be the more interesting of the two. The obvious move —
`FilesystemMiddleware`, which exists precisely to give a model filesystem tools — is the wrong one
here: it registers a write/edit/glob/grep/execute surface, and in this system every tool name has
four other obligations (the prose contract, `available_tool_names`, `tool_role_gates`, the safety
rubric). Acquiring a general filesystem capability as a side effect of wanting to read one Markdown
file is exactly what D-038 refused when it disabled MAF's batteries. One verb, bound to the
narrowed backend, carrying no authority of its own.

Its *name* is not a design choice at all, which is easy to miss: deepagents' skills prompt names
`read_file` explicitly, so the tool is only correct if it matches. Pinned against the prompt.

The staleness fix was verified the way this file keeps insisting on: the hook was deleted and the
test watched to fail. Without that check it would have been a test that passes because the caching
never engaged under `ainvoke` without a checkpointer — which is precisely the shape of a green test
that proves nothing.

## Review of M0–M4 (before starting M5)

Six findings, all fixed. Three were real defects, two were tests that proved less than they
claimed, one was a config knob that lied.

**1. The staleness "fix" deleted the skills layer.** `reload_skills_each_turn` returned
`{"skills_metadata": None}` to clear the cache. That writes `None` into the slot but leaves the
*key* present, so `SkillsMiddleware.before_agent` still short-circuited and rendered an empty list:
measured, **28 skills on turn one and 0 on every turn after**. Worse than the staleness it
replaced. Replaced by `ReloadingSkillsMiddleware`, which hides the key from the state its
`before_agent` reads, so the load actually runs. Verified by mutation: reintroducing the staleness
fails the test, and forcing an empty listing fails it too.

**2. The test that caught it could not have caught it.** It asserted only "the gated skill is gone
from turn two", which an empty list also satisfies — `set() == set() - {gated}` holds. And its
parser silently matched nothing, so the set comparison was vacuous on both sides. The test now
asserts turn two equals turn one *minus exactly the gated skill*, and `_listed_skills` asserts it
parsed something, so a broken parser is a failure rather than a quiet pass.

**3. A refusal meant different things on the two engines.** `_refusal_message` set
`status="error"`, which reaches Anthropic as `is_error` on the tool_result block. The MAF twin
deliberately makes a denial the tool's *successful* result so the model reads it as the answer
rather than a transient failure worth retrying. Exactly the divergence M3's shared decisions exist
to prevent, sneaking back in through the envelope they were wrapped in. Removed.

**4. `agent_engine` was a knob that did nothing.** Added in M0, documented in `.env.example`,
enforced-as-documented by `test_config.py` — and read by no code at all, so
`CHEMCLAW_AGENT_ENGINE=langgraph` silently served MAF. `build_agent` now refuses, naming the phase
(M8) that makes the selection real. A config value that quietly does nothing is worse than one that
is missing.

**5. Dead parameters.** `skills_middleware(profile, tools, backend=None)` had one caller which
always passed `backend`, making the other two dead and the fallback unreachable. Now
`_skills_middleware(backend)`, private, since `skills_backend` is the seam tests use.

**6. Stale prose.** The engine module still said skills were "deliberately not here yet", and the
test module still said asserting on M3–M4 would be "a test of a plan". Both were true when written
and false when read, which is the failure mode `CLAUDE.md` opens by describing.

Two claims were checked and held, so they are now pinned rather than believed: `wrap_tool_call`
middleware nests in list order (first = outermost, measured), and no `..` path escapes the skills
tree (deepagents refuses traversal outright, so `/alpha/../beta/SKILL.md` raises rather than
resolving past the gate).

**M5 (partial).** The plan gate holds on the graph engine; `make lint type test` green at 3948
passed, 0 failed, `prose-validate` passes.

**A correction to the plan, made while implementing it.** M5 was framed as "collapse the harness
plan gate, `interaction_tools.py` and the KG PR-gate onto one `interrupt()`". Two of those three
belong together and the third does not, and the difference is their lifecycle rather than their
shape:

- The plan gate and the interaction approval are **in-turn**: a human answers in seconds while a
  turn is live, which is exactly what `interrupt()`/`Command(resume=…)` models.
- The **KG PR-gate is not an in-turn gate at all.** It is a git pull request a human reviews hours
  or days later, and D-005 is about a human signing off on a *merge*. Holding a turn — and its SSE
  stream — open across a code review is not a design, it is a leak. Forcing it into `interrupt()`
  would have replaced a durable, resumable, auditable artifact with a suspended coroutine.

So the collapse is two gates, not three, and the PR-gate stays a PR. That is a smaller win than
the plan claimed (~350 LOC was the estimate; the honest figure is lower) and it is the right
outcome.

**And the plan gate itself is deliberately still a refusal, not an interrupt.** Under MAF an
unapproved state-changing call is refused and the human approves out of band; switching the graph
engine to *suspend* instead would be a behaviour change on one engine while both are live, which is
the divergence this migration's whole discipline is against. `interrupt()` becomes the mechanism
when the front door can drive it (M8) and both engines can be cut over together — with its own
decision record, because changing when a chemist is asked is a product change and not a port.

**M5 done.** `make lint type test` green at 3950 passed, 0 failed; `prose-validate` and
`skill-validate` pass; nothing lost.

**The second correction, and it retires the phase's headline.** M5 was "collapse three gates onto
one `interrupt()`". Reading the third one closed the question: `interaction_tools.py` starts an
`InteractionApprovalWorkflow` — a *durable Temporal workflow* that holds a candidate until a click,
built precisely so the turn does **not** wait. Its own docstring says "asynchronous". So of the
three, only the plan gate is in-turn at all; the interaction approval and the KG PR-gate are both
deliberately out-of-turn holds that outlive the turn that raised them.

There is therefore nothing for `interrupt()` to unify. The estimate of ~350 LOC removed by "one
human gate" was wrong, and it was wrong because the plan grouped three things by what they *sound*
like — all three ask a human — rather than by lifecycle, which is the only property that decides
whether a coroutine can be held open across them. The real shared code was the plan-approval
decision itself, and that is extracted.

**What the loop cap turned into.** `ModelCallLimitMiddleware` was the obvious answer and does not
work here: it keeps `thread_model_call_count` (persisted, whole-session) and `run_model_call_count`
(per-turn, *not* persisted), so a checkpointed session's final state carries the wrong one and "was
this turn capped" is unanswerable from it — measured. Enforcing with it and counting again for the
record would be two counters for one number, so `lg_loop_cap` does both. That is a deliberate
departure from "use the framework's machinery", written down where the next reader will ask.

It ends the run rather than raising, matching MAF: the partial answer still goes out and a surface
marks it partial. Raising would discard work a chemist is entitled to see.

**M6 (started).** The two pieces that need no database are in and green (3962 passed). The research
behind the rest was done by three parallel subagents, and it moves M6 in ways the plan did not
anticipate. Six findings, in descending order of how much they change the phase:

**1. The checkpointer would open a GDPR hole.** `agent/leaver.py::_ERASE` deletes a departing
person's `session_messages`, `session_events` and `session_turns`, scoped through `session_owners`,
in a load-bearing order. `AsyncPostgresSaver.setup()` creates **four more tables** —
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` — holding the same
conversation as graph state. Wiring the checkpointer without adding the first three to `_ERASE`
would leave a departing person's turn state behind an erasure that reports success. This is now the
first item of M6, not a detail of it, and it needs a test that derives the table list rather than
restating it.

**2. The blast radius is not `session_store.py`.** Four other places read `session_messages` in MAF
shape: `durable/retention.py:235` (`Message.from_dict` + `droppable_rows`, inside a *data-destroying*
nightly sweep), `api/schemas.py::_transcript` (the whole HTTP transcript projection, built on
`function_call`/`function_result`/`call_id`), `cli/explain.py` (the GxP "why was this run" trail,
joined on `correlation_id`), and `cli/live_storm.py:890` (`message::text like`). The plan's
"demote to a read-model projection" has to keep all four working, and `explain.py`'s join means
whatever writes the projection must keep writing `correlation_id`.

**3. `setup()` cannot run on Chemclaw's pool.** Migrations 6–8 are `CREATE INDEX CONCURRENTLY`,
which Postgres refuses inside a transaction block, and `core/db.py:142` builds pools without
`autocommit`, so psycopg opens an implicit transaction on first execute. Needs a dedicated
autocommit connection or a checkpointer-specific pool — `_pool_for` already keys by `(dsn, options)`,
so a distinct pool costs nothing structurally.

**4. One `asyncio.Lock` per saver serializes every checkpointer statement**, and `alist` yields
*inside* both the lock and the borrowed connection. A paginated history read would hold a pooled
connection for the whole iteration, on a pool shared with the calculation cache and the vector
index. That is an argument for the separate pool in (3) on its own merits.

**5. A rewind is not a delete, and it becomes the head.** `checkpoint_id` is a UUIDv6, so it sorts
by time; `aupdate_state` on a historical checkpoint writes a *new* checkpoint into the **same
thread**, which immediately becomes that thread's tip. So `rollback_to(session_id, watermark)` does
not map onto "fork and carry on" as cleanly as the ADR implies — measured on a real graph. There is
an `acopy_thread` for forking elsewhere, and the M6 design has to say which of the two a disconnect
should do.

**6. State must be msgpack-encodable.** The serde is `JsonPlusSerializer` over ormsgpack with a type
allowlist, not JSON. `BaseMessage` round-trips exactly (verified, including `tool_calls` and `id`),
and `ChemclawState`'s own fields are `list[str]`/`int`, so nothing is blocked — but a future state
field holding a chemclaw model needs `with_allowlist` or it raises at write time.

**And what this environment cannot do.** There is no Postgres and no docker daemon here, so the
migration rehearsal the plan names as the mitigation for the one irreversible step — running the
converter against a copy of a real `session_messages` — **cannot be performed in this session**.
The pure conversion is exhaustively tested and the shape stamp makes a bad conversion recoverable,
but the rehearsal is still owed before any deployment converts a row.

## Running the tests with a real Postgres (no docker here)

`make up` needs docker-compose and this environment has no docker daemon, so 139 tests — the whole
`session_store` / `message_pairing` / `retention` / `concurrency_claims` surface — were skipping.
They do not skip any more. The recipe, because the container is reclaimed on inactivity and the
non-obvious step is easy to lose:

```sh
apt-get install -y --no-install-recommends postgresql postgresql-contrib postgresql-16-pgvector
PGBIN=/usr/lib/postgresql/16/bin
mkdir -p /var/lib/pgdata /var/run/postgresql && chown postgres: /var/lib/pgdata /var/run/postgresql
su postgres -c "$PGBIN/initdb -D /var/lib/pgdata -A trust --encoding=UTF8"
su postgres -c "$PGBIN/pg_ctl -D /var/lib/pgdata -l /tmp/pg.log -o '-c listen_addresses=127.0.0.1' start"
su postgres -c "$PGBIN/psql -h 127.0.0.1 -c \"CREATE ROLE chemclaw LOGIN PASSWORD 'chemclaw' SUPERUSER\""
su postgres -c "$PGBIN/createdb -h 127.0.0.1 -O chemclaw chemclaw"
```

**And then the step that is not optional.** apt ships pgvector **0.6.0**; this schema's HNSW indexes
use `bit_jaccard_ops`, which arrived in **0.7**. With 0.6 every migrated-database test fails at
`operator class "bit_jaccard_ops" does not exist for access method "hnsw"` — a failure that looks
like a broken test suite rather than a stale extension. Build it:

```sh
apt-get update && apt-get install -y --no-install-recommends postgresql-server-dev-16
git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git /tmp/pgvector
make -C /tmp/pgvector && make -C /tmp/pgvector install
su postgres -c "$PGBIN/psql -h 127.0.0.1 -d chemclaw -c 'DROP EXTENSION IF EXISTS vector CASCADE; CREATE EXTENSION vector'"
```

Measured before and after: **3962 passed / 175 skipped** → **4101 passed / 36 skipped**. What still
skips is the xtb and crest binaries and the Temporal test server, which needs an outbound download
this proxy refuses. If this is worth keeping, its permanent home is `docs/guides/runbook.md` or a
`SessionStart` hook rather than this file.

**M6 done, with its remaining half moved rather than dropped.**

**The scope correction.** M6 was written as "the checkpointer takes over, `session_messages` becomes
a projection, the orphan repair and the rollback watermark are deleted". Four of those are
*deletions of the MAF path* — and the MAF path is the one serving production until M13. The front
door cannot drive a compiled graph's stream until M8, so `PostgresHistoryProvider`, `rollback_to`,
`message_pairing`'s repair, the durable compaction and `retention.py`'s pairing-aware sweep are all
still load-bearing. Deleting them in M6 would break the live engine to tidy up for an engine nothing
routes to yet.

So M6 delivers the *additive* half — the checkpointer, the erasure fix, the converter — and the
subtractive half moves to where the thing it replaces actually stops being used. This is the same
mistake the plan made in M5: grouping work by topic rather than by what has to be true when it
lands.

**A bug I nearly shipped, caught by testing the guard instead of trusting it.** The checkpointer
tables are created by `AsyncPostgresSaver.setup()`, not by a migration, so a deployment that has
never run the LangGraph engine does not have them — and erasure must not become the one operation
such a deployment cannot perform. The first guard was
`DELETE FROM checkpoints WHERE to_regclass('checkpoints') IS NOT NULL AND …`, which does **nothing**:
Postgres resolves the relation when the statement is *parsed*, so the whole erasure failed with
`relation "checkpoints" does not exist`. That is the state every current deployment is in, so the
change would have broken GDPR erasure everywhere while its own new test passed — the new test ran
against a schema where the tables existed. The check is now a separate `pg_class` query, and the
absent-table case has a test of its own driven through a schema with no checkpointer.

**Two smaller things measured rather than assumed.** `erase_actor` defaults to a *dry run* that
counts and rolls back, which is right for an unrecoverable operation and meant my first erasure
test asserted nothing while passing (`applied=False`, 13 rows reported and none deleted). And the
checkpointer pool is bound to the loop it was opened in, so a test that spans several `asyncio.run`
calls closes a pool from the wrong loop — production has one loop per process, so the tests now use
one too.

**M7 done.** `make lint type test` green at 4117 passed, 0 failed; collected ids diffed — nothing
lost, exactly seven tests added (plus the two `test_docstring_paths` parameters two new modules
bring with them).

**The finding that shaped the phase, and it was not in the plan.** The plan said "re-base the
connectors on `langchain-mcp-adapters`", implying a translation: swap the tool class, keep the
shape. The shape does not survive. MAF hands out an *unconnected tool object* that is connected
later, so `open_reachable` can gather `AsyncExitStack.enter_async_context` over six of them.
`langchain-mcp-adapters` has no such object — `load_mcp_tools` needs a **live session**, so a
connector's tools do not exist until it is open — and worse, an MCP session is an `anyio` cancel
scope, which anyio pins to the task that entered it. Gathering the enters puts each scope on a
child task and the exits on the caller's, which raises

    RuntimeError: Attempted to exit cancel scope in a different task than it was entered in

Measured directly, and the sequential form of the same code passes — which is what identifies the
cause as task affinity rather than the session. MAF never met this because it runs each
connector's lifecycle on its own task *internally*; `HeldConnectorSession` does the same thing
where a reader can see it. Concurrency is kept, and it is not optional: a dark fleet otherwise
costs the sum of its connect timeouts before the model is called at all.

The test for this was then mutation-verified rather than trusted — the naive shape was put back and
watched to raise. That matters because the test would have passed against a single connector
however it was written; it needs three, opened together, to see the bug.

**One thing the library gives back.** `DegradingHttpConnector.close` exists because neither MAF nor
the MCP SDK closes a caller-supplied `httpx.AsyncClient` — six leaked clients per turn, the D-119
leak class. The adapter's `httpx_client_factory` seam lets `connector_http_client` cross unchanged
(so the redirect refusal, the identity hook, `auth_for` and the split connect/read timeout all
survive), and `_create_streamable_http_session` enters the client it builds with `async with
client`. So the leak cannot arise on this engine and the ownership workaround has no counterpart.

**Two items moved rather than done.** `durable/template_activities.py`'s hand-built
`FunctionInvocationContext` replay goes to M8: it has to *dispatch* on the engine, and the engine
branch does not exist until M8 makes `build_agent` able to return a graph. Doing it here would mean
writing a branch on a condition that is still unreachable. `agent_pool.py`'s deletion stays gated on
M12's concurrency probe, as planned — the D-123 defect is `agent_framework_anthropic`-specific and
almost certainly absent here, but "almost certainly" is what the probe is for.

**M8 (partial — the engine serves turns; three items remain).** `make lint type test` green at
4133 passed, 0 failed.

**What landed.** The front door can drive a compiled graph, so `CHEMCLAW_AGENT_ENGINE=langgraph`
now selects an engine instead of raising. The shape that made this cheap was deciding *not* to
adapt: the tempting move is to wrap the graph so it yields MAF-shaped updates and the runner's loop
consumes it unchanged, which would mean impersonating one framework's private update shape with
another's — the shape `runner_trace` already refuses to import because it is not stable enough to
depend on. Emitting the contract directly is less code and is the thing tests actually pin.

Everything else in `run_turn` — the budget ledger, the rollback gate, the cancellation teardown,
the metrics, the answer assembly — turned out to be genuinely engine-neutral and was not touched.
That is the payoff from M3's discipline showing up two phases later: the parts that differ between
engines are the parts that were already isolated.

**Two things measured rather than assumed.**

`ToolCallTrace` is not only an event source — it is what *grades the answer*. `build_answer_event`
scores grounding against `trace.outputs` and `trace.called_tools` after the stream ends, so an
engine that emitted a flawless event stream and left the trace empty would mark every answer
fabricated, which is the exact failure `docs/archive/live-grounded-2026-08-03.md` records. Hence
`test_the_trace_the_answer_gate_reads_is_populated`, which asserts the thing no event assertion
would have caught.

And the two providers disagree about what an input token *is*. LangChain's adapter includes cache
reads in `input_tokens` and then breaks them out again under `input_token_details`; Anthropic's API
(and MAF, passing it through) excludes them. Reading both without adjusting bills every cached
token twice — once cheap, once expensive — and overstates the priced input of precisely the
deployments that cache best, which is the population the REV-10 split exists to measure.

**A bug my own tests found, and it was in the runner rather than the tests.** The graph path called
`checkpointer()` unconditionally, so a deployment on the in-memory session store would have had to
reach Postgres to take a single turn. It surfaced as two unrelated tests failing under random
ordering — the loop-bound saver global outliving the `asyncio.run` that made it — which is a
symptom worth remembering: an ordering-dependent failure in a module I did not touch was a real
dependency I had added. The checkpointer is now gated on the same `session_store` setting
`history_provider` reads, so the two engines cannot disagree about whether a conversation survives
a restart.

**Three items remain, and two of them are deferrals with the same reason as M5's and M6's.**
Deleting `core/turn_signals.py` would delete the mechanism the MAF path still uses for every job,
proposal, question, approval and tool failure — so it goes to M13 with that branch, and until then
both engines drain the one contextvar rather than maintaining two mechanisms. `_resume` (mid-turn
job resume) is off by default and its graph equivalent is an `interrupt()` design decision, not a
port. The third — the cross-repo event-contract sequence — is genuinely not started; both
additions are defaulted and additive, so nothing downstream is broken meanwhile.

**M9 (partial — the invariants hold; routing is unmeasured).** `make lint type test` green at 4163
passed; the single failure in that run was `test_reizman.py` timing out under CPU contention from a
second concurrent pytest session, and it passes in 50 s alone.

**The substrate was already here, and that is the finding.** `AgentProfile` is an attenuate-only
bundle discovered from `data/profiles/*.yaml`, so a specialist is a profile plus a compiled subgraph
and not a new concept. Delegation needed the *existing* security model enforced one level down, not
a new one — which is why `agent/team.py` is short and why three of the four invariants are a page of
code rather than a subsystem.

**Invariant 1 needed code, and the reason is worth keeping.** `_reject_unknown_tool_names` asks
whether a profile names a tool the *deployment* provides. That catches a typo and says nothing about
privilege: a specialist naming a tool its supervisor was narrowed out of passed it cleanly.
`reject_widening` compares the two *advertised* surfaces, and
`test_the_check_that_already_existed_would_not_have_caught_it` pins the distinction so nobody
deletes it believing the older check covered it.

**Invariant 2 needed none, and that is also a finding.** The ADR asked to verify identity
propagation *before* building, because deepagents #569 questioned whether `runtime.config` reaches a
subagent. The answer turned out not to depend on it: Chemclaw's actor never travels through graph
state or through `RunnableConfig`. It is a contextvar bound around the whole turn, a subagent runs
inside a parent tool call, and both LangGraph's executor and LangChain's sync-in-async bridge spawn
with `copy_context()`. So `_EXCLUDED_STATE_KEYS` is irrelevant — there is nothing identity-shaped in
state to filter — and the real question was whether execution ever leaves the turn's context. It
does not, and propagation is strictly downward, which is the polarity that makes it safe.

**The one that would have been silent.** `mypy` objected that `_AttributedSpecialist` is not a
`Runnable`. Reading that complaint instead of casting it away found the hole: `SubAgentMiddleware`
binds each subagent's config with `with_config` and invokes *the result*. Forwarded through
`__getattr__`, that call returns the bare inner runnable — so every specialist's tool calls would
have landed in the audit trail attributed to the supervisor, with nothing failing, nothing logged
and no test noticing. There is no observable symptom, which is exactly why it has a test.

**And a latent bug in the chain versioning, found while implementing invariant 3.** The switch in
`audit_store.chain_hash` was `if version < CHAIN_VERSION: payload = select(_V1_FIELDS)` — correct
while exactly one superseded shape existed, and silently wrong the moment a second appeared. Bumping
to 3 with that code would have hashed every **v2** row under v1's eight fields and reported the whole
middle of the trail as tampered with. It is now a version→shape table, so adding v4 is one row.

**What is not done.** Supervisor *routing quality* is unmeasured, and that is the whole reason the
team ships disabled: a supervisor that mis-routes is worse than the single agent it replaces, and no
unit test can establish which a deployment gets. `HandoffEvent` exists and `graph_stream` already
attributes by subgraph namespace, but nothing raises the handoff itself yet. Whether delegation
stays `SubAgentMiddleware`'s `task` tool or becomes a routing *node* with
`Command(goto=…, graph=Command.PARENT)` — which the ADR prefers for trace legibility — is an M12
measurement rather than a guess to make now.

**M10 done.** `make lint type test` green at 4174 passed, 36 skipped, 0 failed.

**The plan's stated reason for this phase was wrong, and finding that out is most of what M10
taught.** It said `Send` would make the evidence sweep "real map-reduce" where the sources "today
serialize". They do not: `gather_evidence` has gathered its retrievers with `asyncio.gather` since
the sweep was written, its own comment explains why, and `test_gather_evidence_runs_its_sources_
concurrently` has been asserting it. So the latency win did not exist to be won.

What *was* missing is visibility, and that gap is not hypothetical — it is the whole of
`D-2026-08-01-a-cap-that-starves-a-source`, where one retrieval leg contributed **zero** surviving
chunks while the sweep looked healthy in aggregate, went unnoticed until someone counted by hand,
and had two competing explanations that were both wrong. Nothing in the tree reported per-source
contribution: no counter, no log line, no event. Two test assertions were the only place those
numbers had ever existed.

**The re-measurement, which is the phase's acceptance.** The ADR's mixed sweep — 45 graph hits at
the notes' 0.8 confidence, 8 lexical at ts_rank 0.02–0.09, 7 dense at cosine 0.60–0.85, against the
40-chunk cap — now yields **25 graph / 8 lexical / 7 dense**. The ADR recorded 38/0/2 under the flat
union and 40/0/0 with the score sort removed. Both previously-starved legs now contribute every hit
they had. The test asserts the *property* (every source with hits survives the cap) rather than the
exact split, because the defect was never a ratio, it was a zero — and pinning 25/8/7 would freeze
the round-robin's arithmetic against a corpus shape nobody promised.

**The finding worth keeping.** `operator.add` fans in whichever branch finishes first, and both
merge modes read the lists *positionally*: `reciprocal_rank_fusion` takes a note's representative
chunk from "the first one encountered across the lists (stable input order)", and the round-robin
interleaves in list order. A completion-ordered fan-in would therefore return different evidence
for the same question on different runs — a reproducibility defect in a GxP system, not a
nondeterminism nobody notices. Every branch carries its source index and the fan-in restores it.

**Three defects found by review rather than by tests.** The two new counters were undeclared, and
`record_metric` swallows the `KeyError`, so the phase's own measurement deliverable was silently
recording nothing. The fingerprint source was labelled `fingerprint` while its chunks carry
`reaction-fingerprint` — the same string `retrieval_source_weights` is keyed by — so the metric
would have named a source appearing nowhere in the evidence, which is exactly the "starved leg
looks like a missing one" confusion this phase exists to remove. And the per-branch stream event
reached `graph_stream`, which dropped it: the "visible while it happens" claim was aspirational
until `EvidenceSourceEvent` was added and wired.

**And one process note.** The first full-suite run came back "0 failed" with **182 skipped against
the usual 36** — Postgres had stopped mid-run, so 146 database-backed tests silently did not run. A
green suite with a sixth of it skipped is not a green suite; the figure above is from the re-run
with the database actually up.

**M11 — not adopted, and that is the deliverable.**

The plan said this phase would map `chemclaw/memory/` onto `BaseStore` so that "cross-session
recall stops being chemclaw-specific plumbing". That sentence is where the error is: it assumes the
package *is* recall plumbing. Fourteen modules, thirteen of them pure functions that read reactions
and emit Markdown notes for a human to merge; the package README says it in one line — "Nothing here
writes to the graph directly." A key-value store has nothing to hold there.

Four things `BaseStore` cannot express, each enforced somewhere today: the PR-gate (there is no
`put` that means *proposal*); bi-temporal retirement (`valid_to` versus overwrite-or-destroy);
the `observations_evidence_is_merged_notes` CHECK, which becomes an agent-writable `jsonb` field;
and — the one easiest to miss — **the audit trail**, because Chemclaw's six `wrap_tool_call`
wrappers key on tool names and *a store write is not a tool call*. A memory surface the GxP trail
cannot see is not one this system can have.

And it would duplicate the retrieval stack badly: `store_vectors` has no lexical half, no fusion and
no `embedding_key`, which is exactly the defect `039_note_index_embedding_key.sql` closed after a
model swap left every stored vector byte-identical and a query scored its exact match at 0.0000.

**One thing genuinely is missing** — a cross-session scratchpad outside the PR-gate. That absence is
deliberate, and filling it is a product decision about whether agents may write ungated durable
memory, not a step in porting a framework. The ADR lists what would have to ship with it.

### M12 — live re-validation · **three probes blocked on a credential**
Recorded here because the blocker is environmental, not a decision: this sandbox has Postgres but
**no Anthropic API key**, so nothing that needs a live model was run. Harnesses are built so each
probe runs the moment a credential exists; none of them is reported as having passed.

- [x] **`make eval-strict` runs offline** — verified: exit 0, 25 metrics scored, 0 regressions.
- [x] **D-123's mechanism does not exist in the replacement**, verified by reading rather than
      assumed. MAF's `agent_framework_anthropic` keeps `self._last_call_id_name` on the *client
      instance* and reads it mid-stream (five sites); `langchain_anthropic/chat_models.py` has
      **zero** `self.<attr> =` assignments in the entire module, and a streamed call carries its id
      and name on the event itself. This substantially de-risks deleting `agent_pool.py` — it does
      not replace the live probe, because a structural argument is not a measurement.
- [x] **An offline concurrency probe was attempted and deliberately discarded.** With a fake model
      `ainvoke` never reaches `_astream`, so there is no stream parser to exercise — and D-123 *is*
      a stream-parser defect. Sharing a fake's iterator across turns fails for reasons unrelated to
      the bug. A probe that cannot see the defect it is named after is worse than no probe.

**M12 — what was measured, and what is still owed.** `make lint type test` green at 4221 passed,
36 skipped. Three tests failed the first full run and none was a regression: `test_third_party_
layering` passes in isolation, and `test_reizman`/`test_xtb_thermo` are wall-clock caps that pytest
itself labels "not assertion failures" — `test_reizman` passes in 55 s once my own leftover
background pytest processes are killed, against 26 minutes of contention.

**The real find was in probe 5, and it was not the probe.** "Scored against the MAF baseline" turned
out not to be runnable at all: `make eval-strict` gates on regressions and inert demonstrations and
never opens `baseline.json`. The only reader was `durable/eval_drift.py`, a Temporal workflow
`eval_drift_enabled=False` keeps off. So the phase's fifth bullet described a comparison nobody
could perform. It is now `make eval-baseline-check`, and closing it needed something absent from the
metric registry: a **direction**. Half the metrics are ungated, so the pass threshold — the only
other place "better" is implied — does not exist for them, and without a sign a comparison cannot
tell an improvement from a regression.

**On the three probes that were not run.** They are built so that a credentialed environment runs
them unchanged, and none of them is reported as having passed. That is the same posture M6 took with
the migration rehearsal: the harness is the deliverable, the run is owed.

**And a probe I built and threw away.** An offline concurrency probe looked like a way to close #1
without a key. It cannot be: with a fake model `ainvoke` never reaches `_astream`, so there is no
stream parser — and D-123 *is* a stream-parser defect. My first version even reported "8/8 turns
consistent" while making zero tool calls, because `all(...)` over an empty list is true. The guard I
added caught it. A probe that cannot see the defect it is named after is worse than no probe,
because its green is quoted later.
