# Verdicts: sweep-authz (lens: is the trigger reachable, and is the consequence what is claimed?)

In scope: the two findings marked **critical** and **high**. The third finding is **medium** and the
two closing sections are negative results — all out of scope, not examined.

Working tree checked against `/tmp/.../scratchpad/pristine`: `diff -rq pristine/src ./src` reports
only `__pycache__` directories. No mutation markers, no missing guards. Everything below is against
unmodified `HEAD`.

Scripts: `/tmp/vf/repro_jobs.py`, `/tmp/vf/repro_agent.py`, `/tmp/vf/repro_expensive.py`.
Postgres was up (`pg_isready` → accepting connections); Temporal was up (`job_status` reached it and
took the `NOT_FOUND` → record branch rather than raising `SubsystemUnavailableError`).

---

## Any authenticated principal can read every other principal's durable job — inputs, results, molecule structures

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the finding says critical — one notch too high)

- **What I did**

  Wrote a `job_records` row attributed to `alice-oid` into a real Postgres (isolation schema
  `vf_authz`, full `migrate()`), then drove the real `create_app()` with `entra_required=True`,
  `entra_privileged_roles="chem-admin"` and `require_principal` overridden to a role-less
  `Principal(oid="mallory-oid")` (`/tmp/vf/repro_jobs.py`):

  ```
  wrote record requested_by=alice-oid
  GET /jobs -> 200 [{'job_id': 'qm-compute_dft_energy-AUDITALICE', 'connector': 'qm',
    'job': 'compute_dft_energy',
    'rationale': 'Project VULCAN: screen the undisclosed lead series before the patent filing',
    'summary': 'lead 42 barrier 18.3 kcal/mol', 'note_id': '', 'completed_at': ...}]
  GET /jobs/{id} -> 200 {'job_id': 'qm-compute_dft_energy-AUDITALICE', 'status': 'completed',
    'summary': 'lead 42 barrier 18.3 kcal/mol',
    'result': {'geometry': 'SECRET 3D COORDS', 'candidates': ['CC-lead-42'],
               'energy_hartree': -1234.5678},
    'rationale': 'Project VULCAN: screen the undisclosed lead series before the patent filing'}
  ```

  Then the same read through the agent's own tools, ambient identity `mallory-oid` / role `chemist`
  (`/tmp/vf/repro_agent.py`):

  ```
  authorize_tool(find_past_jobs): ALLOWED
  authorize_tool(get_durable_job_status): ALLOWED
  find_past_jobs -> [{... 'rationale': '<retrieved-note-...>Project VULCAN: screen the undisclosed
                      lead series before the patent filing</retrieved-note-...>', ...}]
  get_durable_job_status -> {'status': 'completed', 'result': {'geometry': 'SECRET 3D COORDS', ...},
                             'rationale': 'Project VULCAN: ...'}
  ```

  Reachability trace, outermost inward. `deploy/helm/chemclaw/values.yaml:341` sets
  `CHEMCLAW_SESSION_STORE: "postgres"` and `:382` sets `CHEMCLAW_ENTRA_REQUIRED: "true"` — i.e. the
  **shipped chart is exactly the finding's stated trigger configuration**, with no extra operator
  action needed. Upstream of the handler there is `require_principal` (signature/audience/issuer/exp
  validation + the per-principal rate budget) and nothing else: `get_job` takes `principal:
  CurrentUser` and never reads it. `grep -rn "requested_by" src/` shows the column is read by exactly
  one non-writer, `agent/leaver.py:225` (the data-subject export) — never on a read path. So the
  trigger is "hold any valid tenant token", which is the weakest possible precondition short of
  anonymous.

  Both branches of `job_status` leak equally, so this does not depend on history having aged out:
  with the workflow still in Temporal it returns `completed_job_status(job_id, await
  handle.result())`; with it expired it returns `_recorded_status`. I exercised the second.

