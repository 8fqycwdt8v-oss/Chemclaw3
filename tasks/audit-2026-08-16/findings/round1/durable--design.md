# `src/chemclaw/durable/` — design & simplification (round 1)

Lens: structure that costs more than it buys. All 25 modules in the slice were read in full.
Postgres and Temporal were up (`infra-postgres-1`, `infra-temporal-1`); three findings below carry
a script and its output.

---

## The registry removed the worker's workflow list and left its import list, and the test that claims to guard it does not

- **Severity**: medium
- **Location**: `src/chemclaw/durable/background_worker.py:31-54`; `src/chemclaw/durable/registry.py:91-129`; `tests/test_workflow_registry.py:24-34`
- **Trigger**: Delete any one line from the `# noqa: F401` import block in `background_worker.py` — e.g. `from chemclaw.durable import digest as _digest`.
- **Consequence**: `DigestWorkflow` and its two activities are no longer registered, so the background worker never advertises them; a `digest` Schedule fires a workflow no worker serves and the run sits in the queue forever. That is verbatim the failure `registry.py:101-104` says the registry exists to prevent ("a workflow that is written, tested and imported but missing from the worker's list is a workflow that never runs, and nothing fails until someone submits one"). The registry moved the hand-maintained list from 16 class names to 16 import lines — same cardinality, same edit on adding a module, same silent failure — while `durable/README.md` advertises "Adding a durable capability does not mean editing the worker."
- **Evidence**: `BACKGROUND_WORKFLOWS = registered_workflows("background")` is evaluated *at `background_worker` import time*, and the guard asserts that snapshot against the registry:

  ```python
  # tests/test_workflow_registry.py:29-34
  """Importing the worker module is what registers its capabilities, so this also
  proves the imports are still there — the one thing adding a workflow to a *new*
  module still requires."""
  assert BACKGROUND_WORKFLOWS == registered_workflows("background")
  ```

  A dropped import removes the module from *both* sides of the equality, so the assertion is
  vacuous with respect to the thing its docstring claims it proves. Measured:

  ```
  $ sed -i 's|^from chemclaw.durable import digest as _digest  # noqa: F401$|# REMOVED FOR PROBE|' \
        src/chemclaw/durable/background_worker.py
  $ uv run pytest tests/test_workflow_registry.py::test_every_declared_capability_reaches_its_worker -q
  .                                                                        [100%]
  1 passed in 0.64s
  ```

  (The file was restored afterwards; `git status` is clean of it.)
- **Fix**: Make the import list derivable rather than hand-written, and make the guard non-vacuous.
  Concretely: replace the block with a `pkgutil.iter_modules(chemclaw.durable.__path__)` walk that
  imports every sibling module (the package holds nothing but durable capabilities and their
  helpers, and `registry._claim` already tolerates a re-import), or — if an explicit list is wanted —
  assert in the test that the set of `durable/*.py` modules defining `@durable_workflow`/
  `@durable_activity` equals the set the worker imports, computed by AST rather than by import.
  Behaviour-preserving either way; the second is the smaller change.

---

## `chemclaw.agent` and `chemclaw.durable` are mutually dependent, worked around by three function-level imports of a private symbol

- **Severity**: medium
- **Location**: `src/chemclaw/durable/template_activities.py:35-50` (`_agent_surface`), `:384` (`run_agent_step`), `:477-478` (`step_profile`); the other direction at `src/chemclaw/templates/registry.py:33`
- **Trigger**: Move any one of those three deferred imports to module scope, then `import chemclaw.agent.chemclaw_agent`.
- **Consequence**: `ImportError` — the packages form a cycle. `chemclaw.agent.chemclaw_agent` →
  `chemclaw.templates.registry` → `chemclaw.durable.template_job` → `chemclaw.durable.template_activities`
  → `chemclaw.agent.chemclaw_agent`. The cost is paid three times over in deferred imports, plus
  `_agent_surface()` being typed `-> Any` (it returns a 2-tuple of callables, so every caller loses
  type checking on the tool surface), plus the durable layer reaching for a **private** member of
  another package: `from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs`.
