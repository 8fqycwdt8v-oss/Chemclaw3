# Verdicts — `sweep-tests.md`, reachability/consequence lens

Scope: the three **high** findings. No finding in the file is marked critical; medium and low are
out of scope and were not examined.

Environment: `infra-postgres-1` healthy, `infra-temporal-1` up, venv synced. All mutants were
applied to the working tree, run, and reverted (`git status --short src/` clean at the end). The box
carries other reviewers' pytest processes throughout; two attempts at a full-suite run (with and
without coverage) were abandoned at ~1 % after 7–9 minutes, so the mutation runs below use a
grep-derived superset of the test files that could plausibly reach the code (54 files, 971 tests, in
the `plan_approval_store`/`routes/plan.py` case) rather than all 4040 tests. Where that matters, it
is said.

---

## The plan-approval hash: no test binds anything past the first todo line

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)
- **What I did**

  Reproduced the reporter's mutant and widened both the test set and the mutant set.

  ```
  $ # plan_identity → stable_hash(list(items)[:1])
  $ uv run pytest -q -p no:randomly tests/test_plan_gate.py tests/test_plan_state.py \
        tests/test_cli.py tests/test_langgraph_agent.py tests/test_langgraph_stream.py \
        tests/test_profiles.py tests/test_m12_probes.py tests/test_service.py tests/test_audit.py \
        tests/test_autonomy_eval.py tests/test_turn_cancellation.py tests/test_turn_signals.py \
        tests/test_degraded.py tests/test_checkpointer_schema.py tests/test_scratchpad.py \
        tests/test_upstream_surface.py
  270 passed in 29.38s
  ```

  That is every `tests/*.py` mentioning `plan_identity|plan_hash|plan_approval|write_todos|todos`
  — nine files more than the reporter ran. The mutant survives all of them.

  Then I drove the gate itself rather than the hash (`/tmp/probe_planhash.py`, real
  `enforce_plan_approval` + real `InMemoryPlanApprovalStore` + the real contextvar):

  ```
  unmutated:  hash A: 9889966ded12b512   hash B: 68b8a6a9817f3a40
              write under approved plan A: True    write under rewritten plan B: False
  mutated:    hash A: 533e75a28621d1d8   hash B: 533e75a28621d1d8
              write under approved plan A: True    write under rewritten plan B: True
  ```

  A = `["screen the species", "find precedent"]`, B = `["screen the species", "run every DFT job in
  the queue"]`. `propose_knowledge_note` — a knowledge-graph write — executes under an approval
  given for a different plan. That is the DARK-1 sequence verbatim.

  I also ran a second, more plausible mutant the reporter did not:
  `stable_hash(sorted(set(items)))` — the "normalize the plan before hashing" refactor anyone might
  write. It survives too (185 passed in 28.93s over the ten most relevant files), which means the
  suite does not bind plan *order* or duplicate lines either.

- **Why**

  Both halves of the lens hold. **Reachability**: nothing upstream constrains `plan_identity`'s
  input — the plan is the model's own `write_todos` output, read either from `request.state["todos"]`
  or from the checkpointer, with no schema, no length bound and no validator between the model and
  the hash. The one guard that *could* have stood in the way, `rewrites_the_plan_in_this_batch`,
  refuses only a gated call arriving in the *same assistant message* as a `write_todos`; the
  exploit shape is a rewrite in message N and the gated call in message N+1, which that guard is
  explicitly not for. Within a single turn the approval is still unspent (`consume_turn_approval`
  runs at turn end, `api/runner.py:449`), so the rewritten plan meets a live approval.

  **Consequence**: as stated, and I executed it rather than reasoning about it. The finding does not
  overclaim — it is careful that this is a mutation-survival result, i.e. regression exposure rather
  than a live defect; `plan_identity` is correct today.

  I considered downgrading to medium on the grounds that no deployment is broken right now. I did
  not, for two reasons the run produced: the surviving mutant set is not one contrived edit but a
  *class* (prefix, sort, dedupe — everything except "constant"), and the file's own headline
  regression test (`test_an_approved_plan_does_not_authorize_the_next_one`) changes the first line
  of the plan, so the repo's flagship proof of this control proves only "the hash varies with line
  1". The fix the finding proposes (parametrise the changed position) is right and cheap.

