# Connectors seam — CORRECTNESS · reproduction verdicts

Scope: only the **critical/high** findings in
`tasks/audit-2026-08-16/findings/round1/connectors-seam--correctness.md`. That file contains
exactly one — the durable-launch idempotency key. The other five are medium/low and out of scope.

Working tree untouched (`git status --short` empty before and after). No source file was mutated.
All scripts under `/tmp/vprobe/`, written from the source rather than from the reporter's
transcript; none of the reporter's probes were run.

---

## The durable-launch idempotency key omits the calculator/pipeline version, so a completed pre-upgrade run is served as the current answer

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**:

  **1. Re-derived the key from the source, in three configurations** (`/tmp/vprobe/a.py`: loads the
  real `qm` manifest through `registry.discovered()`, runs the real `prepare_job_launch` and
  `job_workflow_id`, alongside `qm_job_key` and `cache.calculation_key`). Same launch arguments
  (`CCO` / `B3LYP` / `def2-SVP`) throughout:

  ```
  mock, pipeline ''          workflow_id qm-compute_dft_energy-29776d63ecaa48fb
                             qm_job_key 941c5e787b19328d   calc_version mock-unversioned
  nextflow, pipeline v2.3.0  workflow_id qm-compute_dft_energy-29776d63ecaa48fb   <-- identical
                             qm_job_key c5a67e881a0e96ad   calc_version nextflow-v2.3.0
                             store key   calc_version='nextflow-v2.3.0' params_hash='70d67bb42da152e4'
  nextflow, pipeline v2.4.0  workflow_id qm-compute_dft_energy-29776d63ecaa48fb   <-- identical
                             qm_job_key cfe3264294f6c05c   calc_version nextflow-v2.4.0
                             store key   calc_version='nextflow-v2.4.0' params_hash='62df4c8e14c4040c'
  ```

  My digest matches the reporter's (`29776d63ecaa48fb`) to the character. Note the third row: it is
  not only the mock→real transition — a plain pipeline bump between two *real* pipelines, which
  `Settings._hpc_launch_config` makes mandatory to declare, is equally invisible to the launch key.

  **2. Ran the whole thing end to end against the live broker** (`/tmp/vprobe/b2.py`,
  `/tmp/vprobe/vwf2.py`, bundle at `/tmp/vprobe/bundles2/`) — the *real* `build_job_tool` launch
  coroutine, the *real* `ConnectorJobWorkflow` on a core worker, a connector-owned child workflow on
  its own `bundle_queue`, real `start_workflow` with the launcher's own reuse policy. The bundle
  declares two jobs on one workflow: `slow_calc` (no `inline_wait_seconds`, the `qm` shape) and
  `fast_calc` (`inline_wait_seconds: 15`, the `calc` shape). The child workflow's answer depends on
  a `VPROBE_BACKEND` constant read at worker start — the stand-in for "which calculator this
  deployment points at": `mock` → `-1.111111`, `nextflow-v2.4.0` → `-76.402312`.

  Run 1, deployment on `mock`:

  ```
  [mock] slow_calc: workflow_id = vpcc7124-slow_calc-427c278ad06337b8
  [mock] slow_calc: tool returned -> 'vpcc7124-slow_calc-427c278ad06337b8'
     [job record] slow_calc
  [mock] slow_calc: polling that id yields -> energy -1.111111 computed by mock
  [mock] fast_calc: workflow_id = vpcc7124-fast_calc-5f6e5c389dbbef2f
     [job record] fast_calc
  [mock] fast_calc: tool returned -> ConnectorJobResult(summary='energy -1.111111 computed by mock',
                    data={'total_energy_hartree': -1.111111, 'backend': 'mock', ...})
  ```

  Run 2, deployment repointed to `nextflow-v2.4.0`, byte-identical launch arguments:

  ```
  [nextflow-v2.4.0] slow_calc: workflow_id = vpcc7124-slow_calc-427c278ad06337b8
  [nextflow-v2.4.0] slow_calc: tool returned -> 'vpcc7124-slow_calc-427c278ad06337b8'
  [nextflow-v2.4.0] slow_calc: polling that id yields -> energy -1.111111 computed by mock
  [nextflow-v2.4.0] fast_calc: workflow_id = vpcc7124-fast_calc-5f6e5c389dbbef2f
  [nextflow-v2.4.0] fast_calc: tool returned -> ConnectorJobResult(summary='energy -1.111111 computed by mock',
                    data={'total_energy_hartree': -1.111111, 'backend': 'mock', ...})
  ```

  The post-upgrade request is answered with the pre-upgrade number, on both shapes. `-76.402312` was
  never computed.

  **3. Pinned the exact mechanism** (`/tmp/vprobe/c.py`), directly against the broker:

  ```
  executions under that workflow id: 1 [('c55e7a35', WorkflowExecutionStatus.COMPLETED)]
  second start refused with: WorkflowAlreadyStartedError | Workflow execution already started
  ```

  One execution, not two: `ALLOW_DUPLICATE_FAILED_ONLY` does refuse a *completed* duplicate, and
  `jobs.py:386`'s `except WorkflowAlreadyStartedError` is the branch taken.

  **4. Checked the fabricated-number claim at its source.**
  `connectors/qm/activities.py:99-101` is
  `fake_energy = -1.0 * (int(handle.scheduler_job_id[-4:], 16) % 1000) / 10.0` — a number in
  `[-99.9, 0]` Ha derived from four hex digits of a job id, parsed back out by the same
  `energy=` regex the real parser uses. So on the `mock → nextflow` cutover the served value is
  invented, and it arrives with the ordinary `QMJobResult` shape.

  **5. Measured the exposure window.** `describe_namespace` on the running broker reports
  `workflow_execution_retention_ttl: 86400s` (24 h) for `default`. The exposure is that window in
  this dev shape and whatever the operator sets in production — non-zero either way, and nothing in
  the repo pins it lower.

  Housekeeping note for whoever repeats this: the broker is shared with other audit sessions. My
  first two attempts were contaminated — another agent's worker on `background-jobs` served my
  parent workflow task and returned *its* canned `DFT total energy -12.345678 Ha` string, and a
  colliding `VProbeWorkflow` type name did the same on the connector queue. The numbers above come
  from the third run, which isolates both the bundle/workflow names and
  `settings.background_task_queue` (`bgq-cc7124`).