- **Evidence**:

  ```
  $ # module-level import added at template_activities.py:30, then:
  $ uv run python -c "import chemclaw.agent.chemclaw_agent"
    File ".../chemclaw/durable/template_job.py", line 39, in <module>
      from chemclaw.durable.template_activities import (
    File ".../chemclaw/durable/template_activities.py", line 30, in <module>
      from chemclaw.agent.chemclaw_agent import _capability_tools, connector_specs  # PROBE
  ImportError: cannot import name '_capability_tools' from partially initialized module
  'chemclaw.agent.chemclaw_agent' (most likely due to a circular import)
  ```

  Note that `template_activities` already imports three `chemclaw.agent` modules at module scope
  (`profiles`, `state`, `tool_invocation`, lines 23-25), so the layering intent is already gone —
  only the cycle-closing imports are deferred.
- **Fix**: Break the cycle at its narrow point rather than at three call sites. The only thing
  `templates/registry.py` needs from `durable/template_job.py` is `TemplateRunInput` and the
  workflow *type name*; the only thing `template_activities` needs from `chemclaw_agent` is
  "the assembled tool surface". Extract the surface assembly (`_capability_tools`,
  `connector_specs`, `advertised_tool_names`) into a `chemclaw.agent.surface` module that imports
  neither templates nor durable, and import it eagerly from both sides. That deletes
  `_agent_surface()`, restores real types, and removes the private-symbol reach.
  Behaviour-preserving (pure move).

---

## `retention._PRUNABLE` carries dead payload for half its rows, and the sweep dispatches on hardcoded table names

- **Severity**: medium
- **Location**: `src/chemclaw/durable/retention.py:121-126` (`_PRUNABLE`), `:190-197` (`_window_days`), `:226-258` (`prune_expired_rows`)
- **Trigger**: Read any value of `_PRUNABLE["session_messages"]` or `_PRUNABLE["checkpoints"]`.
- **Consequence**: Two of the four entries in the dispatch table are `continue`d past before their
  `(column, disposable)` payload is ever used — `session_messages` at line 231, `checkpoints` at
  line 240 — so those tuples are decoration. The real predicates for those two tables are hardcoded
  elsewhere: `created_at` is re-spelled in `_EXPIRED_SESSIONS`/`_EXPIRED_IDS` (`:152-167`) and
  `(checkpoint->>'ts')::timestamptz` is re-spelled in `_EXPIRED_THREADS` (`:138-142`). A reader
  changing the column in the table changes nothing, and the module docstring's own account of
  `_PRUNABLE` as "the timestamp column that dates a row and the extra predicate that decides
  whether a row of that table is disposable" is true of two entries out of four. On top of that,
  `_window_days` is a *second* dict keyed on the same four strings, rebuilt on every call, and a
  fifth table added to `_PRUNABLE` alone raises `KeyError` rather than being caught by a type.
  The loop is also silently order-coupled: `_prune_checkpoints`'s docstring (`:350-352`) argues its
  loud-failure behaviour is safe because "`checkpoints` is last in `_PRUNABLE`", i.e. correctness
  rests on dict literal ordering with nothing asserting it.
- **Evidence**: Replacing both entries' payloads with SQL that cannot execute changes nothing.
  Against the live database (schema-isolated, one expired `session_messages` row seeded):

  ```python
  _PRUNABLE = {
      "session_events":     ("created_at", "consumed_at IS NOT NULL"),
      "session_messages":   ("NO_SUCH_COLUMN", "1 = 'not sql'"),
      "tool_result_blobs":  ("created_at", "TRUE"),
      "checkpoints":        ("NO_SUCH_COLUMN_EITHER", "@@@"),
  }
  ```

  ```
  SHIPPED  : {'session_messages': 1, 'checkpoints': 0, 'checkpoint_blobs': 0, 'checkpoint_writes': 0,
              'skipped': ['session_events (retention disabled)', 'tool_result_blobs (retention disabled)']}
  CORRUPTED: {'session_messages': 1, 'checkpoints': 0, 'checkpoint_blobs': 0, 'checkpoint_writes': 0,
              'skipped': ['session_events (retention disabled)', 'tool_result_blobs (retention disabled)']}
  1 passed
  ```
