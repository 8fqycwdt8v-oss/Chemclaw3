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
- [x] `agent/state.py` — **deferred to M5**, done there, the first phase with a field to put in it
      (`plan_hash`, `approvals`). A state schema whose every field is unread is a stub.
- [x] `agent/harness_types.py` deletion — **moved to M13**, done in its Step 1. `loop_cap.py` and `plan_gate.py` still
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
- [x] `enforce_plan_approval` is the seventh and belongs to M5 — done there — it reads plan/session state this
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
- [x] `session_messages` as a read-model projection; deleting the rollback watermark, the
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
- [x] `durable/template_activities.py`: replay through the ported `wrap_tool_call` chain —
      **moved to M8**, which is where the engine branch it must dispatch on comes into existence.
- [x] Delete `agent/agent_pool.py` + test + the D-123 `DEFERRED.md` row. **The probe gate turned
      out to be moot** and M13 Step 3 says why: D-123's defect is in the framework's Anthropic
      streaming parser, so uninstalling the dependency leaves it no surface to occur on.

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
- [x] **`core/turn_signals.py` — moved to M13**, where it was *ported* rather than deleted: it had
      a live LangGraph consumer. The contextvar and its three drains are gone; the module publishes
      through `get_stream_writer()` (M13 Step 2).
- [ ] The cross-repo sequence `Chemclaw3_mock` → `Chemclaw3` → `Chemclaw3_ui` for the two contract
      additions — **not started**; both are additive and defaulted, so no consumer is broken yet.
- [x] `durable/template_activities.py` replay through the ported chain (inherited from M7) — M13
      Step 6.
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
- [x] Emitting `HandoffEvent` from the stream — raised as a pair by `team.running_specialist`
      (enter, then `to=""` in the `finally`), so the trace's span and the audit trail's span are
      the same `try`/`finally`. Observed at the *invocation* rather than at the dispatch, which is
      what un-entangles it from the routing row above: both candidate mechanisms invoke the
      compiled specialist. `D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs`.

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
- [x] ~~`agent_pool.py`'s deletion stays gated on the concurrency probe *being run*.~~ **The gate
      dissolved rather than being satisfied**, which is a different thing and worth the distinction:
      M13 uninstalled `agent-framework-anthropic`, so the streaming parser D-123 measured is not in
      the tree and the pool's factory (`build_agent`) is gone with the branch. There is nothing left
      for the probe to be a gate *on*. The probe itself is still unrun and still worth running —
      LangGraph's own concurrency behaviour under 8 simultaneous turns is not something this
      migration measured, and the row above says so.

### M13 — remove MAF and update the documents · **done 2026-08-11**
- [x] **Session affinity verified — and the plan's hypothesis is false.** Both Helm comments
      justified affinity partly by "the harness todo list lives in MAF `session.state`". Of the
      three things they named, two were framework state and are gone; the third — a conversation's
      uploaded **attachments** — is session-scoped and in memory *by design*, with no table
      anywhere in `infra/sql`. It never had anything to do with the framework. Affinity stays; the
      way to remove it is to give attachments a durable home.
- [x] Scoped exhaustively: **25 `agent_framework` import sites across 16 modules**, ~50 test files,
      **~166** doc mentions (the plan's "~135" was low, and the miss is concentrated in the two
      files that need real rewrites). Ordered demolition plan below.
