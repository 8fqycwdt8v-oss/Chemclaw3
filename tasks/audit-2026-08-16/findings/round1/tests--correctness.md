# `tests/` — correctness lens, round 1

Slice: the test suite itself. Lens: code that produces a wrong answer, crashes, loses work, or
silently drops data — and, in this slice, the places where the suite cannot see any of that
happening.

Method: every finding below is either a script that was run and whose output is quoted, or a
mutation applied to `src/` with the affected test files re-run and the pass count recorded.
Postgres was started (`sudo dockerd`, `make up`) before any of it, so the Postgres-backed tests
genuinely executed rather than skipping — `pytest tests/test_retention.py` reports
`13 passed, 1 skipped`, not 14 skipped.

---

## An ELN page whose rows share one watermark wedges the sync forever, and the one test written to catch that wedge cannot see it

- **Severity**: high
- **Location**: `tests/test_warehouse_adapter.py:462` (`test_a_page_of_amended_rows_does_not_stall_the_sync_forever`); `tests/warehouse_fake.py:79` (`WatermarkWarehouse`). The code it fails to hold: `src/chemclaw/ingest/eln/warehouse/sql.py:73` (`>=` predicate) with `src/chemclaw/ingest/eln/sync.py` (`cursor = max(cursor, window)`).
- **Trigger**: an entry relation where more rows carry the *same* `COALESCE(modified, created)` value than `entry.fetch_limit`. A bulk amendment (a script that touches N runs, a migration that stamps `LAST_MODIFIED_TS` in one statement) produces exactly this.
- **Consequence**: `entry_statement` filters `watermark >= since`, orders ascending and takes `LIMIT`. The sync then sets `next_cursor` to the largest watermark it saw — which is that same value — so the next fetch returns the *identical* page. Every entry ordered after the tie is never ingested again, on this run or any future one. The workflow's own wedge guard (`durable/eln_sync.py`, `next_cursor <= source_since`) does not stop it: the first run's cursor *does* advance (Jan → Jun), `has_more` is false, and the run reports `ingested=2 rejected=0`.
- **Evidence**: the shipped test uses three *strictly increasing* amendment stamps (`amended`, `+1 min`, `+2 min`), so its cursor always moves and its `NEW-1` is always reached. `WatermarkWarehouse` reproduces the real `>=`/ORDER BY/LIMIT semantics faithfully, so the equal-watermark case is reproducible against it — the fixture simply never asks. Rerunning the shipped scenario with the three amendments at one timestamp and `eln_sync_batch_size` lowered to match `fetch_limit` (so the "fetch_limit below batch size" warning cannot be blamed):

  ```
  chunk 0: cursor=2026-01-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
  chunk 1: cursor=2026-06-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
  chunk 2: cursor=2026-06-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
  ... (chunks 3-5 identical)
  SEEN: ['OLD-1', 'OLD-2']
  NEW-1 reachable: False
  ```

  `OLD-3` and `NEW-1` are unreachable permanently. Script: `/tmp/wedge_repro.py`.
- **Fix**: add the tied-watermark case to `test_a_page_of_amended_rows_does_not_stall_the_sync_forever` (three rows at one timestamp, `fetch_limit=2`), which will fail. The production fix it then demands is a tiebreaker in the page: order and page on `(watermark, key)` and bind the last row's `(watermark, key)` as the cursor, or — cheaper and enough — have `sync_entries` refuse to return a `next_cursor` equal to `since` when the batch came back full, so the workflow's existing wedge guard fires on the first run instead of the second.

---

## The retention sweep's `HAVING max(...)` — the only thing standing between a live conversation and deletion — is not asserted by any test