- **Fix**: Make the register hold what each table actually needs and nothing it does not — one
  frozen dataclass per table with `window: Callable[[], int]` and a `prune: Callable[[conn, days], ...]`,
  so the plain-cutoff tables share one pruner and `session_messages`/`checkpoints` name their own.
  That deletes `_window_days` (the window moves onto the entry), deletes the two dead tuples,
  deletes both `if table == "..."` branches, and makes the ordering explicit as a list rather than
  an accident of a dict literal. Behaviour-preserving; `tests/test_retention.py:48` and
  `tests/test_database_privileges.py:62` iterate the register's *keys*, which the replacement keeps.

---

## `connector_job._notify_failure` is documented as "never raising" and can raise, while its twin in `template_job` guards against exactly that

- **Severity**: medium
- **Location**: `src/chemclaw/durable/connector_job.py:323-342`; the twin at `src/chemclaw/durable/template_job.py:149-169`; the shared helper at `src/chemclaw/durable/notify.py:94-111`
- **Trigger**: A `ConnectorJobWorkflow` whose child fails, where the subsequent push-back raises
  anything that is not a `temporalio.exceptions.ActivityError` — a cancellation reaching the
  `execute_activity` on the way out, a data-converter error, a worker-side `RuntimeError`.
- **Consequence**: The exception from `_notify_failure` replaces the child's `ChildWorkflowError`,
  so the workflow fails with the push-back's reason instead of the job's. That is precisely the
  outcome `template_job._notify_failure`'s docstring names ("an exception here would replace the
  original failure with a push-back error and lose the reason entirely") and guards with
  `contextlib.suppress(Exception)` — and `connector_job`'s copy, which the template's own comment
  cites as the shape it is following (`template_job.py:125-126`), has no such guard while asserting
  "Best-effort and never raising" (`connector_job.py:326`). Two copies of one decision, drifted, with
  the copy that claims the stronger property being the one that lacks it.
- **Evidence**: `notify_session_best_effort` catches one type only:

  ```python
  # durable/notify.py:107-110
      except ActivityError:
          workflow.logger.warning("session push-back failed for %s", session_id)
          return False
  ```

  Run with `workflow.execute_activity` stubbed to raise a non-`ActivityError`
  (`/tmp/claude-0/probe/notify_probe.py`):

  ```
  RAISED OUT OF 'best effort, never raising': RuntimeError data converter refused / worker gone /
  anything not an ActivityError
  ```
- **Fix**: One helper instead of two methods. Put
  `async def notify_failure(session_id: str, exc: BaseException, **fields: str) -> None` in
  `durable/notify.py`, doing the `if not session_id: return`, the `failure_reason(exc)` and the
  `contextlib.suppress(Exception)` once; `connector_job` calls it with `connector=`/`job=`,
  `template_job` with `template=`/`step=`. Behaviour-preserving for `template_job`; for
  `connector_job` it changes behaviour in exactly the direction its own docstring already claims.

---

## The "stamp the actor, or don't" block is written three times, with its body duplicated each time

- **Severity**: low
- **Location**: `src/chemclaw/durable/memory_jobs.py:153-163`; `src/chemclaw/durable/report_workflow.py:74-80` and `:97-103`; the unconditional form three more times at `src/chemclaw/durable/template_activities.py:180-189`, `:260-266`, `:388-411`
- **Trigger**: Reading or changing any of the six.
- **Consequence**: Each of the three conditional sites writes its work expression **twice**,
  verbatim, once per branch:

  ```python
  # report_workflow.py:74-80
  if not request.requested_by:
      return await gather_section(request.section, default_retrievers())
  token = set_current_identity(request.requested_by, frozenset(request.requested_roles))
  try:
      return await gather_section(request.section, default_retrievers())
  finally:
      reset_current_identity(token)
  ```

  The same six lines appear at `report_workflow.py:97-103` around `propose_note(report_note(report),
  default_submitter())` and at `memory_jobs.py:153-163` around `propose_note(note,
  default_submitter(), dependencies=compound_dependencies(note))`. The blank-actor branch is not
  cosmetic — `None` and `""` are genuinely distinguished downstream
  (`ingest/documents/retriever.py:121` `if get_current_actor() is None`, `agent/authz.py:373,408`,
  `connectors/identity.py:106`) — which is exactly why the rule deserves one implementation rather
  than three transcriptions. A fourth site that forgets the guard would stamp an actor of `""`, i.e.
  "authenticated as nobody", silently.
