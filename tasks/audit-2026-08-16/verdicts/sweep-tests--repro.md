# Adversarial verification — `sweep-tests.md`, lens: does it actually reproduce?

Scope: the three **high** findings only.

**Method note.** My first attempt ran mutants in the working tree at `/home/user/Chemclaw3`. Mid-run
the mutant vanished — another session committed (`0d5c4424 "Mock server, security lens"`) and the
tree was reset under me, so a "187 passed" result I had already collected was worthless. Everything
below was therefore re-run in three private copies of the checkout
(`/tmp/verify/repo` = mutant, `/tmp/verify/clean` = pristine, `/tmp/verify/ctrl` = control mutant),
each driven with `PYTHONPATH=<copy>/src uv run --project /home/user/Chemclaw3 --no-sync pytest`,
which I verified resolves `chemclaw` out of the copy and not out of the real tree:

```
$ PYTHONPATH=/tmp/verify/repo/src uv run ... python -c "import chemclaw.agent.plan_gate as m; print(m.__file__)"
/tmp/verify/repo/src/chemclaw/agent/plan_gate.py
```

Two tests fail in every isolated copy regardless of mutation —
`test_migrations_are_additive.py::test_no_merged_migration_had_its_statements_changed` and
`::test_no_grandfathered_edit_outlives_its_reason` — because the copies carry no `.git`. Verified
identical in the pristine copy (`2 failed, 120 passed in 0.62s`), so they are excluded as artifacts
throughout.

Postgres and Temporal were up for all of it (`infra-postgres-1` healthy, `pg_isready` → accepting).

---

## The plan-approval hash: no test binds anything past the first todo line

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (down from high — see Why)
- **What I did**:

  Applied my own mutant to the pristine copy and verified it was live before running anything:

  ```
  $ # src/chemclaw/agent/plan_gate.py:90
  $ #   - return stable_hash(list(items)) if items else None
  $ #   + return stable_hash(list(items)[:1]) if items else None
  $ PYTHONPATH=/tmp/verify/repo/src uv run ... python -c "from chemclaw.agent.plan_gate import plan_identity;
        print(plan_identity(['a','b'])==plan_identity(['a','c']))"
  True
  ```

  Rather than accept the reporter's seven-file selection I derived the candidate set myself:
  every test file matching `plan_identity|plan_hash|plan_gate|write_todos|todos|harness|approval|
  Approval|STATE_CHANGING|side_effecting|/plan` — 55 files. A test that cannot reach the plan gate
  cannot kill this mutant, so this is a superset of what matters.

  ```
  $ PYTHONPATH=/tmp/verify/repo/src uv run ... pytest -q -p no:randomly $(cat runfiles.txt)
  2 failed, 986 passed, 2 skipped, 9 warnings in 86.99s
  ```

  The two failures are the `.git` artifacts named above. **986 tests, zero kills.**

  Control, in a third copy (`return "constant-plan-identity" if items else None`):

  ```
  $ PYTHONPATH=/tmp/verify/ctrl/src uv run ... pytest -q -p no:randomly tests/test_plan_gate.py \
        tests/test_cli.py tests/test_langgraph_agent.py tests/test_langgraph_stream.py \
        tests/test_profiles.py tests/test_m12_probes.py tests/test_service.py tests/test_plan_state.py
  FAILED tests/test_plan_gate.py::test_an_approved_plan_does_not_authorize_the_next_one
  FAILED tests/test_langgraph_stream.py::test_a_streamed_plan_carries_the_hash_a_decision_must_be_posted_against
  2 failed, 154 passed in 27.13s
  ```

  Exactly the two tests the reporter names, so the suite is functioning and the gap is specifically
  positional.

  Line numbers and fixtures check out: `plan_identity`'s body is `plan_gate.py:90`; every `_set_plan`
  call in `tests/test_plan_gate.py` is at 151, 156, 198, 220, 359, 393, 446 as claimed; the one
  plan-*change* scenario (151 → 156) replaces the first item.

- **Why**: It reproduces on my own mutant, my own file selection, and a larger test population than
  the reporter used (986 vs 152). The control kill proves the negative result is not a broken
  harness.

  I would assign **medium** rather than high, for one reason the finding does not weigh: there is no
  live defect and the untested invariant belongs to a single expression, `stable_hash(list(items))`,
  over the whole list. The regressions that are actually plausible for that expression — hashing a
  constant, hashing the rendered checkbox line instead of `content`, treating the empty plan as
  approvable — are all covered (the control mutant, `test_a_streamed_plan_carries_the_hash…`,
  `test_a_session_with_no_plan_has_nothing_to_decide_on`). What escapes is a *partial-binding*
  regression, which is not a shape this code can drift into by accident. The gap is real and the fix
  is one parametrised test; the exposure it represents today is smaller than "the plan-approval
  control is unguarded" implies.

