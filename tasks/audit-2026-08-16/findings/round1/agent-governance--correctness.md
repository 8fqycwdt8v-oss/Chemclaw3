# Round 1 — agent governance slice, CORRECTNESS lens

Slice: `src/chemclaw/agent/{authz,tool_authz,plan_gate,skill_access,skill_backend,tool_invocation,audit,audit_store,turn_flags}.py`

Everything below was reproduced against the installed dependencies (`deepagents 0.7.6`,
`langchain`) and, where a database was involved, against the live Postgres in this environment.
Two claims I checked and found **true** are recorded at the end, so the negative results are not
mistaken for unexamined ground.

---

## The dry-run gate and the plan gate both read the raw `file_path`, but the tool writes the *normalized* one

- **Severity**: high
- **Location**: `src/chemclaw/agent/authz.py:198` (`writes_durable_memory`), consumed at
  `src/chemclaw/agent/authz.py:234` (`side_effecting_call`),
  `src/chemclaw/agent/tool_authz.py:95` (`dry_run_refusal`),
  `src/chemclaw/agent/plan_gate.py:115` (`gated_call`)
- **Trigger**: the model calls `write_file` (or `edit_file`) with a `file_path` that is not already
  the canonical absolute spelling of the memory root — e.g. `"memories/note.md"` or
  `"/./memories/note.md"` instead of `"/memories/note.md"` — on a `dry_run` turn, or under
  `harness_autonomy="plan_only"` with no approved plan.
- **Consequence**: the call is classified as a turn-local scratchpad write and **is not gated**.
  `deepagents`' write tool then runs `validate_path(file_path)` *before* it touches the backend
  (`deepagents/middleware/filesystem.py`, `async_write_file`: `validated_path = validate_path(file_path)`
  … `await resolved_backend.awrite(validated_path, content)`), and `validate_path` normalizes both
  spellings to `/memories/note.md`. `CompositeBackend._route_for_path` then routes that to the
  `StoreBackend` — Postgres, outliving the session, the process and the deployment. So a dry run
  writes durable memory, and an unapproved plan writes durable memory, through the one gate whose
  entire reason to exist is telling those two roots apart.

  Precondition for the *durable* half: `agent_memory_enabled=true` and `session_store="postgres"`
  and a turn with an actor (`api/runner.py:631`, `agent/scratchpad.py:197`). `agent_memory_enabled`
  defaults to `False` (`core/config/agent.py:101`), so today the mis-classified write lands in graph
  state. The classification is wrong either way, and it becomes a live bypass the moment the
  deployment turns memory on — which is exactly the posture `writes_durable_memory` was written for.

- **Evidence**:

  The gate, on the three spellings (`/tmp` script, dry run active):

  ```
  '/memories/note.md'    durable=True  side_effecting=True  dry_run_refusal=REFUSED plan_gate_governs=True
  'memories/note.md'     durable=False side_effecting=False dry_run_refusal=ALLOWED plan_gate_governs=False
  '/./memories/note.md'  durable=False side_effecting=False dry_run_refusal=ALLOWED plan_gate_governs=False
  ```

  The real middleware, with a handler that reports whether the tool body ran:

  ```
  /memories/note.md -> REFUSED: DryRunRefusal DRY RUN — write_file changes stored data or starts work, …
  memories/note.md  -> TOOL BODY RAN for memories/note.md
  ```

  And where the un-gated spelling actually lands (real `CompositeBackend` + `StoreBackend`):

  ```
  raw: memories/note.md -> validated: /memories/note.md
  write result: WriteResult(error=None, path='/memories/note.md')
  store contents: [(('memories', 'hash-of-actor'), '/note.md')]
  ```

  `validate_path` rejects `..` (`Path traversal not allowed`), so traversal is not the vector — plain
  relative and `/./` spellings are, and both are things a model emits unprompted.

  The docstring at `authz.py:213` argues the opposite property for a neighbouring case: *"treating an
  unreadable argument as the ungated case is how a gate becomes bypassable by malformed input."*
  The same sentence applies to a perfectly readable argument that has not been normalized, and the
  code does not do it. `tests/test_upstream_surface.py:420` pins only that the parameter is still
  called `file_path`; nothing tests a non-canonical value.

  A second, smaller case of the same defect: `file_path="/memories"` (no trailing slash) routes to
  the `StoreBackend` with `backend_path="/"` while `startswith("/memories/")` is `False`. That one is
  independently blocked today by `filesystem_permissions()` (`_check_fs_permission(..., "write",
  "/memories") == "deny"`), so it is latent rather than live — but it is the same missing step.

