# `tests/` — correctness, round 1: reachability verdicts

Lens: is the trigger reachable from a real caller in a real deployment, and is the consequence what
is claimed? In scope: the three findings marked **high**. The three marked medium are ignored.

**Working-tree hazard hit while doing this.** Two source files in the shared checkout carried live
mutations from other agents' experiments when I started, and both are mutations belonging to the
findings below:

```
$ git diff HEAD --stat -- src/
 src/chemclaw/agent/plan_gate.py  |  1 +      <- "+    return False  # AUDIT MUTATION"
 src/chemclaw/retrieval/fanout.py | 49 ++++--
$ grep -n "HAVING m" src/chemclaw/durable/retention.py
140:    "HAVING min((checkpoint->>'ts')::timestamptz) < now() - ...
```

I did **not** revert them (another session may be mid-measurement), but every measurement below is
therefore taken either against `…/scratchpad/pristine/src` or by forcing the constant/function under
test in-process, never by trusting the file on disk. Anyone reading `retention.py` or `plan_gate.py`
in this checkout right now is reading a mutant, not `HEAD`.

---

## An ELN page whose rows share one watermark wedges the sync forever, and the one test written to catch that wedge cannot see it

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the finding understates the consequence; see the last paragraph)
- **What I did**:

  Re-ran the shipped scenario of `test_a_page_of_amended_rows_does_not_stall_the_sync_forever`
  twice through the real `sync_entries` + `WarehouseElnAdapter` + `WatermarkWarehouse`, changing
  only the spacing of the three amendment stamps (`/tmp/tie_wedge.py`):

  ```
  shipped scenario (amended, +1 min, +2 min):
    chunk 0: cursor=2026-01-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:01:00+00:00
    chunk 1: cursor=2026-06-01T00:01:00+00:00 ingested=['OLD-2','OLD-3'] next=2026-06-01T00:02:00+00:00
    chunk 2: cursor=2026-06-01T00:02:00+00:00 ingested=['OLD-3','NEW-1'] next=2026-06-02T00:00:00+00:00
    SEEN=['NEW-1','OLD-1','OLD-2','OLD-3']  NEW-1 reachable: True

  tied scenario (three amendments at one timestamp, fetch_limit=2):
    chunk 0: cursor=2026-01-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
    chunk 1..5: cursor=2026-06-01T00:00:00+00:00 ingested=['OLD-1','OLD-2'] next=2026-06-01T00:00:00+00:00
    SEEN=['OLD-1','OLD-2']  NEW-1 reachable: False
  ```

  Then checked the two things the finding claims stand in the way of detection:

  ```
  # the workflow's wedge guard is never even evaluated
  bounded fetch ids: ['OLD-1','OLD-2'] truncated(has_more) = False
  ```

  and what a *steady-state* wedged run logs once a human has merged the two notes it keeps
  proposing (`/tmp/aud/tie_merged.py`, `_merged_note_bodies` patched to the merged bodies):

  ```
  run1: INFO eln sync: ingested=2 rejected=0 skipped_existing=0 awaiting_merge=2
        WARNING eln sync proposed 2 entry/entries whose notes are still unmerged ...
  run2 (cursor wedged at 2026-06-01T00:00:00+00:00):
        INFO eln sync: ingested=0 rejected=0 skipped_existing=2 awaiting_merge=0
  ```

