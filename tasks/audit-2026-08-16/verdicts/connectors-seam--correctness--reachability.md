# Verdicts — connectors seam / CORRECTNESS, lens: reachability + consequence

Scope: only findings marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/connectors-seam--correctness.md`.
That file has **one**: the durable-launch idempotency key. The other five are medium/low and
were not examined.

Environment: `infra-temporal-1` + `infra-postgres-1` up (broker on :7233), `uv run`.
Note for whoever reads the Temporal UI: another audit session has a worker
(`/tmp/v/f2wf.py`) polling `background-jobs`, so my first probe run was served by *their* worker.
All measurements below were re-run on a private queue (`CHEMCLAW_BACKGROUND_TASK_QUEUE=audit-verify-5998`)
to remove that interference. No source file was modified; the working tree is clean.

---

## The durable-launch idempotency key omits the calculator/pipeline version, so a completed pre-upgrade run is served as the current answer

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)
- **What I did**:

  **1. The key is invariant under both knobs** (`/tmp/audit-v/child.py`, one fresh interpreter per
  configuration so `settings` is really re-read):

  ```
  interface=mock      pipeline=""       workflow_id=qm-compute_dft_energy-29776d63ecaa48fb  calc_version=mock-unversioned    qm_job_key=941c5e787b19328d
  interface=nextflow  pipeline=v2.3.0   workflow_id=qm-compute_dft_energy-29776d63ecaa48fb  calc_version=nextflow-v2.3.0     qm_job_key=c5a67e881a0e96ad
  interface=nextflow  pipeline=v2.4.0   workflow_id=qm-compute_dft_energy-29776d63ecaa48fb  calc_version=nextflow-v2.4.0     qm_job_key=cfe3264294f6c05c
  ```

  Same digests the reporter published. `calculation_key`'s `params_hash` moves too
  (`0ebe58616a3b5213` → `70d67bb42da152e4` → `62df4c8e14c4040c`); the launch id does not move at all.

  **2. End-to-end through the real launcher, against the live broker** (`/tmp/audit-v/live.py`).
  I did *not* hand-roll the launch: the probe calls `registry.find_job("compute_dft_energy")` and
  `connectors.jobs.build_job_tool(...)`, i.e. the exact coroutine the agent is handed, twice —
  once under `mock`/unversioned, then again under `nextflow`/`v2.4.0` with identical params. Only
  the *body* of `ConnectorJobWorkflow` is a stub (it stands in for the run that completed under the
  old pipeline; it returns a real `ConnectorJobResult`).

  ```
  [OLD] expected workflow id: qm-compute_dft_energy-a293f772670995e0
  [OLD] launcher returned: 'qm-compute_dft_energy-a293f772670995e0'
  [OLD] execution status: RUNNING  start=2026-08-17 08:07:06.282359+00:00
  [OLD] result: DFT total energy -12.345678 Ha (B3LYP/def2-SVP) [produced by mock-unversioned]
  == operator points at the real cluster and bumps the pipeline ==
  [NEW] expected workflow id: qm-compute_dft_energy-a293f772670995e0
  [NEW] launcher returned: 'qm-compute_dft_energy-a293f772670995e0'
  [NEW] execution status: COMPLETED  start=2026-08-17 08:07:06.282359+00:00   <-- the OLD run
  [NEW] result the chemist is shown: DFT total energy -12.345678 Ha (B3LYP/def2-SVP) [produced by mock-unversioned]
  ```

  And the agent's own follow-up tool, called under the *new* configuration:

  ```
  get_durable_job_status -> {"job_id":"qm-compute_dft_energy-a293f772670995e0","status":"completed",
   "summary":"DFT total energy -12.345678 Ha (B3LYP/def2-SVP) [produced by mock-unversioned]",
   "result":{...,"produced_by":"mock-unversioned",...},"rationale":""}
  ```

  **3. Exactly one execution exists** under that id after two launches
  (`/tmp/audit-v/runs.py`, `list_workflows(WorkflowId = '...')`):

  ```
  run: 57fae4e7-... COMPLETED start 08:07:06.282359 close 08:07:06.302715
  total executions under that workflow id: 1
  ```

  So the second launch really did take the `except WorkflowAlreadyStartedError` branch: nothing
  re-executed, and `chemclaw_jobs_started_total` was not incremented (that `record_metric` sits
  after the `except`).

  **4. Retention, i.e. the size of the window** — `DescribeNamespace('default')` returns
  `workflow_execution_retention_ttl: 86400s` here; the Helm chart configures no retention at all,
  so a deployment gets whatever its self-hosted Temporal defaults to (a day to a few days).

- **Why**: every link the finding asserts holds under execution, and the two things my lens is
  supposed to attack both survive.

  *Reachability.* The primary trigger is not a private-function call — it is a value an operator
  edits in the shipped chart. `deploy/helm/chemclaw/values.yaml:365` ships
  `CHEMCLAW_HPC_PIPELINE_VERSION: "1.0.0"`, and bumping it is precisely the gesture
  `qm_job_key`/`calc_version` were written to respond to. Nothing upstream narrows the payload:
  `QmJobSpec` is three free fields, `prepare_job_launch` serializes exactly those three, and
  `job_workflow_id` hashes `[connector, job, payload]` with no other input. There is no validator,
  no manifest field, no startup guard and no Helm default that puts a calculator identity anywhere
  near the launch key — I looked for one and there is nothing to find. The only bound is Temporal
  retention (≈1 day as configured here), which is a *window*, not a barrier: an identical re-ask
  inside that window is the ordinary case, and the tool's own docstring actively invites it
  ("Identical requests share one job id, so re-asking is free"). The mock→cluster half is the
  weaker of the two scenarios (it needs the same namespace across the cutover), but the
  version-bump half needs nothing but a redeploy.

  *Consequence.* It is not a worse-sounding paraphrase — if anything the report understates it in
  one place. `_envelope` builds `data` from `QMJobResult.model_dump()`, which is
  smiles/method/basis/energy/converged/requested_by: **no backend, no pipeline version, no
  `calc_key`**. The `calc_refs` stamp the reporter cites lives only on the `note`, and
  `completed_job_status` copies `envelope.summary` and `envelope.data` into `DurableJobStatus` and
  drops the note. So what a chemist is actually shown is a bare total energy labelled
  "B3LYP/def2-SVP", with nothing in the answer that could reveal it came from the previous
  pipeline (or, in the mock case, from `-1.0 * (int(job_id[-4:], 16) % 1000) / 10.0` in
  `activities.poll_hpc_status`). There is also no second note proposed on the rejoin — no workflow
  runs — so the PR-gate does not get a second look at it either.

  The `calc` half is real too and lands harder in one respect: all five `calc` jobs carry
  `inline_wait_seconds: 20` (`connectors/calc/connector.yaml:101,132,150,163,184`), so the rejoin
  branch (`jobs.py:391-399`) returns the *finished old envelope* as the tool's own return value
  within the turn — no polling step at all — and `remote_version` confirms `calc_version` is
  stamped by the remote `Chemclaw3-mcp` server per call, so a server upgrade is invisible to the
  launch key by exactly the same mechanism.

  *Why not critical.* It needs a configuration change plus a byte-identical repeat inside the
  retention window, and the payload is the *raw* model-authored SMILES (so "CCO" vs "OCC" already
  miss — the launch key is narrower than `qm_job_key` in the other direction). The wrong answer is
  a computed energy, not a safety or impurity-limit statement. High is the right label; the
  reporter's "the retention window is the exact size of the exposure" is a fair statement of the
  bound rather than a hedge.
