# Sweep: test-suite integrity

Method. I read the coverage baseline, then re-derived it from the `.coverage` data file, then
stopped trusting either and *ran* things: eight hand-built mutants against the modules that carry
authorization invariants (each run against every test file that imports the module), four probe
scripts driving production code paths that no test drives, and two AST sweeps over `tests/` for
vacuous assertions and never-entered code.

Environment: Docker/Postgres/Temporal up (`docker ps` shows `infra-postgres-1` healthy), venv
synced. The box is under heavy concurrent load from the other reviewers, which is why two attempts
to regenerate a full coverage run were abandoned (1 % in six minutes against the baseline's 17
minutes for the whole suite).

Nine findings. Three are high: all three are in the **plan-approval control** — the pre-execution
gate that decides whether the agent may run a state-changing tool — and the pattern is the same in
each. The control has two implementations and three layers, and the suite exercises exactly one
implementation of one layer.

---

## The plan-approval hash: no test binds anything past the first todo line

- **Severity**: high
- **Location**: `src/chemclaw/agent/plan_gate.py:90` (`plan_identity`), tested by
  `tests/test_plan_gate.py:140` (`test_an_approved_plan_does_not_authorize_the_next_one`)
- **Trigger**: a mutant that hashes only the first todo item:
  `return stable_hash(list(items)[:1]) if items else None`
- **Consequence**: the whole reason `plan_approvals` is keyed by `(session_id, plan_hash)` rather
  than by session is stated in `infra/sql/020_plan_approvals.sql`: *"the agent could present a
  modest plan, have it approved, rewrite its todo list, and run something else under the same
  authorization."* Under this mutant that is exactly what happens as long as the agent keeps its
  first line — approve `["screen the species", "find precedent"]`, rewrite to
  `["screen the species", "run every DFT job in the queue"]`, and the gate still says approved. The
  suite does not notice.
- **Evidence**: the mutant survives 152 tests across every file that touches the gate.

  ```
  $ # mutant applied to plan_gate.plan_identity, then:
  $ uv run pytest -q tests/test_plan_gate.py tests/test_cli.py tests/test_langgraph_agent.py \
        tests/test_langgraph_stream.py tests/test_profiles.py tests/test_m12_probes.py \
        tests/test_service.py
  152 passed in 22.62s
  ```

  The cause is visible in the fixtures. Every plan in `tests/test_plan_gate.py` is one or two items
  (`_set_plan` calls at lines 151, 156, 198, 220, 359, 393, 446), and the single plan-*change*
  scenario — line 151 → line 156 — replaces the **first** item:

  ```python
  await _set_plan(session, ["screen the species", "find precedent"])
  await _approve(approvals, session)
  ...
  await _set_plan(session, ["compute the energy of every candidate"])   # first line differs
  ```

  No test in the repository edits, appends to, or removes a *later* line of an approved plan. As a
  control, the coarser mutant `return "constant-plan-identity" if items else None` **is** caught
  (`test_an_approved_plan_does_not_authorize_the_next_one` and
  `test_a_streamed_plan_carries_the_hash_a_decision_must_be_posted_against`, 2 failed) — so the
  suite proves the hash varies with the plan, and nothing more.
- **Fix**: add one case to `tests/test_plan_gate.py` that approves an *n*-item plan, appends an item
  (and separately edits item *n*), and asserts `PlanNotApprovedError`. Parametrise over the position
  changed so the property asserted is "every line binds", not "some line binds".

---

## `POST /sessions/{id}/plan/decision` is untested past its first guard

- **Severity**: high
- **Location**: `src/chemclaw/api/routes/plan.py:108-126` (`decide_plan`)
- **Trigger**: any request that gets past the empty-plan check — i.e. every real approval.
- **Consequence**: the human half of the approval line has no behavioural test. Coverage reports
  `api/routes/plan.py 25 4 4 1 76% 108-126`, and those four statements are the entire remainder of
  the handler: the hash-mismatch refusal (108-112), the `plan_approvals.record(...)` call (119-121)
  and the `204` (126). Nothing in the suite has ever recorded an approval through this route. A
  regression that dropped the hash binding, recorded `True` regardless of `body.approved`, wrote the
  *posted* hash instead of the session's current one, or returned 204 without writing anything at
  all, ships green.