---

## `POST /sessions/{id}/plan/decision` is untested past its first guard

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (down from high)
- **What I did**:

  I did not rerun the reporter's mutant; I measured the thing the mutant was a proxy for. First I
  established which test files can reach the handler at all — the route is only reachable by an HTTP
  call, and the literal `plan/decision` appears in exactly `tests/test_service.py` (twice),
  `tests/test_m12_probes.py` (an `httpx.MockTransport`, never the real app) and `src/chemclaw/evals/live.py`
  (not collected). The only other way in is `tests/test_service.py:1518`'s
  `route.path.format(...)` sweep. `tests/test_route_auth_coverage.py` inspects `Dependant` trees
  statically and issues no requests.

  Then coverage, over every file that could plausibly participate:

  ```
  $ PYTHONPATH=/tmp/verify/clean/src uv run ... pytest -q -p no:randomly \
      --cov=chemclaw.api.routes.plan --cov=chemclaw.agent.plan_approval_store --cov-report=term-missing \
      tests/test_service.py tests/test_plan_gate.py tests/test_cli.py tests/test_langgraph_stream.py \
      tests/test_m12_probes.py tests/test_audit.py tests/test_job_record.py tests/test_preferences.py \
      tests/test_session_store.py tests/test_turn_cost.py tests/test_config.py tests/test_framing.py \
      tests/test_langgraph_agent.py tests/test_profiles.py tests/test_plan_state.py \
      tests/test_route_auth_coverage.py tests/test_runner.py

  Name                                        Stmts   Miss Branch BrPart  Cover   Missing
  src/chemclaw/agent/plan_approval_store.py      69     16     10      2    77%   109, 113, 117-120, 129-132, 143-147, 197->exit, 224
  src/chemclaw/api/routes/plan.py                25      4      4      1    76%   108-126
  336 passed in 71.37s
  ```

  My numbers are byte-identical to the reporter's, and `108-126` is exactly the hash-mismatch
  refusal, the `plan_approvals.record(...)` call and the `204`.

  I then ran the *other* 42 candidate files (713 tests) under the same coverage, to make sure
  nothing outside my first selection reaches the handler:

  ```
  src/chemclaw/agent/plan_approval_store.py   69   28   10    1   53%  109, 113, 117-120, 129-132, 143-147, 185-188, 192, 196-198, 202-205, 224
  src/chemclaw/api/routes/plan.py             25    7    4    0   62%  101-126
  2 failed, 713 passed, 2 skipped in 72.90s
  ```

  `decide_plan` is uncovered *in its entirety* there (101-126). Across all 55 candidate files —
  1,049 tests — the union of covered lines still stops at `plan.py:107`.

  I then proved the path is reachable and that the code is *correct* today, by driving the real
  handler (only `session_todos` stubbed, so the plan is non-empty):

  ```
  GET /plan -> {'plan': ['screen the species','find precedent','write it up'],
                'plan_hash': 'c2f68b6ba31daa3f', 'approved': False, 'decided_by': None}
  hash == plan_identity(PLAN): True
  POST stale hash   -> 409 {"detail":"the plan changed since it was shown; re-read it and decide again"}
    approved after stale post = False
  POST correct hash -> 204
    after -> {'approved': True, 'decided_by': 'dev-user', 'mode': 'execute'}
    store rows: [('dev-user', True, None)]
  ```

- **Why**: The coverage claim reproduces exactly and the reachability argument closes it — no test
  file other than `test_service.py` can execute this handler, and neither of its two tests gets past
  line 107. So the hash binding, the record and the 204 are asserted by nothing.

  I downgrade to **medium** on consequence, not on mechanism: the handler is correct today (my
  round-trip above *is* the missing test, and it passes on unmodified code), so this is a
  verification gap rather than a defect, and the reporter's own equally-security-relevant
  `apply_grants` gap is filed at medium. Worth adding in the finding's favour: the fix is about
  fifteen lines and I have already run it green, so there is no cost argument against closing it.

---

## The durable `PlanApprovalStore` never runs in the suite, and diverges from the twin it claims to mirror