- **Fix**: normalize before deciding. In `writes_durable_memory`, run the argument through the same
  function the tool will use and compare the result:

  ```python
  from deepagents.middleware.filesystem import validate_path
  ...
  path = arguments.get("file_path")
  if not isinstance(path, str):
      return True
  try:
      resolved = validate_path(path)
  except ValueError:
      return True          # the tool will reject it; refusing is the safe direction
  return resolved == MEMORY_ROOT.rstrip("/") or resolved.startswith(MEMORY_ROOT)
  ```

  and add the normalization to `tests/test_upstream_surface.py` beside the parameter-name assertion,
  since `validate_path` is upstream's and a change in it silently moves this gate.

---

## A one-shot plan approval is never spent when the model empties its todo list, and re-authorizes every later turn that re-proposes the same plan

- **Severity**: medium
- **Location**: `src/chemclaw/agent/plan_gate.py:181` (`consume_turn_approval`), specifically
  `plan_gate.py:226-228`
- **Trigger**: a human approves plan A for session S; during that turn the model calls
  `write_todos([])` — which upstream's own tool description invites: *"Remove tasks that are no
  longer relevant from the list entirely"* — so the session's checkpointed `todos` is empty when the
  turn ends. `consume_turn_approval` computes `plan_identity([]) is None` and `return`s **without
  spending the approval**. Any later turn in session S that proposes the byte-identical plan A
  (`plan_identity` hashes only the todo `content` strings, so re-marking statuses does not change it)
  meets a live approval.
- **Consequence**: D-167's one-turn limit is void for that session. One human click authorizes an
  unbounded number of later state-changing turns — the DARK-1 shape this module exists to prevent,
  reached by a different door. Note the asymmetry the module itself calls out for the `None` case
  (`plan_gate.py:218-224` logs and counts an *unreadable* plan precisely because leaving an approval
  live "is the direction this must never fail in") — the empty-plan branch two lines below fails in
  exactly that direction, silently, with no log and no metric.
- **Evidence** (script against the real `InMemoryPlanApprovalStore` and the real `plan_gate`
  functions, with `session_todos` standing in for the checkpointer read):

  ```
  human approved plan A: 153fe194e102d420
  turn 1: approval_stands = True
  after consume_turn_approval with an emptied plan: approval still live = True
  turn N: approval_stands for the re-proposed plan A = True
  contrast, plan intact at turn end -> approval spent: False
  ```

  The last line is the control: with the plan still present at turn end, consumption works. It is
  only the empty list that leaks.

- **Fix**: `consume_turn_approval` must spend *the approval the turn ran under*, not the approval of
  whatever plan happens to be in the checkpoint afterwards. The cheapest correct version is to record
  the plan hash the gate actually matched (the gate already computes it in
  `enforce_plan_approval`) in a turn-local ambient, and consume that; failing that, consume every
  live approval for the session at turn end. At minimum the `plan_hash is None` branch must log and
  increment `chemclaw_plan_unreadable_total`'s sibling rather than returning silently, so the leak is
  visible.

---

## `NarrowedSkillsBackend.grep`/`glob` drop `truncated` and filter *after* `max_count`, so a permitted skill's matches can come back as "no matches, search complete"

- **Severity**: medium
- **Location**: `src/chemclaw/agent/skill_backend.py:113` (`glob`) and `:120` (`grep`)
- **Trigger**: a `grep` (or `glob`) over the skills tree where a *role-gated* skill contains enough
  matches to consume `max_count` before the permitted skill's matches are reached. `max_count` is not
  hypothetical: `FilesystemMiddleware.__init__` defaults `grep_max_count=1000` and
  `CompositeBackend.agrep` hands each route a *remaining* budget, so the cap is always in play.
- **Consequence**: two separate wrong answers.
  1. The permitted skill's matches are lost — the base backend spent the cap on files this turn may
     not see, and the override removes them afterwards.
  2. The reconstructed result drops `truncated`, so the caller is told the search **completed** and
     found nothing. `truncated` is the one field that would let the model or `CompositeBackend`'s
     `truncated = truncated or …` merge know to retry with a wider cap.
- **Evidence** (`/tmp/sk2` with a forbidden `alpha/SKILL.md` holding three `needle` lines and a
  permitted `beta/SKILL.md` holding one):

  ```
  ungated grep, no cap: [alpha:1, alpha:2, alpha:3, beta:1]
  gated  grep, no cap: [beta:1]                       <- correct

  cap=1 GrepResult(error=None, matches=[], truncated=False)
  cap=2 GrepResult(error=None, matches=[], truncated=False)
  cap=3 GrepResult(error=None, matches=[], truncated=False)
  cap=4 GrepResult(error=None, matches=[{'path': '/beta/SKILL.md', 'line': 1, …}], truncated=False)
  ```

  and the base backend for the same call, showing the flag that is being discarded:

  ```
  base cap=1: GrepResult(error=None, matches=[…alpha…], truncated=True)
  ```

  The docstring at `skill_backend.py:132-136` asserts the opposite: *"Filtering after the fact is
  still correct with a cap in play — `max_count` bounds what the tree returns, and this gate only
  ever removes from that."* The measurement above is the counter-example: the cap bounds the *tree*,
  not the *permitted* tree, and what the gate removes is the only thing the caller was entitled to.