---

## `POST /sessions/{id}/plan/decision` is untested past its first guard

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)
- **What I did**

  Three simultaneous mutants in `decide_plan`, each removing a distinct property the docstring
  claims:

  1. the hash-mismatch `409` deleted outright;
  2. `record(session_id, plan_hash, "MUTANT-ACTOR", True)` — the posted `approved` flag and the
     principal's `oid` both discarded, so a **rejection is recorded as an approval** under a
     fabricated actor.

  ```
  $ uv run pytest -q -p no:randomly $(grep -rl "create_app\|TestClient\|plan_approval\|session_store\
        \|/plan\|plan_hash\|plan_identity" tests/*.py | grep -v conftest)
  971 passed, 10 warnings in 81.11s
  ```

  (Same run also carried the `plan_approval_store` mutant below.) 54 test files — every file that
  builds the app, uses `TestClient`, touches `session_store`, or mentions a plan hash. Nothing
  notices.

  I then wrote the missing test as a probe (`/tmp/probe_decide.py`, real `create_app` +
  `TestClient`, `session_todos` stubbed to a two-line plan) to check the *consequence* claim — that
  today's handler is correct and only untested:

  ```
  GET /plan -> {'plan_hash': '9889966ded12b512', 'plan': [...], 'approved': False, 'decided_by': None, 'mode': 'plan'}
  plan_identity agrees: True
  stale hash -> 409 the plan changed since it was shown; re-read it and decide again
  after stale post, decided_by: None
  matching hash -> 204
  after approve: {'approved': True, 'decided_by': 'dev-user', 'mode': 'execute'}
  reject -> 204  approved now False
  ```

- **Why**

  **Reachability**: the trigger is "every real approval", and that is right — this is the only
  network path that writes a `plan_approvals` row. `cli/chat.py::_plan_command` writes through the
  store directly and `evals/live.py:743` posts here but needs a model credential and is not part of
  `make test`, so nothing in CI exercises the handler body. The two tests the reporter names are
  exactly the two that reach the route, and both stop at or before line 107 — I read them
  (`tests/test_service.py:1427`, `:1464`); the first then writes its approval by calling
  `client.app.state.plan_approvals.record(...)` directly, which is the tell.

  **Consequence**: as stated, and slightly worse than the finding says. The report lists "dropped
  the hash binding" and "recorded `True` regardless of `body.approved`"; my run shows the recorded
  **actor** is equally unbound — writing a literal in place of `principal.oid` also ships green, so
  the durable record of *who approved* is unverified as well. Nothing here is a live defect: the
  handler behaves correctly on all four paths I drove. It is a blind spot on the human half of an
  authorization control, closed by roughly the fifteen lines above.

---