- **Evidence**: only two tests reach this route and neither gets past line 107.
  `tests/test_service.py:1427 test_a_session_with_no_plan_has_nothing_to_decide_on` asserts the
  409 raised at lines 103-107 and then writes its approval by calling the store directly
  (`client.app.state.plan_approvals.record(...)`, line 1456), bypassing the handler.
  `tests/test_service.py:1464 test_every_session_scoped_route_is_ownership_gated` posts
  `{"plan_hash": "x"}` as a non-owner and asserts 404 — it never reaches the body.

  Measured: deleting the hash-mismatch refusal outright is invisible.

  ```
  $ # `if body.plan_hash != plan_hash: raise HTTPException(409, ...)` deleted from decide_plan
  $ uv run pytest -q tests/test_service.py tests/test_plan_gate.py tests/test_langgraph_stream.py \
        tests/test_route_auth_coverage.py tests/test_cli.py
  108 passed in 25.45s
  ```

  The docstring at line 90 asserts the property that mutant removes: *"A mismatch is a 409, not a
  silent approval of the current plan: it means the plan changed between being shown and being
  approved, and the human agreed to something else."* Nothing checks it.
- **Fix**: one test that drives a session to a non-empty plan, `GET`s the plan, posts the returned
  hash and asserts `204` + `approved is True` + `decided_by == principal.oid`; and a second that
  posts a stale hash and asserts `409` with the approval **not** recorded.

---

## The durable `PlanApprovalStore` never runs in the suite, and diverges from the twin it claims to mirror

- **Severity**: high
- **Location**: `src/chemclaw/agent/plan_approval_store.py` — `PlanApprovalStore` (lines 104-147),
  `_CONSUME` (lines 79-84), `plan_approval_store()` (line 223-225)
- **Trigger**: any deployment with `session_store="postgres"` — i.e. every real one. Reproduced by
  instantiating the class, which nothing in `tests/` does.
- **Consequence**: two things at once.

  (a) **Zero coverage on the durable backend.** Coverage reports
  `agent/plan_approval_store.py 69 16 10 2 77% 109, 113, 117-120, 129-132, 143-147, 197->exit, 224`
  — that missing set is exactly `PlanApprovalStore.__init__`, `_connection`, `record`, `consume`,
  `decision`, plus the `return PlanApprovalStore()` branch of the factory. The three SQL statements
  that *are* this control in production have never been executed against a database, in a repository
  whose CI runs a real Postgres. The 77 % is supplied entirely by `InMemoryPlanApprovalStore`, which
  is the backend only the CLI uses.

  (b) **The two implementations disagree, and the module docstring says they cannot.** Line 39:
  *"The in-memory backend mirrors it exactly, which costs nothing and matters … a control with two
  implementations that disagree about when an approval is spent is a control nobody can reason
  about."* And line 76 on `_CONSUME`: *"Scoped by `approved AND consumed_at IS NULL` so it is
  idempotent (a second call matches nothing) and so it can never stamp a rejection **or reach back
  past a newer decision** — the subquery picks exactly the row `_LATEST` reads."*

  That last clause is false. `_LATEST` orders by `decided_at DESC, id DESC` with no predicate;
  `_CONSUME`'s subquery adds `AND approved AND consumed_at IS NULL`. When the newest row is not an
  unspent approval, the two select **different rows**, and `consume` reaches back and stamps an older
  approval that never authorized anything. `InMemoryPlanApprovalStore.consume` (line 196) reads
  `self._latest(...)` — the absolute latest, no predicate — and correctly does nothing.

  `api/runner.py` calls `consume_turn_approval` from two teardown paths (lines 449 and 523) and the
  docstring says so — *"the callers cannot guarantee they run once"* — so the double-consume is the
  designed call pattern, not an edge case.