- **Verdict**: OVERSTATED — half (a) confirmed, half (b) refuted
- **Severity I would assign**: medium
- **What I did**:

  **(a) Zero coverage on the durable backend — confirmed.** `grep -rn "PlanApprovalStore" tests/*.py
  | grep -v InMemory` returns nothing, and the coverage run above reports
  `109, 113, 117-120, 129-132, 143-147, … 224` missing — i.e. `__init__`, `_connection`, `record`,
  `consume`, `decision`, and the `return PlanApprovalStore()` branch of the factory. `224` being
  missing means the `@cache`d factory never even *constructed* it across 336 tests, including the
  ones that set `session_store="postgres"`. The second coverage run over the other 42 candidate
  files (713 tests) reports the same six line groups missing. The three SQL statements really have
  never run against a database in this suite.

  **(b) The divergence — real at the store API, unreachable through production.** I wrote my own
  probe (not the reporter's), created a private schema on the live Postgres, applied `020` + `034`,
  and drove both backends:

  ```
  --- scenario A: two unspent approvals, then consume() twice ---
   in-memory: verdict (False,'bob') ; rows [('alice',True,UNSPENT), ('bob',True,consumed)]
   postgres : verdict (False,'bob') ; rows [('alice',True,consumed), ('bob',True,consumed)]
  --- scenario B: approve→consume→approve→reject→consume() ---
   in-memory: rows [('alice',True,consumed), ('bob',True,UNSPENT), ('carol',False,-)]
   postgres : rows [('alice',True,consumed), ('bob',True,consumed), ('carol',False,-)]
  ```

  So the reporter's transcript reproduces — *when you call `store.consume()` directly*. That is not
  how production calls it. `grep -rn "\.consume(" src/` returns exactly one call site,
  `plan_gate.py:231`, and it is guarded on the line above:

  ```python
  decision = await plan_approval_store().decision(session_id, plan_hash)   # 229
  if decision and decision[0]:                                             # 230
      await plan_approval_store().consume(session_id, plan_hash)           # 231
  ```

  `decision[0]` is true **iff** the latest row is approved and unspent — and in exactly that state
  `_CONSUME`'s subquery selects that same latest row. The guard is what makes the docstring's claim
  true in practice. Re-running both of the reporter's scenarios through the production entry point
  `consume_turn_approval` instead of raw `consume`:

  ```
  backend: PlanApprovalStore
  scenario A: after 1st consume_turn_approval (False,'bob'); after 2nd (False,'bob')
    pg  rows: [(1,'alice',True,UNSPENT), (2,'bob',True,consumed)]
    mem rows: [('alice',True,UNSPENT),   ('bob',True,consumed)]
  scenario B: pg verdict (False,'carol'); mem verdict (False,'carol')
    pg  rows: [('alice',True,consumed), ('bob',True,UNSPENT), ('carol',False,-)]
    mem rows: [('alice',True,consumed), ('bob',True,UNSPENT), ('carol',False,-)]
  ```

  **Identical, row for row, in both backends.** The falsified `consumed_at` the finding reports does
  not occur.

  The finding's supporting argument for reachability is also wrong on the code. It says
  `api/runner.py` "calls `consume_turn_approval` from two teardown paths (lines 449 and 523) … so
  the double-consume is the designed call pattern". Line 449 is the last statement of the `try`
  body; line 523 sits inside `except Exception as exc:`. They are alternative branches, not a
  sequence — and `consume_turn_approval` swallows `Exception` itself, so it cannot fall from 449
  into 523. A `CancelledError` at 449 is caught by the `except (GeneratorExit, CancelledError)`
  clause at 450, not by 523. And even if both did run, the transcript above shows two
  `consume_turn_approval` calls are a genuine no-op in both backends.

- **Why**: (a) is exactly as reported and worth acting on. (b) does not survive: the mechanism is
  real in the SQL but the trigger is unreachable, because the only production caller pre-checks
  precisely the condition that makes `_CONSUME` and `_LATEST` agree. The reporter reached it only by
  calling the store's raw method twice, which nothing in `src/` does — a finding about the probe's
  scaffolding.

  What does survive from (b), and is worth keeping in the record: the `_CONSUME` docstring's clause
  *"it can never … reach back past a newer decision — the subquery picks exactly the row `_LATEST`
  reads"* is **false as a statement about the SQL**, as my first probe shows. It is true only as a
  statement about the SQL *plus its one guarded caller*. That is a comment defect and a latent trap
  for whoever adds the second caller, not a live divergence. Note also that in the one interleaving
  where the two backends could still differ — a decision row inserted between `decision()` and
  `consume()` — Postgres stamps the approval the finished turn actually ran under and the in-memory
  store leaves it unspent, so there the *durable* backend keeps the more faithful record, the
  opposite of the finding's direction.

  Severity **medium**: an authorization control's production backend with no test that has ever
  executed its SQL is a genuine gap in a repo whose CI runs a real Postgres. It is not high, because
  nothing is broken — I ran the statements against Postgres myself and they behave as designed.