- **Why**: the mechanism, the trigger and the blindness all hold.

  *Trigger, traced outward.* `entry_statement` (`sql.py:73-81`) is `watermark >= %s ORDER BY
  watermark ASC LIMIT %s`, `fetch_new_entries` binds `entry.fetch_limit`, and `sync_entries:183`
  sets `cursor = max(cursor, window)`. If ≥ `fetch_limit` rows share one `COALESCE(modified,
  created)` value and that value is the smallest at or after the cursor, the page is filled entirely
  by that tie group, the cursor becomes that same value, and the next fetch is byte-identical. No
  validator, pydantic model or manifest schema prevents ties — `EntryBinding` constrains only
  identifiers and `1 ≤ fetch_limit ≤ 5000` (default 500; the shipped `eln-snowflake` manifest sets
  500). The outermost entry point is an operator: enable a warehouse ingest source
  (`CHEMCLAW_DATA_SOURCES`, which does *not* include it by default — `sources.py:45` is
  `"graph,eln-json"`), then have anyone run one bulk `UPDATE … SET LAST_MODIFIED_TS =
  CURRENT_TIMESTAMP()` over >500 reaction rows. Snowflake's `CURRENT_TIMESTAMP` is fixed for the
  duration of a statement, so a single bulk amendment produces exactly one tie value across every
  row it touched. That is a plausible DBA action, not a contrived one.

  The fake's stable `sorted` does not flatter the case: with real SQL's arbitrary tie order the
  members of the tie group returned may vary run to run, but every row whose watermark is *greater*
  than the tie value is still unreachable forever, because the `LIMIT` is exhausted by tie-group
  rows on every fetch.

  *Consequence.* Verified rather than paraphrased. `_BoundedIngest.truncated` is computed on
  `created_at > since`, and in a bulk-amendment the created stamps are old, so `has_more` is
  `False`; the workflow breaks out of the chunk loop *before* reaching `if chunk.summary.next_cursor
  <= source_since` (`durable/eln_sync.py:252-254`), so the wedge guard the code advertises is never
  evaluated. The finding is right that the run reports success.

  *Worse than stated.* The finding's steady-state log is `ingested=2 rejected=0` with the
  `awaiting_merge` WARNING still firing. That is only the transient. Once a human merges the two
  notes the wedge keeps re-proposing, the byte-identical body check (`sync.py:198-207`) short-
  circuits them into `skipped_existing`, and every subsequent hourly run logs
  `ingested=0 rejected=0 skipped_existing=2 awaiting_merge=0` at INFO — literally indistinguishable
  from a source with nothing new. There is then no WARNING anywhere, no `has_more`, no cursor
  regression and no rejected row: the ELN corpus simply stops growing, permanently, with a clean
  log. That is silent loss of every experiment recorded after the amendment, which is what keeps
  this at high rather than medium.

---

## The retention sweep's `HAVING max(...)` — the only thing standing between a live conversation and deletion — is not asserted by any test

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**: every factual claim reproduces. Because the file on disk was already mutated by
  another session, I forced the statement in-process instead of editing source (`/tmp/aud/forcemin.py`,
  a pytest plugin that rewrites `retention._EXPIRED_THREADS` before collection — the constant is
  read by global lookup inside `_prune_checkpoints`, so this is exact):

  ```
  === forced max ===  13 passed, 1 skipped in 1.64s
  === forced min ===  13 passed, 1 skipped in 1.98s
  ```

  and the same for `test_retention.py tests/test_schedules.py …`. Then the two-checkpoint case
  against real Postgres in a throwaway schema (`/tmp/aud/two_ckpt.py`, one thread, checkpoints at 90
  and 1 days, window 30):

  ```
  HAVING max: before={'checkpoints': 2, 'checkpoint_blobs': 2} deleted={...: 0} remaining={...: 2}
  HAVING min: before={'checkpoints': 2, 'checkpoint_blobs': 2} deleted={'checkpoints': 2, 'checkpoint_blobs': 2} remaining={...: 0}
  ```

  I also confirmed `_seed_thread` (`tests/test_retention.py:457`) inserts exactly one `ckpt-1` per
  thread, so `max == min` for every row the file creates, and that nothing else in `tests/` asserts
  the statement text (`grep -rn "HAVING" tests/` matches only a docstring at `:516`).