- **Severity**: high
- **Location**: `tests/test_retention.py:457` (`_seed_thread`), `:507` (`test_an_expired_thread_leaves_none_of_its_three_tables_behind`). Code: `src/chemclaw/durable/retention.py:138` (`_EXPIRED_THREADS`).
- **Trigger**: any thread with more than one checkpoint. The suite seeds exactly one checkpoint per thread (`_seed_thread(thread_id, age_days)` inserts a single `ckpt-1`), so `max(ts) == min(ts) == ts` for every row it creates.
- **Consequence**: with one checkpoint per thread, `HAVING max(...) < cutoff` and `HAVING min(...) < cutoff` are indistinguishable. `min` is the plausible wrong version — it is what "prune old checkpoints" reads as — and it deletes a *live* thread whole (all three tables, one transaction) as soon as its **first** checkpoint ages past the window. A multi-week conversation is destroyed mid-flight; `AsyncPostgresSaver` then resumes it from nothing, and there is no foreign key or later pass that would notice.
- **Evidence**: substituting `min` for `max` in `_EXPIRED_THREADS` leaves the file green —

  ```
  $ sed -i 's/HAVING max((checkpoint/HAVING min((checkpoint/' src/chemclaw/durable/retention.py
  $ uv run pytest tests/test_retention.py -q
  13 passed, 1 skipped in 1.16s
  ```

  and the mutant is not harmless. Seeding one thread with two checkpoints (90 days and 1 day old) against a real Postgres and running the sweep under both statements:

  ```
  before: {'checkpoints': 2, 'checkpoint_blobs': 2}
  HAVING max (shipped)  -> deleted={'checkpoints': 0, 'checkpoint_blobs': 0} remaining={'checkpoints': 2, 'checkpoint_blobs': 2}
  HAVING min (mutant)   -> deleted={'checkpoints': 2, 'checkpoint_blobs': 2} remaining={'checkpoints': 0, 'checkpoint_blobs': 0}
  ```

  Script: `/tmp/ckpt_gap.py`. The test's own docstring claims the live thread is the counter-example that catches "a `HAVING max(...)` that was wrong in the other direction"; a single-checkpoint live thread cannot be that counter-example, because the property under test is a *grouping* over rows the fixture never creates.
- **Fix**: give the live thread two checkpoints in `_seed_thread` (or add a third fixture thread whose oldest checkpoint is past the window and whose newest is not) and assert it survives. That single change kills the `min` mutant.

---

## `plan_gate.rewrites_the_plan_in_this_batch` — the DARK-1 batch guard — has no test at all

- **Severity**: high
- **Location**: `src/chemclaw/agent/plan_gate.py:257` (`rewrites_the_plan_in_this_batch`), called from `enforce_plan_approval` at `:349`. No test references it: `grep -rn "rewrites_the_plan_in_this_batch" tests/` returns nothing, and `write_todos` appears in `tests/` only in unrelated contexts (an audit-noise test, a scratchpad gate test, the upstream-surface pin).
- **Trigger**: delete the guard (`return False` at the top of the function) and run every test file that mentions the plan gate, approvals, autonomy or profiles.
- **Consequence**: the guard is the fix for a sequence the module docstring says was *reproduced live* — turn 2 emits `write_todos(plan B)` and a gated write in one assistant message, `ToolNode` builds every call's runtime from one pre-batch state snapshot, so the gate sees plan A, the approval for plan A stands, and the write executes under an approval a human gave for a different plan. Nothing in the suite would notice its removal, and the failure in production is silent: the write succeeds and the approval is then left unspent, because `consume_turn_approval` hashes plan B and finds no decision.
- **Evidence**: with `rewrites_the_plan_in_this_batch` neutered to `return False`,

  ```
  $ uv run pytest tests/test_plan_gate.py tests/test_scratchpad.py tests/test_authz.py \
      tests/test_tool_authz.py tests/test_middleware_order.py tests/test_audit.py \
      tests/test_langgraph_agent.py -q
  117 passed

  $ uv run pytest tests/test_approvals.py tests/test_cli.py tests/test_degraded.py \
      tests/test_dialogue.py tests/test_disconnect_teardown.py tests/test_hot_path_caching.py \
      tests/test_langgraph_stream.py tests/test_m12_probes.py tests/test_plan_state.py \
      tests/test_profile_autonomy_validation.py tests/test_profiles.py tests/test_publish.py \
      tests/test_service.py tests/test_template_agent_step.py tests/test_turn_cancellation.py \
      tests/test_turn_signals.py -q
  216 passed
  ```

  333 tests, none of them sensitive to the guard. The branch is live, not dead — driving the gate with a `state["messages"]` carrying the batch shows it firing (`/tmp/plan_batch_probe.py`):

  ```
  plain call, approved plan A      : TOOL RAN
  call batched with write_todos    : REFUSED (propose_knowledge_note changes stored data or starts work, a…)
  ```

  The reason the suite is blind is structural: `tests/middleware.py::tool_request` builds a `ToolCallRequest` with `state={}`, and `tests/test_plan_gate.py::_call` then overwrites `state` with `{"todos": [...]}` only — never `messages`. So `rewrites_the_plan_in_this_batch` reads an empty message list on every call the suite makes and returns `False` before reaching any of its logic.