- **Fix**: carry the flag through in both methods —
  `GrepResult(error=result.error, matches=…, truncated=result.truncated)` and likewise for
  `GlobResult` — and stop applying the cap upstream of the narrowing: pass `max_count=None` down to
  `super().grep(...)` and apply the caller's cap to the *filtered* list (`kept[:max_count]`,
  `truncated = result.truncated or len(kept) > max_count`). `_apply_grep_max_count` in
  `deepagents.backends.protocol` is the exact three-line shape to mirror.

---

## `_SKILL_READ_LIMIT` is dead, and the comment beside it claims a behaviour that does not exist

- **Severity**: low
- **Location**: `src/chemclaw/agent/skill_backend.py:201-204`
- **Trigger**: the model follows the skills prompt and calls `read_file("/<skill>/SKILL.md")` without
  passing `limit`.
- **Consequence**: it gets 100 lines, not 1000. Six shipped `SKILL.md` files are longer than 100
  lines (`computational-evidence` 157, `experiment-progression` 129, `ionization-and-partitioning`
  123, `conformational-analysis` 113, `deep-research` 105, `product-prediction` 102), so the judgment
  the model is handed is a truncated fragment unless it happens to pass the argument. The comment
  states: *"The default lives here rather than in the model's hands so a skill is not silently
  truncated when the model forgets."* It does not — nothing reads the constant.
- **Evidence**:

  ```
  $ grep -rn "_SKILL_READ_LIMIT" --include=*.py --include=*.md .   # excluding .venv
  ./src/chemclaw/agent/skill_backend.py:204:_SKILL_READ_LIMIT = 1000
  ```

  and the read itself:

  ```
  tool default limit = 100   module constant = 1000
  total_lines = 157  end_line = 100  next_offset = 100
  ```

  (`SKILL_READ_TOOL` on line 198 is likewise referenced only by `tests/test_skill_backend.py`, but
  that one is a genuine pin against upstream's prompt, so it earns its place.)

  Mitigation that keeps this at *low*: upstream's read tool appends pagination metadata, so a model
  can notice and re-read; and deepagents' own `SKILLS_SYSTEM_PROMPT` tells it to pass `limit=1000`.
  The defect is that the module claims to have removed that dependence on the model and has not.

- **Fix**: either bind it — pass `_SKILL_READ_LIMIT` as the `read` override's default
  (`def read(self, file_path, offset=0, limit=_SKILL_READ_LIMIT)`), which is what the comment
  describes — or delete the constant and the paragraph. Binding it is the smaller change and matches
  the stated intent; a test that reads a >100-line `SKILL.md` through the backend's default and
  asserts `end_line == total_lines` would hold it.

---

## Checked and found sound (recorded so these are not re-audited)

- **The `cancelled` audit row really is written from inside a cancellation.** `_emit_shielded`'s
  `asyncio.shield` claim (`audit.py:447`) holds: a task cancelled mid-tool leaves a durable row.
  Measured against the live Postgres —
  `cancel-probe-1 | slow_tool | cancelled | the turn was torn down while this tool w…`.
  The `audit_events` schema carries every one of the twelve columns `audit_store._INSERT` names.
- **The async twins of the skills backend really do dispatch through the subclass.** `als`, `aread`,
  `aglob`, `agrep` and `adownload_files` all honour the narrowing, and `awrite`/`aedit`/`adelete`/
  `aupload_files` all raise `PermissionError`, as `skill_backend.py:18-22` claims.
- **The governance chain's nesting is what its comments say.** Outermost→innermost is
  `surface_authorization_denials, surface_domain_errors, announce_tool_failures, audit, [refuse_undeclared_writes], enforce_tool_authz, refuse_writes_on_dry_run, refuse_repeated_calls, [enforce_plan_approval]`
  (`langgraph_agent.tool_governance_middleware`). `audit` therefore sits **inside**
  `surface_domain_errors`, so `answered_failure`'s `status="success"` rewrite cannot reach the audit
  row — a returned MCP failure is still booked `outcome="error"`. That was the ordering most worth
  disproving and it is correct.