- **Why**: the mechanism is exactly as described and the mutant is exactly as harmful — but the
  claimed consequence has no reachable trigger. `HEAD` ships `max`, and I verified `max` leaves the
  live two-checkpoint thread intact. Nothing an operator configures, no HTTP request, no manifest
  and no retention setting can turn `max` into `min`; the trigger is a future source edit. So the
  title's "the only thing standing between a live conversation and deletion" describes a
  regression-detection gap, not a defect: no live conversation is at risk today. Judged as what it
  is — a surviving mutant on a destructive code path whose fix is one extra `INSERT` in the fixture
  — it is a solid medium, at the top of that band because the damage the missing assertion would let
  through is unrecoverable and unlogged (the sweep reports the deletion as success). It is not the
  same class of thing as the ELN finding above, which is a defect in shipped behaviour.

  One thing the reporter missed, in their favour: `_prune_checkpoints` deletes all three tables in
  one transaction keyed only on `thread_id`, so a wrong `HAVING` destroys the thread's blobs too —
  the `checkpoint_blobs` column of my run shows it — and `AsyncPostgresSaver` has no foreign key or
  later pass that could notice. Their claim on that point is correct.

---

## `plan_gate.rewrites_the_plan_in_this_batch` — the DARK-1 batch guard — has no test at all

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  Coverage claim — `grep -rn "rewrites_the_plan_in_this_batch" tests/ --include=*.py` returns
  nothing (rc=1). `tests/middleware.py:44` builds every request with `state={}` and
  `tests/test_plan_gate.py:106` overwrites `state` with `{"todos": …}` only, so the guard reads an
  empty `messages` on every call the suite makes.

  Neuter run — the checkout already carried another session's `return False  # AUDIT MUTATION` at
  `plan_gate.py:265`, i.e. exactly the mutation, so this run *is* the mutant run:

  ```
  $ uv run pytest tests/test_plan_gate.py tests/test_scratchpad.py tests/test_authz.py \
      tests/test_tool_authz.py tests/test_middleware_order.py tests/test_audit.py \
      tests/test_langgraph_agent.py tests/test_approvals.py tests/test_plan_state.py \
      tests/test_profiles.py tests/test_profile_autonomy_validation.py -q
  150 passed
  ```

  Liveness of the branch, against the **pristine** source (the mutated tree answers `False` for
  everything, which is how I found the mutation):

  ```
  chemclaw from: …/scratchpad/pristine/src/chemclaw/__init__.py
  guard (batched with write_todos): True
  guard (plain):                    False
  ```

  Production reachability of `state["messages"]`: `ToolNode._extract_state`
  (`langgraph/prebuilt/tool_node.py:1281`) returns the graph state as-is for the ordinary dict input
  and hydrates from channels for the Send payload, and `ToolCallRequest(state=tool_runtime.state)`
  is built from it — so inside the compiled graph the state does carry the `AIMessage` whose
  `tool_calls` contain this call. `ChemclawState` is a TypedDict (`agent/state.py:63`, over
  `PlanningState`), so the `.get("messages")` the guard does is the right access. The branch is live
  in a real turn.

- **Why**: every claim in the finding is true — the guard is untested, its removal is invisible to
  333 tests, and the reason is structural (`tool_request` hard-codes `state={}`). But the same
  objection as above applies to the severity: the shipped guard works, and no caller, request or
  configuration can make it stop working. The stated consequence — "the write executes under an
  approval a human gave for a different plan" — is what happens to a *mutated* build, not to this
  one. Rated as a coverage gap on an authorization control whose fix is two three-line cases, it is
  a medium; "high" borrows the severity of DARK-1 itself, which the code already prevents.

  Two corrections to the write-up, neither changing the verdict. (1) The failure of a mutated build
  would not be wholly silent: the tool call still goes through the audit middleware, so there is a
  trail — what is missing is a *refusal*, not the record. (2) The finding's own reproduction of the
  guard firing is the thing to keep: it is what proves the branch is not dead, and the two cases it
  proposes for `tests/test_plan_gate.py` are the right fix. Worth noting that the blindness is wider
  than this one function — because `tests/middleware.py::tool_request` gives every middleware an
  empty state, *any* future middleware that reads `messages` inherits the same untested hole; fixing
  it in the helper rather than in one test file is the higher-value change.
