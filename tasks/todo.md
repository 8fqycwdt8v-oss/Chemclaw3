# Task: a live-test lane for Temporal + durable workflows + LLM + Postgres

Branch: `claude/temporal-workflows-llm-testing-5nziyp`. Decision:
`docs/decisions/D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md`.

**The question was whether the durable engine can be live-tested together with the model and the
database. It could not be — not because the pieces were missing, but because nothing crossed
them.** The Temporal tests use the time-skipping test server with no model and no database; the
live probe run uses a real model and a real database with no broker, and said so in its own probe
headers; the Postgres tests use neither. The path a durable capability actually takes had been
exercised only in pieces.

_(The previous occupant of this file was the Snowflake-ELN warehouse concept (#113, `23f0d61`),
which landed on `main` while this branch was in flight; it is in `git log`, and its outcome is in
`chemclaw.ingest.eln.warehouse` and its own ADR. Before that, the BoFire capability map (#111) and
the 2026-08-03 grounded-live-run fix list.)_

---

## Plan

- [x] **1. Bootstrap the stack without Docker.** `infra/live/bootstrap.sh` — defers to
      `docker compose` when a daemon answers, otherwise builds pgvector and the Temporal CLI from
      git clones and starts a native cluster on the same ports.
- [x] **2. Supervise the processes.** `infra/live/processes.sh` — connectors, the four Temporal
      workers, the front door; readiness-polled, never slept; one probe port per worker.
- [x] **3. Stage A, the durable smoke.** `chemclaw.cli.live_jobs` — a real declared job through
      the real generated tool, six mechanical checks against Temporal and Postgres, no model.
- [x] **4. Stage B, the model on top.** `data/evals/probes/durable.yaml` (4 probes),
      `Probe.expects_job`, and `evals/live._job_outcomes` resolving every launched workflow id
      against the broker instead of believing the turn.
- [x] **5. Un-hard-code the deployment from the corpus.** Six probe directions across three files
      asserted "Temporal is not running in this test"; rewritten to grade behaviour, with a test
      that pins the class.
- [x] **6. Targets, tests, docs.** Seven `make live-*` targets; `tests/test_live_jobs.py` and five
      new cases in `tests/test_live_probes.py`; runbook section, README port fix, ADR, BACKLOG.

---

## Review — what was actually measured

**The stack, in this container, with no Docker daemon:** PostgreSQL 16 + pgvector **0.8.6** on
5432, Temporal **Server 1.31.2** (CLI 1.8.2, built from source) on 7233, all 34 migrations applied,
six connectors on 8810, four workers ready on 9000-9003.

**`make live-jobs` — 6/6**, on a workflow started by that run
(`calc-compute_reaction_energy-f47443a513e5db4b`):

| check | observed |
| --- | --- |
| workflow reached COMPLETED | COMPLETED, started 2026-08-04T05:54:06+00:00 |
| calculation cached in Postgres | 6 `xtb*` rows in `calculation_results` |
| job recorded in Postgres | `calc/compute_reaction_energy` by `service-account`, with its rationale |
| duplicate launch rejoins the same run | id matches; cache rows 12 → 12 (nothing recomputed) |
| wedged worker yields a pending job | returned the id after 20 s, then COMPLETED once resumed |
| audit chain verifies | OK: the audit trail hash chain is intact |

**`make lint type test`:** green — 3015 passed, 34 skipped. Worth noting what changed underneath
that number: with Postgres up, the 22 Postgres-backed test files **ran** rather than skipped. A
green suite in an offline sandbox had been reporting on a suite that largely did not execute.

### Three things the lane caught while being built, which is the argument for it

1. **The pid files recorded the wrong process.** `uv run python -m …` puts `uv` in the pid file and
   the worker one fork below it, so `kill` reached the launcher. Found only because the
   wedged-worker check sends a signal and got `ProcessLookupError`. Fixed by resolving the
   interpreter once and starting it directly — a layer removed rather than worked around.
2. **The wedged-worker payload was invalid.** It inherited the smoke's symmetry numbers onto a
   different equation, and `_checked_symmetry_numbers` rejected it correctly. The lane reported a
   real failure; the failure was mine. Now pinned by a test, because a bad probe that reads as a
   system fault is the worst kind.
3. **A rerun would have passed on residue.** A durable job's id is a hash of its payload and a
   duplicate launch deliberately rejoins rather than recomputes — so with a fixed payload the
   *second* `make live-jobs` against one database starts nothing, computes nothing, and passes
   every check against the first run's rows. The payload now varies per run on a real physical
   input. This is the failure the whole lane exists to remove; building it into the lane would have
   been the joke writing itself.

---

## Follow-up: the full live pass (same day, with a key)

Stage B ran. Record: `docs/archive/live-full-stack-2026-08-04.md`; decision:
`docs/decisions/D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed.md`.

Every layer up at once for the first time — Temporal 1.31.2, four workers, Postgres 16 + pgvector
0.8.6, six connectors, the front door, and `claude-sonnet-5`. Stage A **6/6** (now over 139 audit
events rather than zero). Harness slice **2/2 answered, 2/2 tool reach, 0 silent failures** — the
chart's configuration meeting a live model.

**Four defects, one class.** A job that failed after its turn told nobody; a job that failed inside
its turn reached the model as `Error: Function failed.`; a turn that wrote nothing said nothing;
and — twice — a *check* passed vacuously. All fixed, all with regression tests.

**Two of the four were in the measurement, not the product,** and that is the part worth keeping:
the smoke's audit check had been verifying an empty chain, and the durable-reach signal flagged
du-01 as having run no job while Temporal held its workflow in COMPLETED. Both were written this
same session to *find* problems. A signal that has never been wrong has usually never been used.

**And one fix was wrong for an hour, measurably.** `failure_reason` first walked to the innermost
cause and reported the tblite internals; the sentence written for a chemist — naming "2-MeTHF" and
the solvents that would work — sat one frame above. Depth is not specificity.

Left open in `docs/planning/BACKLOG.md`: validating solvent names at the tool boundary (the root
cause behind two findings), du-03's behavioural half, the repeated-tool-call cost, the full
230-probe sweep (which needs a corpus worth sweeping — this repo ships 38 notes), and an
Entra-enforced pass.

---

## Follow-up: a much higher testing and review bar (same day)

Record: `docs/archive/storm-2026-08-04.md`. Decisions:
`docs/decisions/D-2026-08-04-the-schema-only-goes-forward.md`.

**The state this started from was weaker than it read, and all three reasons were measurable
rather than suspected:** the storm claimed eight scenario families and wired six; SCALE-3 was
still open because the sweep varied *offered* load and never touched the cap; and no
property-based, mutation or concurrency testing existed at all.

- [x] **1. Make the harness honest before making it bigger.** `FAMILIES` declares the planned set,
      the report prints planned-versus-ran, and the exit code depends on coverage as well as on
      results. Families **E (chaos)** and **H (edges)** wired — the six dead behaviours now assert
      something, plus the missing negative (arguments that parse and cannot be true).
- [x] **2. Close SCALE-3.** `family_a_admission` restarts the front door at each
      `service_max_concurrent_turns` ∈ {2,4,8,16,32} with offered load held at 48, **three samples
      per cap**, and reads the knee against the spread those samples show rather than a threshold
      chosen in advance.
- [x] **3. Property-based tests.** `tests/test_properties_core.py` — nine properties over
      `stable_hash`, `BoundedLru` and the two citation readers, the identity and bounding
      primitives whose contracts are universally quantified.
- [x] **4. Mutation testing configured.** `[tool.mutmut]` scoped to the seven invariant-bearing
      modules; `make mutants` / `make mutant-results`.
- [x] **5. Concurrency tests for the hazards that were only claims.**
      `tests/test_concurrency_claims.py` and `tests/test_concurrency_audit_chain.py`.
- [x] **6. The PR-gate read window, settled by measurement.**
      `tests/test_pr_gate_read_window.py` — recorded, deliberately not fixed.
- [x] **7. Migration rollback policy settled rather than left silent.**
      `tests/test_migrations_are_additive.py` + ADR.

---

## Review — what was actually measured

**Three product findings.** The `openai_compatible` streaming path announced **ten `tool_call`
events for one call** (`c-fragmented` 10/1, `c-parallel` 18/6 → 1/1 and 6/6 after the fix) — a
defect on the seam the target deployment uses, which CI and 3,000 tests had no way to see. A
SIGKILLed connector worker costs **583 s** before its job resumes, because one setting must both
tolerate a CREST-sized heartbeat gap and detect a dead worker. And **SCALE-3 is measured**: goodput rises 2.1×
across the cap range, the 2 → 8 steps buy 29–38 % each (settled), and above 8 the steps are inside
the sweep's own noise — two back-to-back sweeps disagreed and the second refused to name a knee,
which is the harness reporting what it could not see rather than a number.

**Four of the seven findings were in the measurement, not the product**, which is what happens the
first time a signal is used:

1. The storm reported "17/17 checks passed" for a matrix two families short of what it documented.
2. Family D passed while measuring nothing — `<= 1` is a bound a run that launched nothing meets,
   and the collision payload was cached from an earlier run. Fixing it surfaced the harder half:
   the mock is a *separate process*, so a per-run constant does not make the payload cold.
3. The sweep's throughput metric counted refusals as completions, which inverted SCALE-3's answer.
   Draining a queue by refusing it is fast.
4. Two checks were wrong about the system rather than the reverse (a 100 KB argument is legitimate
   input; a new workflow need not recompute cached species) — the opposite correction, and worth
   separating from the vacuous ones.
5. **The knee was declared against a threshold nobody had measured**, one day after
   D-2026-08-04-a-plateau-needs-the-noise-you-measured-it-with said not to. Three single-sample
   runs straddled it, so the same stack answered "cap 8" twice and "no knee" once. Judged against
   the sweep's own measured spread, two sweeps still disagreed (knee at 16, then unresolvable) —
   so the answer is that the fine question is open and the harness now says so. And the correction
   introduced its own failure: a noise floor makes a knee fire *sooner*, so a sweep that could see
   nothing would have named cap 2 — caught only by writing the test for the opposite behaviour and
   watching it fail.

**One thing proven rather than asserted.** The audit-chain concurrency tests were shown to kill
the mutant they exist for: with `pg_advisory_xact_lock` replaced by `pass`, both fail; restored,
both pass. A green concurrency test whose guard can be deleted without it noticing is worse than
none, because it gets cited.

**Left open, deliberately:** the PR-gate's read window (the fix is an architectural change to a GxP
control — `git worktree` for the submission — and is a decision to take, not a diff to slip in),
and the heartbeat/detection coupling (a config-surface decision). Both in
`docs/planning/BACKLOG.md` with their measurements and regression targets.