- **Fix**: Add to `core/identity_context.py`:

  ```python
  @contextmanager
  def acting_as(actor: str, roles: frozenset[str] = frozenset()) -> Iterator[None]:
      """Stamp the ambient identity for this block; a blank actor stamps nothing."""
      if not actor:
          yield
          return
      token = set_current_identity(actor, roles)
      try:
          yield
      finally:
          reset_current_identity(token)
  ```

  All six sites become `with acting_as(...):` around a single copy of the body. Behaviour-preserving
  (the three `template_activities` sites take `StepIdentity.actor`, which is `Field(min_length=1)`,
  so the blank branch is unreachable there).

---

## `fan_out` carries two parameters no caller has ever passed

- **Severity**: low
- **Location**: `src/chemclaw/durable/orchestrator.py:86-87` (`task_queue`, `retry_policy`) and the ~20 lines of docstring at `:120-133` justifying their defaults
- **Trigger**: `grep -rn "fan_out(" src/ tests/`.
- **Consequence**: Three call sites exist in the whole repository —
  `memory_jobs.py:262`, `report_workflow.py:167`, and `tests/test_orchestrator.py:67` — and none
  passes `task_queue` or `retry_policy`. Both are `None`-defaulted options resolved at
  `orchestrator.py:135` and `:139` to a value that is the only one ever used, and the docstring for
  `retry_policy` alone runs eight lines explaining a default nobody overrides. `_run_child`
  (`:56-83`) exists solely to receive them. This is the repo's own "no abstraction without a second
  real caller" and "delete dead params on sight" rules applied to its own generic helper.
- **Fix**: Delete both parameters; read `settings.background_task_queue` and use `BAD_DATA_RETRY`
  directly inside `fan_out`, and inline `_run_child` (its remaining arguments are all in scope).
  `max_parallel` stays — `tests/test_orchestrator.py:67` passes it, and it is the one knob that
  changes the emitted command count. Behaviour-preserving; the ~30 lines of parameter documentation
  collapse to the two sentences that are still true.

---

## `document_sync._document_index` is an indirection for tests that no test uses, justified by a comment that is false

- **Severity**: low
- **Location**: `src/chemclaw/durable/document_sync.py:56-57`, used at `:129`, `:170`, `:192`, `:202`
- **Trigger**: `grep -rn "_document_index" tests/`.
- **Consequence**: The module-global alias

  ```python
  # Module-level indirection so tests swap the Postgres backend for the in-memory one.
  _document_index = default_document_index
  ```

  has no consumer that uses it as an indirection. No test in `tests/` patches
  `document_sync._document_index` (the only `default_document_index` swaps are in
  `tests/test_sync_share_cli.py:148,227`, against `chemclaw.cli.sync_share`). Four call sites pay a
  module-global lookup and a reader pays "why is this indirected?" for a rebinding nobody performs;
  the comment asserting the reason is contradicted by the test tree. Contrast `eln_sync.py:44-45`,
  whose identical-looking `_reaction_store`/`_molecule_store` aliases *are* swapped
  (`tests/test_eln_workflow.py:48-49`) — so this is the one of the pair that is dead, which is
  precisely what makes the shared comment misleading.
- **Fix**: Delete the alias and call `default_document_index()` at the four sites, or — if the
  in-memory backend is wanted in a future test — reach it the way the CLI tests already do, by
  patching the imported name. Behaviour-preserving.

---

## `connector_job.py` is the shared durable-job module under a connector-specific name