- **Fix**: add two cases to `tests/test_plan_gate.py`, both reusing `_call`: put a `messages=[AIMessage(tool_calls=[...])]` entry in the state, once with a `write_todos` call beside the gated one (must refuse even with the approval recorded) and once without (must run). Both are three lines on top of the existing helper.

---

## The Temporal `thread` timeout method is silently discarded for any Temporal test that carries its own `@pytest.mark.timeout(...)`

- **Severity**: medium
- **Location**: `tests/conftest.py:216` (`pytest_collection_modifyitems`, the `add_marker` at `:244`) and `tests/conftest.py:213` (`_apply_timeout_scale`).
- **Trigger**: a module that defines/imports `start_env_or_skip` *and* has a test carrying its own `@pytest.mark.timeout(N)`. `tests/test_bo_knowledge.py:200` records that one such marker existed and was removed; re-adding one is the natural response to a slow Temporal test.
- **Consequence**: the hook does `item.add_marker(pytest.mark.timeout(method="thread"))`, which **appends**. pytest-timeout reads timeout *and* method off the single closest marker, and the function's own decorator marker is closer, so the item runs under the default `signal` method — which the conftest docstring itself says "silently does nothing" for a test blocked in `temporalio`'s Rust core. That is precisely the 28-minute silent hang the mechanism was written to end. `_apply_timeout_scale` does not rescue it: it copies `**kwargs` off the *closest* marker, which is the plain one, so the scaled replacement also lacks `method="thread"` (and at the default scale of 1.0 it returns before doing anything at all).
- **Evidence**: a throwaway suite importing the real hook, module defining `start_env_or_skip`, one test with `@pytest.mark.timeout(1)` and one without:

  ```
  test_has_its_own_marker      -> "Failed: Timeout (>1.0s) from pytest-timeout"   (signal: ordinary failure, session continues)
  test_no_marker_of_its_own    -> "+++ Timeout +++ / ~~~ Stack of MainThread ~~~"  (thread: stack dump, os._exit)
  ```

  and under the scale, still signal:

  ```
  $ PYTEST_TIMEOUT_SCALE=2 pytest test_scaled.py     # @pytest.mark.timeout(1), sleeps 6
  E  Failed: Timeout (>2.0s) from pytest-timeout      # no stack dump -> signal method
  ```

  `tests/test_suite_timeouts.py::test_scaling_a_marker_keeps_the_timeout_method_it_carried` does not catch this: it writes `method="thread"` into the marker *by hand*, so it only proves the scale copies kwargs it was given, never that the hook's own marker reaches an item that already has one.
- **Fix**: in `pytest_collection_modifyitems`, merge instead of append — read the closest `timeout` marker, and re-add it prepended with `method="thread"` folded into its kwargs (the same shape `_apply_timeout_scale` already uses). Then extend `test_scaling_a_marker_keeps_the_timeout_method_it_carried` with a module that defines `start_env_or_skip` and a test carrying a bare `@pytest.mark.timeout(1)`, asserting the session ends with no parseable outcome.