- [x] Steps 0–10 below, all landed. **Three of them were new code, not deletion**, which the plan
      did not say — and a fourth turned out to be a defect fix (Step 10's `explain`).

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

- [x] **Step 0 — flip the default** (done 2026-08-11). `agent_engine = "langgraph"`, verified at
      **4223 passed / 47 skipped / 0 failed** — byte-identical to the explicit
      `CHEMCLAW_AGENT_ENGINE=langgraph` run, with the `maf` override still green over the eight
      turn-level files (155 passed / 1 skipped). **This was the real proof gate**, and M12 left
      three probes unrun: the config comment says so rather than letting the demolition imply they
      passed. Step 3 then deleted the switch outright. **Attempted 2026-08-11 with Step 3 and stopped without a line of either landing:
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
- [x] **Step 1 — `harness_types.py`** (done 2026-08-11, with Steps 5/7) (6 files). Not free: its importers are the MAF halves of
      `loop_cap` and `plan_gate`, so it lands with them.
- [x] **Step 2 — port `turn_signals` to the stream writer** (done 2026-08-11). Its stated ordering
      — "before the runner, so both engines stay green" — was wrong, measured 2026-08-11: the
      contextvar was never MAF-shaped, `graph_stream` drained the same buffer, and
      `test_turn_signals.py` was 11 passed / 2 skipped on the graph engine. So it ran last, as
      cleanup, and what it bought is what that re-measurement predicted: the contextvar, the
      `begin_turn`/`end_turn` pair the runner's non-awaiting `finally` had to police, and **three**
      separate drains — one per streamed update, one after the graph returned ("no next iteration
      to carry the last signal"), one after a mid-turn resume. A writer publishes into the same
      stream the tokens ride, so the ordering is the stream's rather than something the runner
      reconstructs.

      **The measurement that shaped the design.** `get_stream_writer()` does not return `None`
      outside a graph — it raises `RuntimeError: Called get_config outside of a runnable context`.
      The same tools run in two places: a chat turn's tool node, and a Temporal activity replaying
      a template step (`durable/template_activities.py`), where no graph exists. An unguarded port
      would fail a durable job because a tool tried to *narrate*. So `_emit` catches `RuntimeError`
      and drops — which costs nothing that was not already lost, since the only readers are the
      front door's stream and `api/graph_stream`, and a signal recorded in an activity had no
      reader before this either.

      That guard is also why `tests/signals.py` drives a **real one-node graph** rather than
      patching `get_stream_writer`: a guard that swallows everything is indistinguishable from one
      that swallows nothing unless something proves the success path. Mutation-checked — making
      `_emit` never call the writer fails 5 tests.

      **`_signal_event` moved from `runner.py` to `graph_stream.py`.** It lived in the runner and
      was imported at call time to dodge a cycle, because the runner owned the MAF loop that
      drained the buffer. Both reasons are gone: there is one loop, it is `graph_events`, and a
      signal arrives as a stream payload.

      **One declared coupling, written down rather than worked around.** `core/turn_signals.py` now
      imports `langgraph.config`, so the kernel knows the conversation engine — a row in
      `test_third_party_layering.py` states why the alternative is worse: the *recording* ends are
      `connectors/` and `templates/`, so moving the module into `agent/` would make capability code
      import layer 1. The kernel already owns the other engines' single primitives on everyone's
      behalf (`core/db.py`, `core/temporal_client.py`).

      Suite 4178 → 4179; 2 ids removed, 3 added.

- [x] **Step 3 — the switch and the runner's MAF branch** (done 2026-08-11). The checkpoint that
      proves the graph engine carries production alone.

      **Result: 4213 passed / 36 skipped / 0 failed**, and the `maf_engine_only` category is gone
      — every remaining skip is an environment limit (Temporal's test server, the xtb/crest
      binaries). Collected ids 4270 → 4249, and the −21 is accounted for id by id rather than
      inferred: 7 from `test_agent_pool.py`, the 11 marked tests, the engine-switch test, the
      renamed resume test (removed and re-added), and 2 parametrized `test_docstring_paths` cases,
      one per deleted `.py`. 22 removed, 1 added.

      What went, beyond the switch and `graph_engine_selected`: `run_turn`'s MAF stream branch,
      `_resume` and the `agent` parameter; the route's agent lease; `FrontDoor.turn_agent` and
      `.agent_pool`; `agent/agent_pool.py` and its test; the Step 3a scaffolding, collapsing to
      `connector_specs` + `open_connector_specs` exactly as written; the 11 marked tests, the mark,
      `ScriptedTurn.run` and `_maf_update`; and the leak probe's `agent_exit_callbacks` series,
      which counted pooled agents' exit-stack callbacks and could only ever report zero.

      **D-123's defect has no surface left, which is why the lease could go rather than being
      ported.** Two turns sharing one chat client interleaved its tool-call bookkeeping and emitted
      a `tool_use` block with an empty name — 20 % of turns in a live 50-user run. A graph is
      compiled per turn around that turn's own connectors, so there is no shared object to lease.

      **`mypy --strict` was structurally blind to the three failures this step produced.** Every
      call-site change was type-checked; all three failures were
      `monkeypatch.setattr(settings, "agent_engine", …)`, which takes the attribute name as a
      *string*. Types confirmed the demolition and only the suite could confirm the tests — worth
      remembering for Steps 4–7, which delete far more settings than this one did.

      One test was kept rather than deleted with its subject:
      `test_the_graph_resume_never_reaches_for_the_turns_agent` pinned that the resume did not call
      `.run` on the `None` the front door passed. That slot no longer exists, so the defect has no
      surface; the behaviour it protected does, and it is now
      `test_the_resume_continues_the_same_graph_with_the_job_results`.

      Two things verified while this was attempted with Step 0, both of which survived:

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
- [x] **Step 4 — the M6-deferred subtractive half** (done via 4a + 4b below) (~14 files): rollback watermark, durable
      compaction, orphan repair, `PostgresHistoryProvider`. Keep `message_migration.py`.

      **STOP — this step is additive before it is subtractive, and the reason is a live regression
      Steps 0/3 have already shipped.** The plan's §2 promised that `session_messages` "survives as
      a **read-model projection** for `GET /sessions/{id}/messages` and the audit trail — written
      from the checkpoint stream, never read back into the graph", keeping the route, the ownership
      gate and the GxP transcript intact. **That writer was never built.** `save_messages` is a MAF
      `HistoryProvider` hook; the graph writes to the checkpointer and calls nothing else.

      Measured 2026-08-11 against a real Postgres, not inferred:

      - One complete graph turn through `run_turn` under `session_store=postgres` — events
        `['capability_degraded', 'token', 'answer']` — leaves **0 rows** in `session_messages`.
      - Two turns on one session leave **8 rows in `checkpoints` and 0 in `session_messages`**.

      So the blast radius is exact: **conversation continuity is fine** (the thread lives in the
      checkpointer under `thread_id = session_id`, which is why turn two still sees turn one), and
      **`GET /sessions/{id}/messages` returns `[]` for every session** — the transcript route is
      dead in production code right now.

      **No test caught it, and the reason is worth keeping.**
      `test_service.py::test_the_transcript_route_...` seeds `session_messages` by calling
      `save_messages` directly, and its docstring says it does so deliberately — "rather than by
      running the fake agent, which yields updates without persisting anything… That keeps the test
      on the route's own behavior". Sound reasoning for what it set out to pin, and it makes the
      test structurally blind to the writer's absence: it asserts ordering, flattening and the
      ownership gate over rows it wrote itself.

      **So the order inverted, and the additive half is done (Step 4a, below).** Only then the
      rollback watermark, the orphan repair and `PostgresHistoryProvider` — otherwise this step
      takes the system from "transcript silently empty" to "transcript route deleted" and locks the
      regression in.
- [x] **Step 4b — the subtractive half, now that 4a exists** (done 2026-08-11, three commits).
      Scoped by reading what
      each target is still load-bearing for, which changed the step in two ways.

      **`PostgresHistoryProvider` does not go away — it *became* the projection store.** Step 4a
      writes through its `save_messages`, so "delete `PostgresHistoryProvider`" is now wrong as
      written. What goes is the machinery it carried *because MAF made the store authoritative*,
      and each piece is deletable for a reason the new shape supplies rather than for tidiness:

      - **The rollback watermark** (`latest_message_id`, `rollback_to`, and `run_turn`'s two
        branches, ~60 lines). It existed to undo a *half-written* turn, because MAF wrote the
        thread incrementally as the run progressed. The projection writes both messages in one
        statement after the answer is assembled, and `answered = True` follows it with no `await`
        between — so there is no cancellation point that can leave half an exchange. Nothing to
        roll back.
      - **The orphan repair** (`unmatched_call_ids`, `strip_call_ids`, `_persist_repair`, and the
        repair branch in `get_messages`). Its justification is quoted in its own docstring: an
        unmatched `tool_use` "makes every later turn on that session fail outright", because the
        stored thread was fed back to the model. The graph reads the checkpointer; the projection
        holds user text and answer text and is never read back into a turn. It also contains no
        tool calls at all, so new rows cannot even produce a pair to orphan.
      - **Durable compaction** (`_compact`, `history_compaction.py`, and the
        `agent_durable_compaction_enabled` / `_min_rows` settings, which is D-151's row in
        `DEFERRED.md` — delete it in the same commit per CLAUDE.md). It existed because
        `get_messages` re-read every row before each model call, so the thread's length was a
        context cost. The only caller left is the transcript route, read by a human on reload;
        `retention_session_messages_days` is what bounds the table now.

      **`message_pairing.py` survives, and only `droppable_rows` does.** It is the one function
      with a caller outside this cluster: `durable/retention.py`'s nightly sweep uses it to refuse
      deleting a row whose partner is not also expiring (D-145). That caller is about *legacy* rows
      that still hold tool calls, and it is unaffected by any of the above.

      **What the execution changed about that scoping, all three found by checking rather than
      trusting the plan:**

      1. **`unmatched_call_ids` stays, and so does `unmatched_result_ids`.** The scoping listed the
         first for deletion with the repair. But `unmatched_result_ids` was *already* an assertion
         by design (D-145: "deliberately not wired into the read-time repair"), and once the repair
         is gone the two are the same kind of thing — a symmetric pair a test uses to prove a
         deletion stranded nothing. Only the *healing* half goes: `strip_call_ids` and
         `strip_unmatched_calls`. The old docstrings spent paragraphs on an asymmetry ("one
         direction self-heals, the other is permanent") that turns out to have been an artifact of
         the repair; deleting it made the module more coherent, not less.
      2. **There is no D-151 row in `DEFERRED.md`.** The scoping said to delete one in the same
         commit. Checked: the file has no compaction row at all. Nothing to delete.
      3. **The reason durable compaction had to go is stronger than "its caller is gone".** The
         scoping said the transcript route is the only reader now, so the re-read cost that
         motivated D-151 has evaporated. True, but the real argument is what compaction *is*: it
         applies `keep_last_conversation_groups` — a model context-window policy — to stored rows.
         That was right while the rows were the model's context. It is a category error once they
         are a GxP record, because it deletes a chemist's older messages not because policy says to
         keep less but because the model stopped needing them. Age-based retention is the policy
         statement a deployment actually makes. Also note the setting already defaulted to `False`,
         so deleting it changes no default deployment's behavior — it removes an option that had
         become the wrong shape.

      Also deleted along the way, each because its only subject was one of the three: the
      `chemclaw_rollback_watermark_unavailable_total` counter and its `PrometheusRule` alert plus
      the `rollbackWatermarkWarning` value; `chemclaw_history_rows_compacted_total`; the
      `history_repair` and `history_compaction` degraded labels; `tests/test_history_compaction.py`
      and `tests/test_rollback_watermark_guard.py`; and `tests/test_durable_compaction_gap.py`,
      whose two MAF `after_run` tests existed only as D-151's justification — its surviving
      no-window test moved into `test_session_store.py`.

      **Three tests changed subject rather than being deleted, which is the part worth checking in
      review.** `test_a_disconnect_during_a_slow_verifier_…` and `…_slow_job_result_wait_…` asked
      whether a teardown in a post-run window destroys a committed exchange. Nothing destroys
      anything now — but the predicate they were really guarding, `answered or run_complete` rather
      than `answered` alone, still governs the *state* rollback. Rewritten onto `session.state` and
      mutation-checked: flipping the predicate to `answered` fails exactly those two and nothing
      else. `test_the_load_repair_writes_back_which_is_why_a_limit_is_unsafe` said in as many words
      that removing the repair should turn it into a different test; the `LIMIT` survives for a new
      reason (a windowed transcript does not look truncated, it looks like the conversation started
      later than it did) and the replacement asserts that behaviorally against Postgres instead of
      grepping the SQL. Mutation-checked: `LIMIT 50` fails it.

      Suite 4214 → 4178, 36 skipped, 0 failed, every collected id diffed and accounted for at each
      of the three commits (42 removed, 6 added). `make cov` 85.71% against the 84% floor; all
      eight offline validators green.
- [x] **Step 4a — the transcript projection, from the turn's event stream** (done 2026-08-11).
      Written in `run_turn._record_transcript`, immediately after the answer is assembled, through
      the history provider the front door already passes. Re-measured on the same probe that found
      the regression: **0 rows → 2 rows** for one turn.

      **The lighter of the two sources, chosen deliberately and with its cost stated in the
      docstring.** Projecting from the checkpoint stream survives a process that dies mid-turn,
      because the checkpoint is already committed; this runs after the answer, so a turn killed
      before it answers leaves no transcript row. That is the same exchange the teardown path
      already rolls back, so the two agree about what a half-turn is worth — which is what makes
      the cheaper source honest here rather than merely cheaper.

      Best-effort, for the rule `chemclaw.api.tool_results` already states: a transcript is a
      rendering and no rendering is worth failing an answered turn over. An empty answer writes
      nothing, because the turn yielded an `ErrorEvent` saying nothing was produced and a blank
      assistant row would contradict it.

      `tests/test_service.py::test_a_turn_writes_itself_into_the_transcript` seeds nothing: it
      posts a message, lets the turn run, and reads the route back. **Mutation-verified** — remove
      the one writer line and it fails. That is the shape the existing transcript test could not
      have, since it asserts over rows it wrote itself.

      Two things fell out of it, both worth keeping:

      - `state=` has to be passed, or the in-memory provider has nowhere to put the messages. One
        call is now correct under both stores, which is the same reason the read route passes it.
      - `test_rollback_watermark_guard`'s fake provider implements only `latest_message_id`, so
        calling `save_messages` on it raised `AttributeError` and failed an otherwise-good turn.
        Guarded with `hasattr`, matching how the rollback two lines away already duck-types the
        same object — rather than swallowing `AttributeError` broadly, which would hide real
        faults in a provider that does implement the hook.

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
- [x] **Step 5 — the harness surface** (done 2026-08-11) (~12 files), including a **rewrite of `api/routes/plan.py`
      onto graph state** — new code.
- [x] **Step 6 — `template_activities.py` onto `wrap_tool_call`** (done 2026-08-11) (~3 files). Two workarounds go
      away free: `skip_parsing=True` and most of `_serializable`.
- [x] **Step 7 — the middleware MAF halves** (done 2026-08-11) (~50 files). **The turn-level test re-point is done —
      it is Step 7a above, and it moved before Step 0 rather than after Step 3.** What is left here
      is the middlewares' own MAF halves and the direct `lg_*` coverage that today exists only
      through whole-turn tests. The ~315–420 estimate covered both halves; 67 of them were the
      turn-level ones and they are green on both engines now.
- [x] **Step 8 — OTel** (done earlier, verified 2026-08-11). `core/logging.configure_telemetry`
      builds the `TracerProvider`, the `BatchSpanProcessor` and the OTLP span exporter directly
      against the OTel SDK, so removing the dependency cannot take the tracing bootstrap with it —
      the risk this step was isolated for. `opentelemetry-api` arrived transitively through the
      framework and is a hard requirement of `opentelemetry-sdk`, so it survives the removal;
      `pyproject.toml`'s comment now says that instead of arguing from what the framework resolved.

      **What is genuinely lost is stated rather than faked:** the framework's chat-client
      instrumentation recorded `gen_ai.client.token.usage` per model, and the LangChain stack ships
      no equivalent. `core/metrics.py` had been justifying its `profile`-only counter label by
      pointing at that histogram — a premise that no longer holds. Corrected, and the decision is
      unchanged: `turn_costs` carries per-turn model attribution
      (D-2026-08-01-spend-is-a-ledger-not-a-label), and a lossier second answer as a counter label
      would be the two systems to reconcile the original comment warned about.
- [x] **Step 9 — the dependency and the layering rows** (done 2026-08-11). The port that blocked it
      landed first, as its own commit and its own mutation check, because this is the pairing rule
      for a *data-destroying* nightly job.

      **The port found a live defect rather than merely moving code.** `message_pairing` and
      `durable/retention` read legacy rows with `Message.from_dict`, which raises `TypeError` on a
      LangChain row — so once M6 started writing the new shape, the retention sweep would crash on
      exactly the sessions that had taken a turn since, Temporal would retry to exhaustion, and
      pruning would stop silently for the sessions still in use. The fix is `stored_call_ids`,
      which reads **both** shapes and returns `None` (not empty) for one it cannot read: empty
      means "in no pairing, disposable", `None` means "nothing can be concluded", and collapsing
      them would make an unreadable row look droppable. `droppable_rows` now takes call-id sets
      rather than messages — pairing is a relation between identifiers, and that is what removed
      the last framework import from the deletion path.

      Then the dependency itself: `agent-framework-anthropic`/`-core`/`-openai` out of
      `pyproject.toml`, `uv.lock` refreshed, the three distributions **uninstalled**, and the suite
      run against a tree that genuinely cannot import them — which is the verification, not a grep.
      `_STACKS["agent_framework"]` and the two remaining `_ALLOWED_MODULE_STACKS` rows went in the
      same commit, as the bidirectional ratchet requires.

      **Two dead paths fell out of it**, each surviving until now by reading as one half of a pair:
      `connectors.registry.open_reachable` (opening process-lived connector tool objects) and
      `api.runner_usage.usage_tokens` (reading `UsageDetails`). Neither had a production caller.
      `usage_tokens`'s two tests re-point onto `graph_usage_tokens` with real chunk shapes — the
      assertions were about the arithmetic, which is unchanged.

      **And the twin-distinguishing names went with the twins.** Eight `lg_`-prefixed middlewares
      and `make_langgraph_audit_middleware` were named to sit beside something that no longer
      exists; the prose throughout the tree already cited the *unprefixed* names, so the rename
      makes ~30 docstring references true again rather than merely shorter.
- [x] **Step 10 — docs and the prose that had gone false** (done 2026-08-11). Larger than "~15
      files", because the real subject is not documents — it is that **~180 `MAF` mentions in
      `src/` are prose claims about the tree**, and roughly half of them had become false.

      The line drawn: **past tense about the framework is evidence; present tense about it is
      false.** "This is shaped this way because that framework did X" stays — deleting the reason
      leaves an unexplained shape. What went is every sentence asserting the present: a module
      docstring describing a `build_agent` and a `SkillsProvider` that no longer exist, a config
      section titled *"The MAF conversational agent"*, an audit middleware described as an adapter
      to a second implementation, `connectors/transport.py`'s "both engines live here", the runner
      usage reader's "MAF emits usage as a content".

      Left alone deliberately: `docs/reference/architektur.md`,
      `docs/planning/implementation-plan.md` and `implementation-tickets.md` each already open by
      saying they are historical build documents, and `BACKLOG.md`'s mentions are all inside closed
      `[x]` rows — a log, not a claim about now. Checked rather than assumed: no open row mentions
      the framework. `docs/archive/` is unmaintained, ADRs are append-only.

      **`docs/guides/harness-konzept.md` needs no decision and must not be archived.** This row
      said it was "a proposal document for a MAF feature that was built and has since been
      replaced", and that was checked against the file on 2026-08-11 and is false: the title is
      already *"Der Plan-/Ausführungs-Harness (LangGraph)"*, the status line reads "gebaut und in
      Betrieb hinter `harness_enabled`", and §10 records what the switch changed and why two of the
      original design decisions existed only to compensate framework defects. It is current
      documentation that was rewritten with the rebuild. Archiving it would break citations from
      three append-only ADRs (D-038, D-058, D-137) and from `agent/plan_gate.py`, to retire a
      document that is true today. Its remaining MAF mentions are §10's history, which is the one
      place they belong.

      **One of those false claims was a defect, not prose.** `chemclaw.cli.explain` said it read
      "a stored MAF message" and did exactly that — it parsed the legacy `contents` shape inline.
      Measured: every row written by any turn since M6 renders as role `unknown` with empty text,
      so the audit reconstruction that answers *"why was this run?"* for a GxP auditor showed an
      **empty conversation** for precisely the sessions still in use. It never failed; it printed
      nothing. Fixed by making `session_store.message_from_row` public and the only function
      allowed to decide which serialization a row holds — a second reader is how a table with two
      shapes acquires a reader that knows one. Mutation-checked: pinning the shape argument to
      `None` fails the new test.

      Recorded in `docs/decisions/D-2026-08-11-what-the-removal-found.md`.

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
unit test can establish which a deployment gets. Whether delegation stays `SubAgentMiddleware`'s
`task` tool or becomes a routing *node* with `Command(goto=…, graph=Command.PARENT)` — which the
ADR prefers for trace legibility — is an M12 measurement rather than a guess to make now, though
*trace legibility* is no longer part of what it has to settle: `HandoffEvent` is now raised from
`team.running_specialist`, which both mechanisms pass through, so the comparison is down to routing
accuracy and per-specialist cost (`D-2026-08-11-a-handoff-is-observable-where-the-specialist-runs`).

**The handoff gap, closed.** `HandoffEvent` had shipped as a union member nothing produced — the
one item on the migration's open list where the code made a promise it did not keep. The fix looked
blocked behind the routing choice and was not: reading the `task` tool's arguments would have bound
the contract to the delegation mechanism, while observing the specialist's *invocation* survives
either mechanism, because both invoke the compiled specialist. Raised as a pair from
`team.running_specialist`, which is already invariant 3's bracket, so the trace's span and the audit
trail's span are the same `try`/`finally` and cannot drift apart.

Falsified twice rather than asserted: stubbing the emitter fails all three new tests, and moving the
entry announcement after execution — both events still firing, in the wrong order — passes the pair
test and fails the ordering test, which is what earns the ordering test its place. `make lint type
test` green at 4042 passed, 154 skipped, 0 failed (141 of the skips are the offline sandbox's
missing Postgres, not this change).

**M12 suite C, run live for the first time (2026-08-11).** Postgres + Temporal + the four workers +
the front door, against a real credential. Three findings, and *two of them were in the reading
rather than in the system* — which is the reason to run a harness before trusting the number it
prints:

1. **The `agent` attribution named the tool node, never the specialist.** Every specialist event
   arrived as agent `"tools"`, so the suite scored its one observed delegation as
   `expected evidence → tools` — reporting a supervisor mis-route that had not happened. The name
   is not in the subgraph namespace under any dispatch through the `task` tool, so this was not a
   formatting slip. Fixed by attributing from the handoff pair, which is the only reader that holds
   the real name (`D-2026-08-11-the-specialists-name-is-not-in-the-namespace`).
2. **The cost column was `None` on every turn** while 26 rows sat in `turn_costs`. `session_tokens`
   short-circuited on *the harness process's* `session_store`, which defaults to `memory` and is
   exported by `processes.sh` only to the processes it starts. A local guess about a remote
   process; the query is now the only reader.
3. **The measurement itself: the supervisor delegated 0 times in 15 probes.** It answered every one
   directly, and used only tools the expected specialist advertises in **14 of 15** (12 of those
   unambiguously — `ask_clarifying_question` and `find_notes` are shared across profiles; rt-07
   called no tool at all). So the shape here is *not* the mis-routing M9 feared. On this model the
   single agent picks the right capability cluster on its own, and the team buys nothing it is
   paying for. A five-probe sonnet-5 arm delegated once out of fifteen before the credential's
   usage limit cut the run short, so "rarely, and model-dependent" is as far as the evidence goes.

**Both arms, on haiku, 15 probes each.** The control arm ran once the credential recovered; the
team arm's cost did not need re-running, because the ledger fix made it readable retroactively.

| arm | delegated | tool calls | median tokens/turn | mean tokens/turn |
| --- | ---: | ---: | ---: | ---: |
| team | 0 | 19 | 95,313 | 101,858 |
| single | — | 33 | 91,321 | 117,271 |

**Read the median, not the mean.** The mean says the single agent costs 15% *more*, and that is an
artefact of four high-variance turns (rt-01, rt-09, rt-13, rt-14) where it took extra tool-calling
round trips. The median is the systematic part: the team costs **~4k tokens more per turn, on every
turn**, which is the `task` tool and the supervisor prompt riding in every request. The single agent
is cheaper on **11 of 15** probes.

So the answer to M9's question, on this model and this corpus: **the team is a constant tax for a
capability that fired zero times**, and the single agent reaches the right specialist's tools
unaided in 14 of 15. `agent_teams_enabled` stays off — now on evidence rather than on caution. This
also makes the routing-mechanism choice (`task` tool vs. routing node) moot until something
actually routes: there is no trace to make legible.

**M12 suite B (degradation): 3/3 PASS**, at zero token cost, with the broker deliberately stopped —
the outage is announced, announced *before* the first token, and the durable launcher was genuinely
reached. Getting there required fixing the mock (below).

**The mock spoke a protocol nothing uses any more.** `cli/mock_llm` served only `/v1/responses`,
because it was written when layer 1 ran on the Microsoft Agent Framework, whose client resolved to
the Responses API. The LangGraph rebuild builds a `ChatOpenAI`, which posts to
`/v1/chat/completions`. Nothing followed, so **every credential-free lane** — `live-degradation`,
`live-storm`, `live-soak` — had been taking a bare `404` and dying with no answer and no tool call
since M13. First run scored 1/3 with "the turn produced no token or answer at all" while the mock's
own counter read `requests: 0`; after adding the route, 3/3 and `requests: 2` (the second call being
the answer after the tool result, which is `already_has_tool_results` correctly recognising the
chat-completions shape rather than looping to the cap).

**M12 suite A (plan gate), run live — and it found the day's fourth reading defect.** First run:
0/5, with state-changing calls apparently running unrefused. The gate was innocent twice over. The
lane needed `CHEMCLAW_HARNESS_ENABLED=true` as well as `harness_autonomy=plan_only` (the Makefile
target documents only the second), which took it to 4/5. The remaining failure was real and was
*not* in the gate: `announce_tool_failures` sat innermost, so it nested **inside**
`enforce_plan_approval`, which raises before calling its handler. The announcer never ran, and a
refusal reached the chemist only as a `tool_result` reading "Refused: …" — which a surface renders
as a step that worked. The front-door log recorded two refusals in a run the suite scored as zero.
Fixed by moving the announcer outside everything that refuses
(`D-2026-08-11-a-refusal-nobody-can-see-is-not-a-gate`); the refusal check now PASSes.

Across the two post-fix runs every one of the five checks has passed, and none has failed for a
system reason. DARK-1 shows FAIL in the latest run only because haiku left the todo list unchanged
on the third turn, so the scenario was never exercised — the harness reporting an un-taken
measurement as a miss rather than a pass, which is the rule
`test_a_script_that_never_changes_the_plan_cannot_report_dark_1_as_passed` exists to enforce. It
PASSed with a genuine hash change in the preceding run.

**The pattern across all four findings, which is the thing worth keeping.** The handoff, the
attribution, the mock protocol and the announcer were every one of them a defect in *observation*
rather than in mechanism — the specialist ran, the gate refused, the delegation happened, the
behaviour catalogue was right. Each had passing unit tests, and each test supplied the observation
by hand (an invented namespace, a hand-written SSE frame, a protocol nobody re-checked after the
engine changed). None was reachable without a live stack. The rule now written into the tests: **a
test whose subject is a reader of an external shape may not supply that shape by hand.**

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

**M13 done.** `make lint type test` green at **4115 passed / 36 skipped / 0 failed** (7:24), and
the verification that matters is that the run happened against a tree with
`agent-framework-anthropic`, `-core` and `-openai` **uninstalled** (`import agent_framework` →
`ModuleNotFoundError`). A grep proves an import is not written; an uninstall proves it cannot be
satisfied. Every skip is an environment limit — no Temporal test server (offline sandbox), no
xtb/crest binaries — and none is framework-related. Helm render + `kubeconform`, which this
container cannot run, passed on CI.

**The removal was not mechanical, and the reason generalises.** While both engines were live a
module could read the *old* stored message shape and be right about half the rows. With one engine
left, "half the rows" became "every row a live session writes", and that one sentence accounts for
three of the four findings:

- `chemclaw.cli.explain` parsed the legacy `contents` shape inline, so the audit reconstruction —
  the tool that answers *"why was this run?"* for a GxP auditor — rendered role `unknown` with
  empty text for every row written since M6. It never failed; it printed nothing, which reads as a
  quiet session. `session_store.message_from_row` is public now and is the only function allowed to
  decide which serialization a row holds. Mutation-checked: pinning its shape argument to `None`
  fails the new test.
- `connectors.registry.open_reachable` and `api.runner_usage.usage_tokens` had no production caller
  once the branch went, and had survived earlier phases by *reading as one half of a pair*. That is
  the failure mode worth naming — a symmetrical name makes dead code look load-bearing.
- `core/metrics.py` argued for its `profile`-only counter label by pointing at a
  `gen_ai.client.token.usage` histogram that went with the framework. The premise was corrected and
  the decision left alone, because `turn_costs` is where model attribution belongs
  (D-2026-08-01-spend-is-a-ledger-not-a-label) — but a comment that argues from a dead premise is
  how the *next* decision gets made wrongly.

**Prose was triaged on one line rather than swept.** ~180 `MAF` mentions in `src/`, roughly half of
them load-bearing history: *past tense about the framework is evidence and stays; present tense
about it is false and is rewritten.* Deleting the history would leave unexplained shapes — a
duck-typed reader, a per-turn connector session, a counted loop cap — each of which exists because
of something that framework did. The historical planning documents keep theirs (each already opens
by saying it is historical) and `BACKLOG.md`'s are all inside closed `[x]` rows, which is a log
rather than a claim about now. Checked rather than assumed: no *open* row mentions the framework.

**Two names that were only ever twin-distinguishers went with the twins.** Eight `lg_`-prefixed
middlewares and `make_langgraph_audit_middleware`. The tell was that the prose throughout the tree
already cited the *unprefixed* names — ~30 docstring references that were false until the rename and
true after it, which is a better argument for renaming than brevity.

**Still owed, unchanged from M12 and now a BACKLOG row of its own:** the concurrency probe, the live
plan→approve→execute round trip, and team routing accuracy. Each needs a credential or a tenant this
environment does not have. `agent_teams_enabled` stays off by default for the third of them.

---

# Deep-agents audit + the LangSmith question (2026-08-11)

Prompted by: are the deep-agents patterns properly implemented, and is LangSmith implemented — if
not, is it (or something comparable) worth adding? Audited against LangChain's own four pillars for
Deep Agents plus the middleware `create_deep_agent` composes by default.

Decided in [`D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has`](../docs/decisions/D-2026-08-11-a-policy-nobody-can-see-is-a-policy-nobody-has.md)
and [`D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape`](../docs/decisions/D-2026-08-11-the-observability-gap-is-real-and-langsmith-is-not-its-shape.md).

**The finding.** Five of six pillars are sound and each narrowing is already argued for in the tree.
The sixth — context management — does not exist, and everything that *describes* it survived the
framework removal: three settings with no reader, a config comment in the present tense, three
`.env.example` rows, and a sentence in the system prompt telling the model its context is compacted.

## Steps

- [x] **Restore D-025's policy** as `agent/compaction.py`: upstream's `ClearToolUsesEdit` for the
      tool-result half, first-party `KeepLastConversationGroupsEdit` for the conversation window,
      both inside `wrap_model_call` (non-destructive), attached unconditionally.
- [x] **Two counters** so the policy is checkable rather than believed —
      `chemclaw_context_compactions_total`, `chemclaw_context_reclaimed_tokens_total`, incremented by
      an observer nested inside the editor so it sees both the full thread and the edited request.
- [x] **Prune the checkpoint tables by thread** (`retention_checkpoints_days`), all three in one
      transaction, absent tables skipped rather than raised on.
- [x] **`core.db.existing_tables`** — extracted from `agent/leaver.py` at its second caller.
- [x] **Delete `ChemclawState.awaiting_jobs`** and rewrite the three docstrings that described it as
      live.
- [x] **Give the CLI the checkpointer it documents** (`cli_checkpointer`), handed to both the graph
      and `_plan_command`.
- [x] **Fix the stale prose**: `langgraph_agent`'s "not here yet" list, `infra/sql/README.md`'s
      `session_messages` row, and the four checkpoint tables that inventory structurally cannot list.
- [x] **Two ADRs + ledger rows + two BACKLOG rows** (the model-call span, the eval-lane spike).

## Review

**What the numbers say.** A thread of realistic turns (one 20,000-character evidence sweep each —
the largest real result `api/tool_results.py` measured) under the shipped defaults, recorded off a
fake model inside a real compiled graph. Below the budget the model gets the whole thread; above it
the sent size stops tracking the thread size:

| turns | thread tokens | sent to the model |
|------:|--------------:|------------------:|
| 10 | 51,610 | 51,616 |
| 20 | 103,230 | **13,740** |
| 80 | 412,950 | **25,140** |
| 160 | 825,910 | **40,340** |

`message_pairing.calls_without_adjacent_results` is empty on every one of those, asserted rather
than reasoned about — a reduction that strands a tool call is rejected by the API outright and
replayed on every later turn.

**What the first failing test was worth.** The end-to-end test originally asserted a cleared
placeholder *and* a shortened list in one run, and failed: with a two-group window the cleared
results were themselves dropped, so both edits were working exactly as specified while the assertion
was wrong. Split into two tests, one per edit, with the other edit's knob set out of range. The
lesson is the file's own: an assertion that spans two mechanisms cannot tell you which one moved.

**What could not be verified here.** This sandbox's pgvector is 0.6.0 and the full migration set
needs 0.7+ (`bit_jaccard_ops`), so every Postgres-backed test skips locally and runs in CI. Rather
than assert the checkpoint prune from the code, it was run against a live Postgres 16 with the
checkpointer's own DDL: an expired thread left 0 rows in all three tables, a thread inside its window
kept all 3, `_prune_checkpoints` reported per-table counts, and a schema with no checkpoint tables
reported them skipped while `session_events` was still pruned.

**What was declined and why it is a decision rather than a deferral.** LangSmith is proprietary —
client SDKs open, backend/UI/storage closed, self-host Enterprise-only and sales-gated — so the one
deployment shape this tree accepts is not on offer. Its core value is prompt/response content in a
third-party service, which four merged decisions forbid (`core/tracing.py`, `core/logging.py`,
`SECURITY.md`, D-049's self-hosted-Temporal argument). The gaps being used to argue for it split
cleanly: per-model attribution, model-call spans and dashboards are in-house work through the
collector the chart already runs, and the eval/experiment gap (AG-13) is a scoped spike on the eval
lane where a *self-hostable* tool — Phoenix or Langfuse — is the candidate, not LangSmith.

---

# Phoenix / OpenInference: a model call becomes a span (2026-08-11)

Prompted by: "then go with implementation of phoenix", after the licence comparison. Decided in
[`D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment`](../docs/decisions/D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment.md).

**What working it out changed.** "Implement Phoenix" turned out not to mean adopting Phoenix.
`openinference-instrumentation-langchain` and its closure are **Apache-2.0**; only the Phoenix
server image is ELv2, and the instrumentation emits OpenInference spans over plain OTLP without
speaking to Phoenix at all. So the backend is a deployment choice, nothing ELv2 enters this tree,
and the change closes two backlog rows that were never vendor-shaped.

## Steps

- [x] Dependency: one Apache-2.0 package, with the reason in `pyproject.toml` beside it.
- [x] `otel_llm_spans` (new, off in code / on in the chart) and **`otel_include_sensitive_data`
      revived** — it had no consumer and a warning; this asks the identical question, so it gets the
      flag back rather than a second knob.
- [x] `_instrument_llm_calls` + `_trace_config` in `core/logging.py`, lazy-imported, raising the
      same directive `RuntimeError` the SDK check raises when the extras are missing.
- [x] `tests/test_llm_spans.py` — five tests, with the content assertion written as a **sweep over
      every exported attribute** rather than a list of keys.
- [x] `.env.example`, `values.yaml`, `docs/guides/runbook.md`, `deploy/README.md` — the three places
      that said per-model attribution was gone now say what replaced it and in which pipeline.
- [x] ADR + ledger row; the two backlog rows closed, the AG-13 row narrowed to the backend.

## Review

**Measured before a line was written.** An overlay venv (`.pth` onto the project's site-packages, so
`.venv` was untouched while the gate ran) drove a scripted model through the real
`build_langgraph_agent` with spans in an `InMemorySpanExporter`. Content allowed → 5 attributes
carry the question or the answer; suppressed → **0**, with `llm.token_count.*` and `llm.provider`
byte-identical across the two runs. That is what made suppression a default rather than a trade-off:
it costs none of what the instrumentation was added for.

A tool-calling turn exports 8 spans — `AGENT` ×1, `LLM` ×2, `CHAIN` ×4, `TOOL` ×1 — so the
per-*call* attribution the backlog row asked for is there, not a per-turn total.

**The test earned its shape immediately.** `test_every_hide_flag_is_set_together` compares against
`TraceConfig`'s own dataclass fields rather than a list written twice, and it failed on the first
run: three embedding flags were missing from the suppression list. `hide_embeddings_text` is the one
that mattered — the text being embedded is a chemist's question or a note's body — so a list-versus-
list test would have passed while the default configuration leaked content.

**What this does not do.** It does not close AG-13. That row wants datasets, run-over-run diffing
and annotation on the eval lane, which is a Phoenix *deployment* run against the probe transcripts.
What changed is that the trace half of the original ask no longer needs a platform at all.

### Run against a live Phoenix (2026-08-11, follow-up)

The in-memory measurement covers spans as they are *built*. What a deployment gets is spans as they
are *exported*, so the same turn went twice through the shipped path — `configure_telemetry()`, the
real OTLP gRPC exporter, `CHEMCLAW_OTEL_ENDPOINT` — into Phoenix 20.0.0 running locally, read back
out of Phoenix's own REST API:

| | spans | traces | token counts | content-bearing attributes |
|---|---|---|---|---|
| suppressed (default) | 10 | 1 | 1234 / 56 / 1290 | **0** |
| content allowed | 10 | 1 | 1234 / 56 / 1290 | **5** |

Identical except for the one thing the flag governs, and the same five attributes the in-memory run
found — nothing is added or removed on the way through the exporter.

**The finding worth having run it for**: the first-party and OpenInference spans join into *one*
trace. Phoenix shows `chemclaw.turn` as the root, with `chemclaw` (CHAIN) beneath it, the skills
middleware (AGENT), `model` (CHAIN) → the model call (LLM) twice, `tools` (CHAIN) →
`ask_clarifying_question` (TOOL), and our `chemclaw.tool` as a second child of the turn. An operator
sees one turn, not two disconnected halves — which was an assumption until this run.

Phoenix needed Python 3.12 to import (20.0.0 declares `>=3.10` and carries a dataclass default only
3.12 accepts), so it ran in a venv of its own. That is the topology anyway.

---

# Review pass over the compaction + Phoenix change (2026-08-11)

Prompted by: "Review all changes. Implement fixes automatically. Really make it production ready."
Decided in [`D-2026-08-11-what-the-review-found-in-the-compaction-change`](../docs/decisions/D-2026-08-11-what-the-review-found-in-the-compaction-change.md).

Three methods, three disjoint sets of findings — which is the argument for running all three rather
than whichever is cheapest:

- **The review skill at max effort** found six, including the one that mattered: the observer
  middleware declared only the async hook, so every synchronous `graph.invoke()` raised.
- **A hand pass over interactions** found the two a diff-shaped review cannot see: the privacy flag
  that re-arms itself on upgrade, and the placeholder instructing the model to do what the repeat
  guard refuses.
- **A test written to compare against upstream's own dataclass fields** had already found the three
  missing embedding hide flags, before either of the above ran.

## Steps

- [x] `RecordContextCompaction` declares **both** hooks; sync path pinned by a test.
- [x] `_warn_about_sensitive_data` warns in both directions, naming the endpoint in the live case.
- [x] Placeholder reduced to the fact; the guidance moved to the system prompt, where it is paid
      for once rather than once per cleared result.
- [x] `_plan_command`'s `saver` required; `threads_deferred` reported; `leaver._existing_tables`
      inlined; the harness guide's `awaiting_jobs` and two `lg_`-prefixed names corrected; the
      middleware diagram gained the context editor; the `DEFERRED.md` prompt-cache row now says
      compaction moves the number it tells an operator to read.
- [x] The `timestamptz` cast's failure mode written down as a considered answer rather than silence.
- [x] ADR + ledger row; the merged ADRs are amended rather than edited.

## Review

**Re-verified against a live Phoenix after the fixes, not just re-reasoned.** Two turns — one
async, one **sync** (the path that raised `NotImplementedError` before the fix) — both land as
traces rooted at `chemclaw.turn`, each carrying an LLM span with `llm.token_count.prompt=99 /
completion=5 / total=104`, and **zero** content-bearing attributes across all 10 spans, including no
trace of the 20,000-character evidence payload compaction had cleared.

**What is uncomfortable and worth keeping in the record:** the change being reviewed was itself a
fix for "a mechanism whose description and behaviour had drifted apart", and all three findings
above are that same defect. Writing the fix does not confer immunity. The docstring that argued for
the async-only hook was *specific and confident* and wrong — which is the case for measuring rather
than reasoning, applied to one's own prose.

---

# Post-migration review (2026-08-12)

The rebuild shipped green — `make lint type test`, every validator, CI, 4155 tests — and then a
16-lane review with adversarial verification (181 agents; 81 findings raised, 72 survived) found
three separate defects that each ended in a permanently unusable conversation. All three are fixed;
`docs/decisions/D-2026-08-12-a-review-the-migration-did-not-get.md` records what a green suite could
not see and why.

**The third was found twice.** While this review ran, the deep-agents audit above reached the same
missing context bound from the opposite direction and shipped `agent/compaction.py` first — a
better fix, because it restores all three of D-025's settings rather than two and gives the policy
a counter. This branch's `_context_middleware` is deleted rather than merged beside it, and the
setting it had removed as unread (`agent_keep_last_conversation_groups`) is restored. Two reviews
with no contact finding one defect within a day is the strongest evidence here that the class is
structural rather than incidental.

**The one lesson worth carrying forward.** Sixteen of the confirmed findings are the same shape: a
property was moved to a new mechanism and *only the declaration moved*. The specialist attenuation
compared profiles while the tools were passed down already open; `awaiting_jobs` was declared as a
replacement and never written; the skills backend's "every reach path" was seven of twenty-two; the
test asserting that enumeration was a hand-written list. In each case the sentence was written by
the same change that removed the old mechanism, so there was never a moment when it was true — which
is why review found them and the suite did not.

Three follow-on habits, each cheap:

- **A checkpointed field is per-session until something resets it.** Per-turn is a claim that needs
  a mechanism, and a single-turn test cannot see the difference. `state.turn_input` is that
  mechanism; anything hand-building `{"messages": ...}` reintroduces the defect.
- **A test that drives one request cannot see a batch.** The plan gate's whole suite drove the
  middleware one call at a time, and the bypass needed two calls in one assistant message.
- **When a docstring claims a set is derived, derive it in the test.** Two of the six unfalsifiable
  tests said "enumerated from the protocol" / "proves the list is complete" over a literal.

## What merging this into `main` found (a fourth defect, in the seam between two fixes)

The branch sat behind 21 commits that had independently fixed overlapping ground, so the merge was
semantic rather than textual. Three of its resolutions are just "main's version is more complete"
— `agent/compaction.py` over `_context_middleware`, handoff-tracked attribution over the namespace
read, `calls_without_adjacent_results` restored because main gave the function a real caller after
this branch deleted it as dead. Deleting it was right on the evidence available and wrong within a
day, which is the ordinary cost of removing a thing whose next caller is being written elsewhere.

The fourth is a defect **neither branch had alone**, and it is the interesting one:

- On `main`, a specialist's tokens stream **unattributed**, and `api/runner` concatenates every
  `TokenEvent` into `answer_parts` — which is both the text a chemist reads and the durable
  transcript. So a delegated turn splices the specialist's working prose into the supervisor's
  answer, interleaved in production order. Measured by mutating the producer back: the stream is
  `[('', 'no genotoxic alert matched'), ('', 'done')]` — two agents, one voice.
- This branch's fix dropped sub-root tokens entirely, which is silent for the whole delegation and
  contradicts `main`'s new test that a specialist's output must land *inside* its handoff span.

Neither test could see the other's defect: one asserted the specialist is visible, the other that
the answer is clean, and the shipped code satisfied exactly one at a time. The resolution uses the
mechanism the contract already had — `TokenEvent` gains the same additive, defaulted `agent` field
five other events carry, the producer stamps it, and the runner concatenates only unattributed
chunks. Both properties now hold and one test asserts both directions.

**The lesson is about merges, not about tokens.** Two correct branches can compose into a defect
that is in neither, and the place to look is where each side's *test* stops: a test pins one
direction of a property, and merging two one-directional pins is not a two-directional pin.