## The durable `PlanApprovalStore` never runs in the suite, and diverges from the twin it claims to mirror

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**

  Half (a), "zero coverage on the durable backend" — tested by a mutant that cannot be missed:

  ```python
  def __init__(self) -> None:
      raise RuntimeError("MUTANT: PlanApprovalStore was instantiated")
  ```

  Same 54-file / **971 passed** run as above. The class is never constructed. (No test sets
  `session_store="postgres"` and then reaches `plan_approval_store()`; the three files that set it
  — `test_audit.py`, `test_job_record.py`, `test_config.py` — do not.) The deployment claim checks
  out too: `infra/live/processes.sh:46` pins `CHEMCLAW_SESSION_STORE=postgres` "because they are what
  the Helm chart sets", so this is the production backend.

  Half (b), the divergence. I ran both backends against a live database in a throwaway schema
  (`/tmp/probe_pgstore.py`, real migrations, real `PlanApprovalStore`; `/tmp/probe_mem.py`):

  ```
  backend: PlanApprovalStore
  decision after approve: (True, 'alice')     decision after consume: (False, 'alice')
  pg rows s2 (direct double consume):          [(alice,True,consumed), (bob,True,consumed)]
  pg rows s3 (reach-back past rejection):      [(alice,True,consumed), (bob,True,consumed), (carol,False,-)]
  pg rows s4 (two consume_turn_approval calls):[(alice,True,UNSPENT),  (bob,True,consumed)]
  pg rows s5 (rejection then consume_turn_approval):
                                               [(alice,True,consumed), (bob,True,UNSPENT), (carol,False,-)]
  backend: InMemoryPlanApprovalStore
  mem s4 (two consume_turn_approval):          [(alice,True,UNSPENT),  (bob,True,consumed)]
  mem s5 (rejection then consume_turn_approval):
                                               [(alice,True,consumed), (bob,True,UNSPENT), (carol,False,-)]
  mem s2 (direct double consume):              [(alice,True,UNSPENT),  (bob,True,consumed)]
  ```

- **Why**

  Half (a) stands exactly as written, and the finding is right that a real-Postgres fixture is
  cheap. But the run above also answers the question the coverage number leaves open: the three SQL
  statements are **correct**. `record`/`decision`/`consume` behave, the `consumed_at IS NULL` fold
  works, an unknown key returns `None`. So the gap yields nothing today, which caps the severity.

  Half (b) is where the finding overreaches, and it is the half its `high` label rests on. The
  divergence is real **only when `store.consume()` is called directly**, which is what the
  reporter's probe does (rows `s2`/`s3` above reproduce it exactly). No such caller exists:
  `grep -rn "\.consume(" src/` returns one hit, `plan_gate.py:231`, inside `consume_turn_approval`,
  and it is guarded:

  ```python
  decision = await plan_approval_store().decision(session_id, plan_hash)
  if decision and decision[0]:
      await plan_approval_store().consume(session_id, plan_hash)
  ```

  `decision[0]` is the *effective* verdict of the row `_LATEST` returns — approved **and** unspent.
  So `consume` is only ever invoked in the one state where `_CONSUME`'s subquery and `_LATEST`
  select the same row, and the docstring's disputed clause, false as a property of the SQL, is true
  of every call the system makes. Rows `s4`/`s5` are the finding's own two scenarios driven through
  `consume_turn_approval` instead of the store: **the two backends produce byte-identical rows**,
  alice stays unspent in `s4`, bob stays unspent in `s5`. The stated trigger — "`api/runner.py`
  calls `consume_turn_approval` from two teardown paths … so the double-consume is the designed call
  pattern" — is therefore not a trigger for this defect. (It is also not two paths for one turn:
  449 is the last statement of the `try`, 523 is in the `except Exception` arm, and
  `consume_turn_approval` never raises.)

  What is left reachable is a TOCTOU window inside `consume_turn_approval` — a rejection posted, or
  a second turn's consume landing, between the `decision()` read and the `consume()` write on
  separate connections. Narrow, and the consequence is smaller than "an approval was used up by a
  turn that never happened" implies: because the table is append-only and both statements order
  `decided_at DESC, id DESC`, a row that is not the latest can never become the latest, so the
  falsely stamped `consumed_at` sits on a row `_LATEST` will never read again. No authorization is
  widened or narrowed — the reporter concedes this ("a falsified record rather than a widened
  authorization") but files at `high` anyway.

  Medium: a real record-fidelity flaw in one SQL statement, reachable only through a race or a
  caller that does not exist, plus a genuine but currently harmless coverage hole on the production
  backend. The proposed fixes (1) and (2) are worth doing; fix (3) narrows `_CONSUME` for a case no
  caller produces.