- **Why**: every step of the chain holds on the code as written, and the end-to-end run shows the
  consequence rather than inferring it. `job_workflow_id` (`jobs.py:254-261`) hashes exactly
  `[connector, job, payload]`; `payload` is `prepare_job_launch`'s `spec.model_dump(...)`, and for
  `qm` the spec is `QmJobSpec` — `molecule_smiles`, `method`, `basis_set` and nothing else, since
  `requested_by` is deliberately on the *subclass*. Neither `settings.hpc_launch_interface` nor
  `settings.hpc_pipeline_version` reaches it. The two keys that *do* carry the calculator identity
  (`qm_job_key`, `cache.calculation_key`) are only ever evaluated inside the workflow, which the
  launch-level refusal prevents from running at all. The finding's reading of `jobs.py:386-403` is
  exact: the `qm` shape falls to `return workflow_id` at :403 and the `calc` shape returns the old
  `ConnectorJobResult` at :391-399.

  The secondary `calc` claim also holds: `remote_version`/`cached_remote`
  (`connectors/calc/remote.py:293-313`) take `calc_version` from the `Chemclaw3-mcp` server per
  call, so a server upgrade is a correct miss at the store and, again, invisible at the launcher.

  Two things the reporter did not say, both of which make it worse:

  - **The rejoin leaves no trace.** Run 2 printed no `[job record]` line — `record_job` runs inside
    the workflow, which never executes, and `chemclaw_jobs_started_total` is deliberately not
    incremented on this branch (`jobs.py:420-429`). So the D-157 durable record contains the
    original run only; there is no artifact anywhere saying a post-upgrade request was made and
    answered from a pre-upgrade run.
  - **It is not only a config knob.** My probe changed the *code the child workflow runs*, not a
    setting the launcher could have read. Any change to a connector-owned workflow's science — a
    bug fix in `CalcJobWorkflow`, a corrected constant — is equally invisible to the launch key, and
    the `version_ref` fix in the finding would not cover it either unless the job declares
    something that actually moves with the code.

  Severity stays **high** rather than critical: the trigger needs a completed identical run inside
  the retention window and an operator-visible upgrade, and the failure is bounded in time rather
  than permanent. But it is a silently wrong scientific answer, carried on the normal result shape,
  on the path the model reads as the answer — so not lower than high.