- **Severity**: low
- **Location**: `src/chemclaw/durable/connector_job.py:62-95` (`failure_reason`), `:139-153` (`ConnectorJobResult`), `:156-186` (`envelope_from_result`), `:189-214` (`child_workflow_id`), `:217-245` (`job_record_for`)
- **Trigger**: Read `DevelopmentReportWorkflow.run`'s return type, or `TemplateWorkflow._run_job_step`'s helpers.
- **Consequence**: Four of the module's five module-level helpers plus its result model are general
  durable-job machinery, not connector machinery, and their non-connector callers say so out loud:
  `report_workflow.py:294` returns `ConnectorJobResult` from a workflow whose own docstring has to
  spend six lines explaining "**It returns the connector envelope, though it is not a connector's
  workflow**" (`:302-308`); `template_job.py:32-37` imports `ConnectorJobInput`, `ConnectorJobResult`,
  `child_workflow_id` and `failure_reason`; `agent/job_results.py:41` and `agent/durable_tools.py:59`
  import two more. The name forces every non-connector user to write a paragraph disclaiming it,
  which is the cost a misleading name imposes.
- **Fix**: Split the module: keep `ConnectorJobInput`/`ConnectorJobWorkflow` in `connector_job.py`,
  and move `ConnectorJobResult` (renamed `DurableJobResult`, with the old name kept as an alias for
  one release since it crosses the Temporal wire as a *shape*, not a name), `envelope_from_result`,
  `failure_reason`, `child_workflow_id` and `job_record_for` into `durable/job_envelope.py`.
  Behaviour-preserving — none of these names is a Temporal workflow or activity type name, so no
  recorded history references them.

---

## Checked and found sound

Recorded so the absence of a finding is not read as an absence of a check.

- **Dead code.** Every public symbol in the slice was cross-referenced against `src/` and `tests/`.
  Everything with zero external references is reached by dynamic registration — `@durable_activity`
  /`@durable_workflow` into `registry._ACTIVITIES`/`_WORKFLOWS`, or `fan_out(ReportSectionWorkflow,
  …)` / `fan_out(PublishNoteWorkflow, …)` from within the defining module. The one genuinely
  internal-only public name is `registry.temporal_name` (used at `registry.py:189,213` and nowhere
  else); not worth a finding, but it should be `_temporal_name`.
- **Single-caller abstractions.** `heartbeat.beating` (5 callers), `publish.publish_note` (2),
  `job_record_store._connect` (3), `artifact_eviction._reclaimed` (2), `memory_jobs.all_reactions`
  (3) all clear the Rule of Three. `connector_job._record_run` and `digest._digest_channel` have one
  caller each and are named decisions rather than layers; inlining them would lose more than it saves.
- **The two chunked-drain loops** (`eln_sync.py:232-263`, `document_sync.py:253-296`) share only the
  no-cursor-advance guard sentence, verbatim in both; the surrounding loops differ materially
  (`continue_as_new`, per-source sweep, per-chunk cursor persistence). Extracting them would be
  premature — this is correctly two loops, not one clone.
- **`resolve_fan_out_limit` / `resolve_notes_per_run` / `DocumentSyncPlan.max_iterations`** are three
  instances of "record a settings int in history so the emitted command count is a function of
  history". They look like a clone; they are not usefully one function, because a shared activity
  would key its recorded result on one activity type across three unrelated bounds.
- **Config.** No hardcoded URL, path, threshold or timeout found in the slice; every bound comes
  from `settings`. The two fixed strings — `eval_drift.DRIFT_ALERT_CHANNEL` and
  `digest._digest_channel`'s `f"digest-{owner}"` — are internal channel identifiers, correctly not
  knobs.
- **Module-global state.** `background_worker.BACKGROUND_WORKFLOWS`/`BACKGROUND_ACTIVITIES` are
  import-time snapshots of the registry; that is the documented contract and the equality test does
  hold. `eln_sync._reaction_store`/`_molecule_store` are live test seams (see the
  `_document_index` finding for the one that is not).