---

## `test_run_turn_reports_failure_as_error_event` passes identically against a model that raises nothing

- **Severity**: medium
- **Location**: `tests/test_service_events.py:28` (`_FakeAgent.stream`) and `:63` (`test_run_turn_reports_failure_as_error_event`).
- **Trigger**: run the test's assertions against the base `_FakeAgent` instead of `_BoomAgent`.
- **Consequence**: the test claims to prove that a turn whose model call raises yields exactly one user-safe `ErrorEvent` with the raw exception text stripped (SEC-1). It cannot fail for that reason, because its own base fake already produces exactly one `ErrorEvent` — `_FakeAgent.stream` yields `FakeUpdate` objects into `ScriptedTurn`'s LangGraph rendering, `fakes_turn._chunk` wraps a non-`str`/non-`Chunk` piece as `Chunk(text=<FakeUpdate>)`, and `AIMessageChunk(content=<FakeUpdate>)` fails pydantic validation inside the graph. The runner swallows that into the same generic message. So a real regression in `run_turn`'s error path — one that let the exception text through, or emitted a token before the error — would have to survive a fake that never reaches the model at all.
- **Evidence**:

  ```
  $ uv run python /tmp/vacuity_probe.py
  _BoomAgent (raises, as shipped)   : types=['error']  test-body-passes=True
  _FakeAgent (raises nothing)       : types=['error']  test-body-passes=True
  ```

  and driving the base fake directly shows why:

  ```
  content.list[union[str,dict[any,any]]]
    Input should be a valid list [type=list_type, input_value=<tests.fakes.FakeUpdate object …>]
  During task with name 'model' …
  events: [('error', 'The turn could not be completed due to an internal error (se')]
  ```

  The module docstring's claim that this file proves `run_turn` "translates a scripted stream of model updates into tokens + a tool-call trace + a final answer" is not held by either of its two tests: `_FakeAgent.stream` is never executed by any of them, and would raise if it were.
- **Fix**: make `_FakeAgent.stream` yield what `ScriptedTurn` renders — `Chunk`/`str` pieces (`fakes_turn.Piece`), not `FakeUpdate` — so the happy path it documents actually runs; then assert on it. Keep `_BoomAgent`, and add the negative that makes the error test meaningful: assert the non-raising agent yields `token`/`answer` and *no* `error`.

---

## Roughly 47 assertions across three files exercise `ToolCallTrace.feed`, which no production caller reaches

- **Severity**: medium
- **Location**: `tests/fakes.py:81` (`fed`) and `:41` (`FakeUpdate`); call sites in `tests/test_runner.py` (32), `tests/test_review_2026_08_05.py` (8), `tests/test_tool_results.py` (7). Code: `src/chemclaw/api/runner_trace.py:124` (`feed`), `:194` (`flush`), `:247` (`_take`).
- **Trigger**: `grep -rn "\.feed(" src/chemclaw/` — no match. The one production driver, `src/chemclaw/api/graph_stream.py`, calls `trace.issued(...)` at `:275` and `await trace.returned(...)` at `:302`; `issued`'s own docstring says why ("LangGraph's `updates` stream hands over a finished `tool_calls` list, so the graph driver has nothing to reassemble and calls this directly").
- **Consequence**: two things. First, the most intricate part of the module — the fragment buffer, the `_arguments_complete` JSON-balance check, the empty-fragment-is-not-the-end rule, `_take`, the `contents` walk that `FakeUpdate` exists to feed — is reachable only from tests. Its ~47 assertions are evidence about the removed Microsoft Agent Framework's streamed-update shape, not about anything this deployment runs, and they consume the maintenance budget of tests that do. Second, `_fragments` is filled only by `feed`, so `src/chemclaw/api/runner.py:324`'s `for call in tool_trace.flush(): yield call` — commented as covering "a tool call whose arguments finished on the *final* update" — iterates a permanently empty dict on every turn. `tests/fakes.py`'s own docstring says `fed` exists because "the forty call sites that read it are ordinary `def` tests"; forty call sites is the size of the problem, not the justification.
- **Evidence**: static — `.feed(` has zero occurrences under `src/`. Dynamic — raising `AssertionError` on entry to `feed`, and asserting `_fragments` empty on entry to `flush`, then running the production-path suites (`test_service.py`, `test_service_events.py`, `test_langgraph_stream.py`, `test_turn_cancellation.py`, `test_turn_signals.py`, `test_dialogue.py`, `test_m12_probes.py`, `test_disconnect_teardown.py`, `test_session_events.py`, `test_evidence_fanout.py`):

  ```
  $ uv run pytest tests/test_service.py tests/test_service_events.py tests/test_langgraph_stream.py \
      tests/test_turn_cancellation.py tests/test_turn_signals.py tests/test_dialogue.py \
      tests/test_m12_probes.py tests/test_disconnect_teardown.py tests/test_session_events.py \
      tests/test_evidence_fanout.py -q
  150 passed in 36.87s
  ```

  Neither trap fired: the whole front-door turn path — SSE streaming, cancellation, mid-turn
  signals, the M12 probes — runs 150 tests without `feed` being entered once, and `flush` is
  always called on an empty buffer.