- **Evidence**: `/tmp/…/scratchpad/probe_divergence.py` drives both backends through the identical
  sequence (approve, approve again without consuming, then the runner's two `consume` calls):

  ```
  --- in-memory backend ---
    after first consume : (False, 'bob')
    after second consume: (False, 'bob')
    mem row alice True UNSPENT
    mem row bob   True consumed
  --- postgres backend ---
    after first consume : (False, 'bob')
    after second consume: (False, 'bob')
    pg  row alice True consumed      <-- spent by the "idempotent" second call
    pg  row bob   True consumed
  ```

  A second probe (`probe_approval.py`) shows the reach-back past a rejection: after
  `record(alice,True) → consume → record(bob,True) → record(carol,False) → consume`, `bob`'s
  approval carries a `consumed_at` even though a rejection superseded it and no turn ever ran under
  it. The effective verdict from `decision()` is unaffected in both cases (it reads the latest row),
  so this is a falsified record rather than a widened authorization — the durable evidence says an
  approval was *used up* by a turn that never happened, and migration 034's stated value is
  precisely *"a spent approval keeps saying who approved it and when it was used up"*.
- **Fix**: (1) add a Postgres-backed test module for `PlanApprovalStore` — the suite already has
  `tests/pg.py::migrated_db_or_skip` and `plan_approvals` is in the migration set, so this is a
  fixture and four tests; (2) drive both backends through one parametrised contract test, which is
  what would have caught the divergence; (3) narrow `_CONSUME` so it can only stamp the row `_LATEST`
  returns, e.g. by selecting the latest row unconditionally and requiring
  `approved AND consumed_at IS NULL` on *that* row rather than in the row selection.

---

## The approval-listing test asserts a filter the production code does not implement

- **Severity**: medium
- **Location**: `tests/test_approvals.py:54-55` (`fake_list` in the `seam` fixture) vs
  `src/chemclaw/agent/interaction_tools.py:147` (`list_pending_approvals`); the test is
  `tests/test_approvals.py:144 test_listing_is_scoped_to_the_caller`
- **Trigger**: any hold whose `requested_by` is `""` — the "started with no real actor" case the
  sibling test at line 119 exists for.
- **Consequence**: the fake and the real function implement **different** owner filters, and the
  test asserts the fake's. `GET /approvals` never runs the real narrowing at all
  (`interaction_tools.py` is 61 % covered; 128-147, the whole of `list_pending_approvals`, is
  missing), so the route's authorization scoping is proven by nothing.

  ```python
  # tests/test_approvals.py — the fake
  return [h for h in holds if owner is None or h.requested_by in ("", owner)]
  # src/chemclaw/agent/interaction_tools.py — the system
  if owner is not None and holder != owner:
      continue
  ```

  The fake treats an empty holder as matching *every* caller. The real function drops it. The two
  tests in this file therefore encode contradictory policies for the same state:
  `test_unowned_hold_is_unreachable_once_entra_is_required` (line 119) asserts an empty owner is
  answerable by **nobody** (404), while `test_listing_is_scoped_to_the_caller` asserts an empty owner
  is listed for **anybody** — and only the second one is testing a fake.
- **Evidence**: `/tmp/…/scratchpad/probe_listing.py` drives the real `list_pending_approvals` over a
  stub Temporal client holding exactly the fixture's data
  (`{"mine": ("", "q"), "theirs": ("somebody-else", "q")}`):

  ```
  real list_pending_approvals(owner='dev-user') -> []
  tests/test_approvals.py::seam fake_list       -> ['mine']
  ```

  The test asserts `["mine"]`. Production cannot produce that value for that input.
- **Fix**: give the empty-owner case one policy and assert it against the real function. The
  ownership predicate (`owner is not None and holder != owner`) should be extracted as a pure
  function and tested directly — as `api/deps._owner_authorizes` already is — so the route test can
  keep its stub without the stub carrying an authorization rule.

---

## `apply_grants` never executes: the append-only guarantee is checked by regex over SQL text

- **Severity**: medium
- **Location**: `src/chemclaw/core/grants.py:45-76` (`apply_grants`);
  `tests/test_database_privileges.py:200-260` (`verbs_the_grant_allows`,
  `test_the_audit_trail_is_append_only_by_grant`)
- **Trigger**: `make db-grants` on a deployment that has split its database principal — the only
  configuration in which any of this has an effect.
- **Consequence**: `infra/sql/grants/app_privileges.sql` says of itself: *"A hash chain (011) and
  signed anchors (032) detected that afterwards; nothing prevented it. Those are gone now, so this
  file is the whole of the guarantee."* The suite verifies that guarantee by **parsing the SQL file
  with a regex** and comparing verb sets against SQL literals grepped out of `src/`. It never
  executes the file, never creates the role, and never asks Postgres what the role can do. Coverage:
  `core/grants.py 21 11 6 1 41% 63-76, 83` — the whole of `apply_grants`, including the documented
  `RuntimeError` for an empty grants directory ("the deploy then continued with a role holding
  whatever privileges it happened to have — the exact failure this exists to prevent, reported as
  success and exiting 0"), which is asserted by no test.

  A `DO $$` block that raises, a `format()` with a swapped placeholder, an over-broad `REVOKE`, or a
  `sql_migrations_dir` that does not ship the subdirectory are all invisible to `make ci`.
- **Evidence**: I ran it. `/tmp/…/scratchpad/probe_grants.py` creates `chemclaw_app`, calls
  `apply_grants(dsn)` against the live database, and reads `information_schema.role_table_grants`:

  ```
  tables in public: 33
  apply_grants -> ['app_privileges.sql']
  granted tables: 33
    audit_events        INSERT,SELECT
    schema_migrations   SELECT
    audit_anchors       SELECT
    ...
  ```

  The mechanism is **correct today** — `audit_events` really is INSERT-only — which is exactly why
  this is a verification gap and not a live defect. Note also that the static check's verb set is
  write-only by construction (`if not granted <= {"INSERT","UPDATE","DELETE"}: continue`), so
  `test_the_migration_ledger_is_never_granted_to_the_runtime_role` passes while the ledger is in fact
  granted `SELECT`; the assertion is narrower than its name and docstring.
- **Fix**: one Postgres-backed test that creates the role in the test schema, calls `apply_grants`,
  and asserts the *observed* grants for `audit_events` and `schema_migrations` from
  `information_schema.role_table_grants` — i.e. asks the database rather than the file. Plus a
  two-line test for the empty-directory `RuntimeError` (point `sql_migrations_dir` at a `tmp_path`).

---

## `require_principal`'s 503 branch is uncovered while a test docstring claims it

- **Severity**: low
- **Location**: `src/chemclaw/api/auth.py:229-231`; the claim is in
  `tests/test_auth.py:301` (`test_a_jwks_outage_is_a_503_not_a_401`, docstring)
- **Trigger**: the tenant JWKS unreachable while a request carries a valid token.
- **Consequence**: coverage reports `api/auth.py … 96% 130, 230-231`. Line 130 is the
  no-`kid` refusal; 230-231 are the `IdentityProviderUnavailable → 503` handler in
  `require_principal`. The existing test drives `_signing_key` directly and asserts the exception
  type; its docstring then says *"which the route turns into 503, not 401"* — the second clause is
  asserted by nothing. Collapsing the two `except` arms into one `401` (the failure mode the module
  docstring argues against at length: "answering 401 would tell a user with a perfectly good token
  that it was rejected, and would hide a dependency failure inside a metric operators read as
  'someone is probing us'") passes the suite.
- **Evidence**: coverage's missing-line set, plus `grep -rn "IdentityProviderUnavailable" tests/`
  returning only `tests/test_auth.py:301-309`, which never builds a request.
- **Fix**: extend the existing test to drive `require_principal` through the app with `_signing_key`
  patched to raise `IdentityProviderUnavailable`, asserting `503`. Same shape for the no-`kid` token
  at line 130. (I mutated `api/auth.py` twice as a control — removing `audience=` and removing the
  `GROUP_ROLE_PREFIX` namespacing — and the suite killed both, 5 failed and 2 failed respectively.
  This module is otherwise well tested; the gap is the route arm specifically.)

---

## A tautological assert in the DARK-1 regression test

- **Severity**: low
- **Location**: `tests/test_plan_gate.py:162` and `:166`
- **Trigger**: always.
- **Consequence**: one of the two assertions in the repository's headline authorization-regression
  test can never fail. `_run` returns `(approved_write, True)`, and the caller asserts the second
  element:

  ```python
      return approved_write, True                      # line 162
  approved_write, demoted = asyncio.run(_run())        # line 164
  assert approved_write, "the approved plan's own write was refused; the gate is too tight"
  assert demoted, "the session kept an execute mode it is not entitled to"   # line 166
  ```

  It is a leftover from the MAF-era session mode — the comment two lines above says so ("No mode to
  check … nothing else says 'may this session act'") — but it survives as a live-looking assertion
  with a security-sounding message, which is worse than no assertion: a reader counting what this
  test proves will count it.
- **Evidence**: read directly; my AST sweep for literal-constant assertions over all 249 test files
  found only three other candidates (`test_durable_heartbeat.py:155`, `test_tracing.py:86`,
  `test_worker_observability.py:139`) and all three are genuine — flags set from a nested scope.
  This one is interprocedural, which is why the sweep missed it.
- **Fix**: delete lines 162/166 and return `approved_write` alone. The `pytest.raises` at line 157
  is the real assertion.

---

## `FakeUpdate` models a reader that no longer exists

- **Severity**: low
- **Location**: `tests/fakes.py:41-65` (`FakeUpdate.user_input_requests`);
  `src/chemclaw/api/runner_trace.py:1` (module docstring)
- **Trigger**: none — that is the finding.
- **Consequence**: `tests/fakes.py` carries a 25-line justification for deriving
  `user_input_requests` from `contents` rather than hard-coding it, on the stated grounds that *"the
  real filter is on `content.user_input_request`"* and that hard-coding it empty is what left *"the
  runner's approval branch … executed by no test in the suite until D-2026-08-08"*. There is no such
  filter and no such branch: `grep -rn "user_input_request" src/` returns **nothing**. The property
  is never even reached by a test — every `FakeUpdate` in the suite is built from
  `_CallContent`/`_ResultContent`/`_ToolContent` (`tests/test_tool_results.py`,
  `tests/test_runner.py`, `tests/test_service_events.py`). `runner_trace.py`'s own first line still
  advertises "tool-call reassembly **and the approval prompt**"; `grep -n approval` on that file
  matches line 1 and nothing else.
- **Evidence**: `grep -rn "user_input_request\|function_approval_request" src/ tests/` — 0 hits in
  `src/`, 8 in `tests/fakes.py` and its cross-reference in `tests/fakes_langgraph.py:7`.
- **Fix**: delete the property and the paragraph that justifies it; correct `runner_trace.py`'s
  first line. A shared fake whose docstring argues for a field the system does not read is a fake
  that will be preserved through the next refactor for a reason that stopped existing.

---

## mutmut's seven modules leave the whole approval control unmutated

- **Severity**: low (observational — but it is the reason the three high findings above are
  invisible to the existing tooling)
- **Location**: `pyproject.toml:358-367` (`[tool.mutmut] source_paths`)
- **Trigger**: `make mutants`.
- **Consequence**: the configured seven are `agent/authz.py`, `agent/audit_store.py`,
  `api/budget.py`, `api/runner_trace.py`, `kg/note.py`, `kg/pr_gate.py`, `science/calc/store.py`.
  The comment beside them says the criterion is *"where a silently-wrong answer is expensive"*. By
  that criterion these are missing, in descending order of what I could measure:

  | module | invariant it carries | mutant result |
  |---|---|---|
  | `agent/plan_gate.py` | which plan an approval authorizes | **survived** (first-line-only hash, 152 tests) |
  | `api/routes/plan.py` | the human decision is hash-bound and recorded | **survived** (guard deleted, 108 tests) |
  | `agent/plan_approval_store.py` | when an approval is spent | not mutatable — the Postgres backend is never executed |
  | `core/grants.py` | the audit trail is append-only by grant | not mutatable — `apply_grants` is never executed |
  | `api/auth.py` | signature/audience/issuer/expiry, group namespacing | killed (2/2) |
  | `agent/skill_backend.py` | which skills a role may reach | killed (2/2) |
  | `connectors/identity.py` | identity headers never cross an origin | killed (1/1) |
  | `agent/compaction.py` | a cut never strands a tool call | killed (1/1) |
  | `agent/scratchpad.py` | one person's memories are one namespace | killed (1/1, by a single test) |

  Note that `api/runner_trace.py` **is** in the list while `api/routes/plan.py` — the route that
  writes the authorization record — is not.
- **Evidence**: the eight mutation runs above; each was applied to the working tree, run against
  every test file importing the module, and reverted (`git status --short src/` clean at the end).
- **Fix**: add `agent/plan_gate.py`, `api/routes/plan.py` and `agent/plan_approval_store.py` to
  `source_paths` — after the tests those findings call for exist, otherwise the run will simply
  report what is already known here.

---

## What I checked and found sound

Reporting these so the negative results are on the record rather than looking unexamined.

- **Incidental coverage.** The brief's cross-check — modules whose lines are covered only as a side
  effect of importing them — found **nothing**. I walked every measured file's AST, mapped each
  function's body-line range against the executed-line set, and looked for modules where fewer than
  40 % of functions were ever entered: zero hits. Every `src/chemclaw` module has real function-body
  execution behind its number.
- **Never-executed `except` handlers.** Attempted the same sweep for exception bodies; the analysis
  is reported here as incomplete rather than clean, because the `.coverage` data file was clobbered
  by my own (subsequently abandoned) instrumentation run before it finished, and the box was too
  loaded to regenerate it. The individual uncovered handlers I did identify from the printed report
  are in the findings above (`api/auth.py:230-231`, `api/routes/approvals.py:44-45,60-61`).
- **Vacuous loops.** 64 `for`-loops whose body is only assertions; all but the one at
  `tests/test_plan_gate.py` iterate literal tuples or collections with an explicit non-emptiness
  guard beside them (`tests/test_eval_baseline_check.py:70` is the model: `assert registered_names()
  # the registry is populated, so this proves something`).
- **Tautological assertions.** AST sweep over all 249 test files for `assert <truthy literal>`,
  `assert <name only ever assigned a constant>`, and `assert ... or <truthy literal>`: three
  candidates, all false positives (flags mutated from a nested scope). The only real one is the
  interprocedural case reported above.
- **Fakes.** `tests/fakes_turn.py` compiles a *real* `build_langgraph_agent` over a scripted model
  rather than standing in for the graph; `tests/surface.py` reads the three production functions
  rather than a framework object's internals; `tests/middleware.py` deliberately refuses to
  reimplement upstream's chain composition. `tests/calc_server_fake.py`'s key derivation matches
  `connectors/calc/`'s expectations (its `_KEYED` omits `optimize_geometry` correctly — this repo
  composes `embed`+`relax` rather than calling the remote tool, per
  `connectors/calc/server/tools.py:824`). `tests/legacy_rows.py`'s MAF payloads exercise both the
  dict and string `arguments` forms that `message_migration._arguments` branches on.
  The two drifted fakes are the ones reported above.
- **Skips.** The suite has almost none: the baseline run reported `1 skipped` out of 4040. The
  skip-shaped risks are `tests/pg.py` (Postgres unreachable → ~157 silent skips, already documented
  in `CLAUDE.md`) and `tests/test_prompt_caching.py:304,379` (`skipif "API-KEY" not in os.environ`),
  which never run in CI.
- **Baseline failures.** `test_no_grandfathered_edit_outlives_its_reason` passes now
  (`1 passed in 0.69s`); commit `d919044` "Fix the migration guard's truncation sentinel" landed
  after the baseline was taken. `test_reizman.py::test_bo_campaign_finds_high_yield` failed as a
  `pytest-timeout` cap, which by this repo's own terminal-summary hook means its assertions never
  ran and it is not evidence either way — worth noting only because `make cov` and CI set no
  `PYTEST_TIMEOUT_SCALE`, so the gate's verdict depends on how loaded the machine is.
