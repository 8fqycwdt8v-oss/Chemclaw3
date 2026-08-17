# `tests/` — correctness lens, round 1: reproduction verdicts

Lens: does it actually reproduce? Every claim below was re-derived from the source with my own
scripts (`/tmp/wh_sqlite.py`, `/tmp/wedge_check.py`, `/tmp/wedge_workflow.py`, `/tmp/ckpt_two.py`,
`/tmp/audit_verify_plan_batch.py`). The reporter's scripts (`/tmp/wedge_repro.py`,
`/tmp/ckpt_gap.py`, `/tmp/plan_batch_probe.py`) exist in this sandbox and were **not** run.

In scope: the three findings marked **high**. The three marked medium were ignored.

Working tree: both mutations (`retention.py`, `plan_gate.py`) were applied from a saved copy and
restored; `git status --porcelain -- src/` is empty at the end of this run. One incidental
observation, recorded because it is a shared-checkout hazard: during my full-suite run
`tests/test_degraded.py::test_the_subsystem_label_space_is_exactly_what_is_declared` failed once and
passes on the clean tree — that test AST-walks the live `src/` tree, so a *different* agent's
in-flight source edit is what tripped it. It is unrelated to the findings below.

---

## An ELN page whose rows share one watermark wedges the sync forever, and the one test written to catch that wedge cannot see it

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  I did not reuse `tests/warehouse_fake.py`. Its `WatermarkWarehouse` *approximates* WHERE/ORDER
  BY/LIMIT in Python, and a wedge reproduced only against an approximation is a finding about the
  approximation. Instead I wrote a `Warehouse` whose `execute()` runs the engine's emitted statement
  on a real SQL engine (in-memory sqlite), `/tmp/wh_sqlite.py`, and drove `sync_entries` through the
  real `WarehouseElnAdapter` with `connection.driver: wh_sqlite:open_sqlite`
  (`/tmp/wedge_check.py`). Same four rows both ways; the only variable is whether the three
  amendments are strictly increasing or tied.

  The statement sqlite actually received (printed by the harness):

  ```
  SELECT * FROM V_REACTION WHERE COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ?
  ORDER BY COALESCE(LAST_MODIFIED_TS, CREATED_TS) ASC LIMIT ?
  ```

  Strictly increasing amendments (the shipped test's fixture) — drains:

  ```
  chunk 0: since=2026-01-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:01:00+00:00
  chunk 1: since=2026-06-01T00:01:00+00:00 ingested=['OLD-2','OLD-3'] next=2026-06-01T00:02:00+00:00
  chunk 2: since=2026-06-01T00:02:00+00:00 ingested=['NEW-1','OLD-3'] next=2026-06-02T00:00:00+00:00
  SEEN=['NEW-1','OLD-1','OLD-2','OLD-3']  NEW-1 reachable: True
  ```

  Three amendments at one timestamp — wedges, identically to the report:

  ```
  chunk 0: since=2026-01-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
  chunk 1: since=2026-06-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
  ... chunks 2-5 identical ...
  SEEN=['OLD-1','OLD-2']  NEW-1 reachable: False
  ```

  I then checked the part that decides whether anything *reports* it, by driving the production
  wrapper `chemclaw.durable.eln_sync._BoundedIngest` and the workflow's own loop condition
  (`/tmp/wedge_workflow.py`), four scheduled runs:

  ```
  run 0: since=2026-01-01 ingested=['OLD-1','OLD-2'] next=2026-06-01 has_more=False rejected=0
  run 1: since=2026-06-01 ingested=['OLD-1','OLD-2'] next=2026-06-01 has_more=False rejected=0
  run 2: since=2026-06-01 ingested=['OLD-1','OLD-2'] next=2026-06-01 has_more=False rejected=0
  run 3: since=2026-06-01 ingested=['OLD-1','OLD-2'] next=2026-06-01 has_more=False rejected=0
  ```

  `has_more` is `False` on every run, so the `next_cursor <= source_since` guard at
  `durable/eln_sync.py:254` is never reached — exactly as claimed. I also re-ran that with
  `CHEMCLAW_ELN_SYNC_BATCH_SIZE=2` so the adapter's "fetch_limit below batch size" warning does not
  fire at all: byte-identical output, so that warning is not what is producing the wedge.

- **Why**

  The mechanism is in the source, not in a fixture. `sql.entry_statement` (`sql.py:73-81`) filters
  `watermark >= since`, orders ascending, takes `LIMIT`; `sync.py:183` sets `cursor = max(cursor,
  window)`. When the number of rows sharing the page's largest watermark is ≥ `fetch_limit`, the
  next fetch's `since` equals that watermark and `>=` returns the same page forever. Everything
  ordered after the tie is permanently unreachable. `_BoundedIngest.truncated` cannot see it either,
  because it counts entries with `created_at > since` and an amended row's `created_at` is old — so
  `has_more=False` and the workflow's wedge guard is dead in exactly this case.

  The shipped test is blind for the reason stated: `test_warehouse_adapter.py:481-483` stamps the
  three amendments at `amended`, `+1 min`, `+2 min`, so its cursor always moves. I read it; the
  finding quotes it correctly.

  Trigger plausibility, which is the only part I would qualify: `fetch_limit` defaults to **500**
  (`warehouse/binding.py:131`), so the tie must span ≥ 500 rows — one bulk `UPDATE … SET
  LAST_MODIFIED_TS = CURRENT_TIMESTAMP` in a warehouse, which stamps one statement timestamp on
  every touched row, does exactly that. And `eln-snowflake` is not in the default
  `data_sources` (`graph,eln-json`), so this bites a site that has enabled the warehouse ELN. Both
  qualifications narrow *when*, not *whether*: when it fires the loss is silent and permanent, and
  the log reads `ingested=2 rejected=0`. High stands.

  One thing the reporter did not say that makes it slightly worse: even without a tie spanning the
  whole page, the sync re-proposes the boundary rows on every run — `awaiting_merge` warns about
  `OLD-1, OLD-2` on each of the six chunks above, so an operator watching the intended
  wedge-detection signal sees a *steady* warning about the wrong entries while the reachable ones
  vanish.

---

## The retention sweep's `HAVING max(...)` — the only thing standing between a live conversation and deletion — is not asserted by any test

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Baseline, real Postgres up (`docker ps`: `infra-postgres-1 Up (healthy)`):

  ```
  $ uv run pytest tests/test_retention.py -q
  13 passed, 1 skipped in 1.81s
  ```

  I made the mutation myself (saved copy first, restored after) — `max` → `min` in
  `_EXPIRED_THREADS`, `retention.py:140`:

  ```
  $ uv run pytest tests/test_retention.py -q
  13 passed, 1 skipped in 1.15s
  ```

  Then the part that decides whether the mutant is harmless. My own script `/tmp/ckpt_two.py`
  creates a scratch schema `audit_ckpt_repro`, runs LangGraph's own `base.MIGRATIONS[1:4]`, seeds
  **one live thread with two checkpoints** (first 90 days old, newest 1 day old) plus a blob per
  checkpoint, and calls the real `retention._prune_checkpoints(conn, 30)`:

  ```
  min (mutant):  before {'checkpoints': 2, 'checkpoint_blobs': 2}
                 deleted {'checkpoints': 2, 'checkpoint_blobs': 2, 'checkpoint_writes': 0}
                 after  {'checkpoints': 0, 'checkpoint_blobs': 0}

  max (shipped): before {'checkpoints': 2, 'checkpoint_blobs': 2}
                 deleted {'checkpoints': 0, 'checkpoint_blobs': 0, 'checkpoint_writes': 0}
                 after  {'checkpoints': 2, 'checkpoint_blobs': 2}
  ```

  Source restored (`git diff --stat src/chemclaw/durable/retention.py` empty), scratch schema
  dropped.

- **Why**

  Both halves reproduce on my own scaffolding: the file cannot distinguish the two statements, and
  the statements are not equivalent. The cause is what the report says and I confirmed by reading
  `_seed_thread` (`test_retention.py:456-488`): one `INSERT INTO checkpoints … 'ckpt-1'` per thread,
  so `max(ts) == min(ts)` for every row the fixture creates, and a `HAVING` over a *group* is being
  tested with groups of size one. `grep -rln "prune_expired_rows\|_prune_checkpoints\|
  retention_checkpoints_days" tests/` returns only `tests/test_retention.py`, so there is no other
  file that could catch it.

  What keeps this at high rather than medium is the class of change it fails to guard, not the
  specific `min` mutant: the disposal unit is a whole thread across three tables in one transaction
  with no foreign key and no second pass, so any future edit to that predicate — including a
  perfectly reasonable "prune old checkpoints within a thread" refactor — deletes live conversation
  state irreversibly and the suite stays green. The docstring at `test_retention.py:512-516` claims
  the live thread is the counter-example for "a `HAVING max(...)` that was wrong in the other
  direction"; it is not, and the fix is two extra rows in one fixture.

  Noted honestly: no shipped code is wrong today. This is a verified coverage hole, and the
  severity is the severity of what it leaves unguarded.

---

## `plan_gate.rewrites_the_plan_in_this_batch` — the DARK-1 batch guard — has no test at all

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Static, my own greps:

  ```
  $ grep -rn "rewrites_the_plan_in_this_batch" tests/ src/
  src/chemclaw/agent/plan_gate.py:257:def rewrites_the_plan_in_this_batch(request: Any) -> bool:
  src/chemclaw/agent/plan_gate.py:351:    if rewrites_the_plan_in_this_batch(request):
  ```

  (The report cites the call site as `:349`; it is `:351`. The definition line `:257` is right.)

  Is the branch live, or dead code that only a hand-built request can reach? I built the request the
  way `ToolNode` does — `langgraph/prebuilt/tool_node.py:1035` and `:1182` construct
  `ToolCallRequest(..., state=tool_runtime.state, ...)`, i.e. the graph state, which carries
  `messages` — and drove the real middleware (`/tmp/audit_verify_plan_batch.py`), with the plan
  approved in the real `InMemoryPlanApprovalStore`:

  ```
  gated call alone, plan approved      : TOOL RAN
  gated call batched with write_todos  : REFUSED (propose_knowledge_note changes stored data or ...)
  ```

  Then the coverage question, with the guard neutered by my own edit (`return False` as the first
  statement of the function; verified live by re-running the probe above, which then printed
  `TOOL RAN` for both cases):

  ```
  $ uv run pytest tests/test_plan_gate.py tests/test_scratchpad.py tests/test_authz.py \
      tests/test_tool_authz.py tests/test_middleware_order.py tests/test_audit.py \
      tests/test_langgraph_agent.py tests/test_approvals.py tests/test_langgraph_stream.py \
      tests/test_plan_state.py tests/test_profiles.py tests/test_profile_autonomy_validation.py \
      tests/test_upstream_surface.py tests/test_jobs_api.py tests/test_publish.py \
      tests/test_turn_signals.py -q -p no:randomly
  244 passed in 22.25s

  $ uv run pytest tests/test_dialogue.py tests/test_service.py tests/test_m12_probes.py \
      tests/test_cli.py tests/test_turn_cancellation.py tests/test_hot_path_caching.py \
      tests/test_template_agent_step.py tests/test_disconnect_teardown.py -q -p no:randomly
  127 passed in 27.54s
  ```

  I also started a full-suite run under the mutation; it reached **872 passed** before stopping at
  the unrelated `test_degraded` failure described in the header (that file AST-walks the live `src/`
  tree and another agent was mid-edit). Nothing in those 872 was sensitive to the guard either.
  Source restored afterwards.

- **Why**

  Reproduces on my own scaffolding in both directions: the branch fires when a `write_todos` call
  shares the assistant message, and 371 tests across every file that touches the gate, approvals,
  autonomy, profiles, the CLI and the graph do not notice when it is deleted. The structural reason
  the report gives is correct and I verified it by reading: `tests/middleware.py:44` builds the
  request with `state={}`, and `tests/test_plan_gate.py:105` replaces `state` with `{"todos": …}`
  only — so `(request.state or {}).get("messages")` is `[]` on every call the suite makes and the
  function returns `False` at line 271 before reaching any of its logic. `tests/test_langgraph_agent.py:322`
  is the one place a *batched* assistant message is driven through a compiled graph, and its two
  calls are both `ask_clarifying_question`, so it does not reach this either.

  The one thing I would add: this is not just an untested helper, it is the only place in the chain
  that can answer the question at all. `_plan_behind` reads `request.state["todos"]`, which
  `ToolNode` snapshots *before* the batch, so removing the guard does not merely lose a check — it
  makes the pre-batch plan the authority for a call issued beside the plan's replacement, and the
  approval is then left unspent because `consume_turn_approval` hashes the new plan. Two three-line
  cases on the existing `_call` helper close it.

  Reachability qualification, stated for completeness: the gate is attached only when
  `gate_applies` holds — `harness_enabled` (default `False`) and `harness_autonomy == "plan_only"`
  (the default *where* the harness is on). So this guards the deployments that switch the harness on,
  which is the configuration the whole plan-gate exists for.