- **Fix**: decide whether the MAF-era reassembly is still wanted. If not, delete `feed`/`_take`/`_arguments_complete`/`_result_text`/`_names`/`_fragments`, `flush` and its call in `runner.py:324`, and `FakeUpdate`/`fed` with the ~47 assertions that drive them — the coverage that matters (`issued`, `returned`, the argument budget, `outputs`) stays. If it is wanted for a future non-LangGraph provider, it needs a production caller and a test that reaches it *through* one; a private method exercised only by a hand-built double is the shape `tests/test_upstream_surface.py`'s own docstring warns about ("a behaviour assertion that runs in isolation is exactly the kind that passes while the thing it describes is disconnected").

---

## What I checked and found sound

- **`tests/test_calc_thermo.py`** — the RRHO arithmetic is genuinely validated, not asserted-by-comment. Run against the recorded Hessians: water 45.054 vs NIST 45.10, CO2 51.202 vs 51.06, H2 31.309 vs 31.23 (tolerance `abs=0.3`), with 3N-6 / 3N-5 mode counts correct. The only prose slip is the header's "agreement to a few hundredths", which is 0.14 for CO2.
- **`api/runner_usage.graph_usage_tokens`** — the cache-token subtraction is right for both providers. Checked against the installed `langchain_anthropic._create_usage_metadata`, which explicitly adds `cache_read` and the per-TTL creation tokens into `input_tokens`; `_cache_creation`'s `specific or flat` mirrors upstream's own rule, so it cannot disagree with the total it subtracts from. `assert usage.input == 21347 - 15136 - 6208 == 3` is a chained comparison and both halves hold.
- **`retrieval.hybrid.reciprocal_rank_fusion`** — 1-based ranks, per-list dedupe on best position, deterministic tie-break; no off-by-one.
- **`agent/message_pairing.droppable_rows`** — union-find over call ids, contracts rather than expands, `None` (unreadable row) correctly poisons the whole session rather than being read as "pairing-free".
- **`tests/pg.py` / `conftest.isolated_postgres_schema`** — the pid-suffixed schema and the `search_path` redirect do isolate; the `public`-shadowing hazard for the LangGraph checkpoint tables is already handled by the schema-qualified drop in `test_retention.py:596` (the `_run` at `:620`) and its skip.
- **`tests/calc_server_fake.py`** — the three key properties it claims to reproduce (Fukui key without the mode, `xtb.opt` key without the caller, `predict_logd` with no key) are consistent with the client code that depends on them, and `SiteReactivityResult.ranked_for`'s cache-hit re-rank is covered by `test_calc_tools.py` with f_minus/f_plus deliberately anti-correlated so a mis-served ranking cannot pass by coincidence.
