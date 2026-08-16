# Hardening session — the non-agentic job seam, the quick path, the slow path

**Goal:** maximum robustness of everything between an agent tool call and a durable result,
with the *lowest possible* configuration and maintenance burden. Both halves are the task; a
fix that buys robustness by adding a setting nobody will get right is not a fix.

## Plan

- [x] Start the local stack (dockerd, `make up`, `make db-migrate`) so Postgres-backed tests
      actually run — a local suite without it silently skips ~157 of them.
- [x] Baseline `make lint type` (green) and a full `make test` (green, exit 0).
- [x] Fan out five read-only reviewers, one per seam, each told to measure rather than argue.
- [x] Triage: confirm each finding myself before acting on it.
- [x] Implement the confirmed fixes, smallest sufficient change each, with a test that fails
      without the fix.
- [x] Two ADRs for the changes that alter a rule rather than fix a bug.
- [x] `BACKLOG.md` rows for what was found and deliberately not fixed.
- [ ] Re-run `make lint type test` with infra up; report what was skipped.
- [x] Review section below.

## What was fixed, and how each was confirmed

Every item was measured before being believed — against the live Temporal broker, the live
Postgres, or the companion mock launcher driven through the real adapter code.

**Silent wrong science**
- `pipeline_name` was in neither the DFT cache key nor `qm_job_key`, so two pipelines tagged
  `1.0.0` produced the byte-identical key and served each other's energies, with a `calc_refs`
  stamp naming the pipeline that had not run it. Added to both, only when configured, so `mock`
  keys are untouched and exactly the ambiguous rows are invalidated.

**Work lost permanently**
- `ConnectorJobWorkflow` declared no `failure_exception_types`, so a non-envelope child result
  parked the parent in an unbounded workflow-task-failure loop: RUNNING forever, no push-back,
  `get_durable_job_status` answering "running" for a job that will never finish. Same hole in all
  three bundle workflows. Fixed on the job path, guarded by a test over the *registry*.
- `fetch_artifacts` sat outside the poll's error tolerance, so a few seconds of 503 at the artifact
  store burned all five Temporal attempts in 1.51 s and discarded a finished multi-hour run.
- A failed run's `Idempotency-Key` was the science alone, so a re-drive got the old dead run id
  back and the molecule stayed unrunnable for the launcher's key-retention window.

**Retry classification inverted**
- Every infrastructure fault on the calc server (`"an internal error occurred"`, which is the path
  `CliError` takes *by design*) arrived as `CalcToolError` — registered non-retryable — so an xtb
  timeout failed a durable job on attempt 1 with the retry budget untouched.
- The child call's `BAD_DATA_RETRY` cannot classify anything at a child boundary: measured 5 child
  executions for one `ValueError`, i.e. five DFT submissions for one bad basis set.

**Green while doing nothing**
- Every `helm upgrade` resumed any Schedule an operator had paused (measured: pause → True,
  re-apply → False).
- A dropped fan-out child had no metric, so a dead PR-gate credential reads as "zero proposals",
  indistinguishable from an idle system.
- `notify_session_best_effort` set `start_to_close` only, which never fires for a task no worker
  picks up: measured still RUNNING at 75 s against an unserved queue.
- `eln-sync` fired hourly with no ELN configured; `retention_enabled` scheduled a sweep whose four
  windows all default to 0.

**Fails late instead of early**
- `hpc_api_token` was the one `nextflow` input the startup validator omitted, so a secret that
  failed to mount produced green pods and a 401 five attempts into the first DFT job.
- Nothing checked that a manifest's `workflow:` names a workflow the bundle registers — a typo cost
  25 h of "running" and passed every other gate. Now `make connector-validate` names the fix.

**Configuration burden removed rather than added**
- The launch POST raced its own activity timeout, and the poll's heartbeat gap was mis-measured.
  Both are now *derived* (`hpc_submit_timeout_seconds`, `hpc_effective_heartbeat_timeout_seconds`)
  rather than validated, because the validator I wrote first **refused the shipped chart** — see
  `D-2026-08-16-arithmetic-about-a-loop-is-derived-not-configured`.
- Net settings added by this session: **zero**.

## Review

**What went well.** Five parallel reviewers with one seam each, all told to measure rather than
argue, produced ~35 findings of which the ones acted on here were reproducible without exception.
Two reviewers independently found the missing `hpc_api_token` check and the missing
`workflow:`-name check, which is the useful kind of redundancy — it raised confidence in both
before either was verified.

**Where I was wrong, twice, and the measurement caught it.**
1. I wrote in a code comment that adding `pipeline_name` "keeps every `mock` key byte-identical".
   `CalculationKey.build` hashes the params mapping whole, so a `None`-valued key changes the hash;
   the claim was false until the field was made conditional. My own test failed on it.
2. I added a validator requiring `2 * hpc_http_timeout < qm_activity_timeout`. It was correct about
   the defect and it refused the shipped chart — the exact trade this session was told not to make.
   The ADR records it rather than deleting it silently, because the next person to see those two
   knobs will reach for the same validator.

**Process defect worth keeping.** I ran `make lint type 2>&1 | tail -3 && git commit`, which commits
on a *failing* gate: the pipeline's exit status is `tail`'s, not `make`'s. One commit had to be
amended for formatting. Run the gate as its own command and read its exit code.

**What I did not fix, and why.** The digest writes to a mailbox with no reader (a route is a design
decision about who reads a digest, not a bug fix); the chart's `enabled` flag never reaching
`CHEMCLAW_CONNECTORS_ENABLED` and the two connector-health gaps (Helm work, and `helm` is not in
this sandbox to verify a render); widening `failure_exception_types` to the sixteen periodic
workflows (a different trade for runs nobody is waiting on). All four are `BACKLOG.md` rows carrying
the measurement, so the next session starts from evidence rather than from a hunch.