- **Why**

  Mechanism, trigger and headline consequence all reproduce verbatim. **Three supporting claims do
  not hold, and one thing the reporter missed makes it worse.**

  *Does not hold — the report path.* "This also **invalidates the stated defence of the report
  path**" is false. `grep -rn "record_job\b\|JobRecord(" src/` gives exactly one writer:
  `durable/connector_job.py:230/348/399`, inside `ConnectorJobWorkflow`. `DevelopmentReportWorkflow`
  writes no `job_records` row (`durable/report_workflow.py` contains no `record_job` call). So no
  report id is ever published by `GET /jobs` or `find_past_jobs`, and `_report_id`'s per-actor key
  still makes a report id underivable by a second principal. The mitigation `durable_tools.py:118-140`
  argues for is **intact**; the paragraph attacking it is arguing about a class of job that never
  relied on unguessability in the first place.

  *Does not hold — the exposed field set.* The evidence block prints `requested_by`, `session_id` and
  `payload` under the heading "Same read through the agent's own tools". No tool returns any of them.
  `find_past_jobs` returns `JobRecordSummary` (`job_id, connector, job, rationale, summary, note_id,
  completed_at` — `job_record.py:77-95`); `get_durable_job_status` returns `DurableJobStatus`
  (`job_id, status, summary, result, rationale` — `durable_tools.py:66-85`). `lookup_job_record`, the
  only reader of the whole `JobRecord`, has exactly one caller — `_recorded_status`, which drops
  `payload`, `requested_by`, `session_id` and `correlation_id` on the floor. My run above confirms it:
  those three keys are absent from both tool results. So the title's "**inputs**" is not exposed as
  such — the launch arguments never leave the database.

  *Does not hold — the word.* "Full **cross-tenant** read" is cross-*principal* inside one tenant.
  `entra_audience`/`entra_issuer` are single-tenant settings; there is no second tenant to cross.

  *Missed, and it cuts the other way.* The finding gives up "inputs" for nothing, because the
  identifying inputs come back inside `result` anyway. `connectors/qm/specs.py:96-110` — `QMJobResult`
  carries `molecule_smiles`, `method`, `basis_set` **and `requested_by`**; `science/calc/models.py:173`
  — `XtbResult` carries `smiles`. `job_record_for` stores `result.data` whole
  (`connector_job.py:239-241`). So for a real `qm` run, `GET /jobs/{id}` hands a stranger the
  structure *and the launching principal's oid*, and for `bo` it hands over `CampaignResult.history`
  — every `Observation.params` the campaign evaluated. The title is right by a route the finding
  does not name.

  *Why high rather than critical.* Read-only; no privilege escalation; no write; nothing anonymous —
  the caller must already hold a valid tenant token, so the population is "every ChemClaw user",
  not "the internet". And the *listing* half is a deliberate, argued position the finding itself
  accepts and proposes no change to. What is genuinely un-argued is the detail read: `get_job`'s own
  docstring makes no unscoped claim, `JobRecordSummary` exists specifically to keep the result blob
  out of a listing, and `deps.py`'s blanket sentence ("nothing about the answer depends on *which*
  caller it is") is simply untrue of a route whose body is another named principal's converged
  geometry. That is a real defect on the shipped chart, reachable in one GET, and also reachable by
  the model unprompted — `find_past_jobs`'s docstring instructs the chain. High, not critical.

---

## `entra_expensive_actions` is inert: an operator-gated connector job launches for any authenticated user

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium (the finding says high — too high)

- **What I did**

  `/tmp/vf/repro_expensive.py`, with `entra_required=True`, `entra_privileged_roles="chem-admin"`,
  `entra_expensive_actions="compute_reaction_energy"`, ambient identity `bob-oid` / role `chemist`,
  and the real `calc` manifest's `JobSpec` and params model:

  ```
  expensive_actions() = ['compute_dft_energy', 'compute_interaction_energy',
                         'compute_reaction_energy', 'request_development_report',
                         'sample_conformers', 'start_optimization_campaign']
  authorize_trigger(compute_reaction_energy): REFUSED — user bob-oid lacks a privileged role for compute_reaction_energy
  manifest expensive flag: False
  prepare_job_launch: LAUNCH ALLOWED, payload keys: ['kind', 'level', 'products', 'reactants']
  authorize_tool: ALLOWED
  ```

  Every clause of the finding checks out: the action is in the gate's set, the gate refuses it, the
  only production caller never asks, and no other gate compensates.

  Call-site census — `grep -rn "authorize_trigger" src/` (excluding `authz.py` itself and docstrings)
  gives **two** live call sites: `durable_tools.py:183` (`request_development_report`, unconditional)
  and `connectors/jobs.py:303` (guarded by `if job.expensive:`). `template_activities.py:186` calls
  `prepare_job_launch`, so it inherits the same guard rather than being a third site.

  Enabled jobs and their flags, dumped from the live registry:

  ```
  bo       start_optimization_campaign      expensive=True
  calc     compute_reaction_energy          expensive=False
  calc     compare_solvents                 expensive=False
  calc     scan_coordinate                  expensive=False
  calc     sample_conformers                expensive=True
  calc     compute_interaction_energy       expensive=True
  qm       compute_dft_energy               expensive=True
  ```

  Helm: `grep -n "ENTRA_EXPENSIVE\|ENTRA_PRIVILEGED" deploy/helm/chemclaw/values.yaml` →
  `CHEMCLAW_ENTRA_PRIVILEGED_ROLES: ""` and **no** `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` key at all.

- **Why**

  Confirmed, and **stronger than filed**. The finding says the knob is enforced for "no connector job
  it does not already cover via the manifest". With only two call sites in existence, the setting is
  *entirely* without effect: every name an operator can put in it is either `request_development_report`
  (already in `CORE_EXPENSIVE_ACTIONS`, so redundant), or a job the manifest already declares
  `expensive: true` (already in `declared`, so redundant), or anything else — never asked. There is no
  string for which `entra_expensive_actions` changes an outcome. `authorize_trigger` returning early
  for a non-expensive action (`authz.py:370`) makes `if job.expensive:` a strictly narrower duplicate
  of a check the callee already makes, which is exactly the divergence shape the finding names.

  *Why medium rather than high.* Three things bound it.

  1. **The trigger is not a default.** `values.yaml` sets neither knob, and the config validator
     (`entra.py:166`) makes the two mandatory as a pair, so reaching this needs an operator to set
     both deliberately. On the shipped chart (`entra_privileged_roles: ""`) `authorize_trigger` fails
     closed on the empty role set for *every* expensive action, so the hole cannot be stumbled into —
     it only opens once an operator has configured the gate and believes it is running.
  2. **The consequence is initiation of bounded compute, not data access.** Given today's manifests
     the uncovered set is exactly three `calc` jobs — `compute_reaction_energy`, `compare_solvents`,
     `scan_coordinate` — all `inline_wait_seconds: 20` xTB work. The genuinely unbounded things
     (CREST searches, DFT, BO campaigns) are `expensive: true` and *are* gated, and
     `compute_dft_energy` is additionally in `DEFAULT_WRITE_TOOL_GATES`. Nothing is read, written or
     disclosed; a chemist starts a cheap calculation they were meant to be refused.
  3. **A working equivalent control exists and is live.** `tool_role_gates` gates the same tool name
     by role and is enforced (the sweep's own third finding measures it refusing
     `start_optimization_campaign`), so the operator's intent is achievable today by another knob.

  What keeps it at medium rather than low is the failure *direction* and the silence: a security
  setting that the docs describe, the validator demands a companion for, and which does nothing —
  precisely the "a control that reads as a control and is not one" shape. The fix the finding
  proposes (drop the `if job.expensive:` condition) is correct and cannot loosen anything, since
  `authorize_trigger` already no-ops outside `expensive_actions()`.
