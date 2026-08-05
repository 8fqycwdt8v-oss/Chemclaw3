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

---

# Task: the BO capability audit and its five-wave roadmap

Branches `claude/bofire-capabilities-roadmap-pmeipd` (#111) then one per wave: W1 #114, W2 #117,
W3 #118, W4 #119, W5 #120. Map: `docs/reference/bo-capability-map.md`. Register:
`docs/decisions/D-2026-08-04-what-bofire-does-when-you-actually-run-it.md`; one ADR per wave.

**The question was what the BO layer can already do for chemical and analytical development, what
BoFire offers that we never call, and in what order to close the gap.** The answer turned on a
decision made at the start: **measure BoFire rather than read it.** The previous BO roadmap had been
written from a code audit and was wrong — it called threading `n_generators` a one-line change
because the parameter exists and its docstring explains it, and the parameter is inert on the only
domain shape that reaches us. So eight measurements ran first. Three changed a wave, and one
**reversed a refusal**: cross-validated fit quality had been ruled out because reaching it means
naming a surrogate class, and `strategy.surrogate_specs` turned out to expose the one BoFire itself
chose.

**All five waves shipped.** Campaign health with a required `assay_noise` and an observed-spread
scale for the sd (W1); continuous factors in a screen, plus centre points, replication and seeded
run-order randomisation (W2); multi-objective with a computed Pareto front and a `best_of` that
refuses to pick an axis (W3); linear constraints and a scoped categorical exclusion (W4); and
`predict_outcome` — what the model expects at a point the *chemist* named, with the cross-validated
quality of the surrogate behind it (W5). Story 3.3 went `PARTIAL` → `SERVED`; 3.4 and 3.5 gained
what they were missing.

**The recurring defect was a refusal outliving its refusal.** Four times a wave made an earlier
"we cannot do this" false — in a tool description, a skill instruction, an eval probe and a test
assertion — and each had to die in the same commit as the code, or the model would be taught to
decline a capability that exists. `tests/test_bo_tools.py`'s description test broke on purpose in
three separate waves and now records all four states. One of those was **missed and caught later**:
W4's ADR said `op-17` needed no rewrite because it asks for a coupled constraint; the probe actually
asks for two limits, only one of them conditional, and would have graded the correct new answer as a
failure. W5's ADR carries the correction.

**Three things the measurements caught that reading would not have.**
`discrete_candidate_count` returned the product of the category counts, which an exclusion makes an
over-count — 2×2×2 minus one forbidden pairing is six, not eight — and both its readers act on that
number. The roadmap claimed the exclusion would work for a screen; measured,
`FractionalFactorialStrategy` rejects *every* constraint class at construction. And a constraint
costs about three times an unconstrained ask at one candidate and ~9s per further candidate, which
is why two tests blew CI's timeout and why the tool now tells the model to keep constrained batches
small.

**One number is retracted.** The register's "R² 0.948 / MAE 1.47" cannot be reproduced: the script
that produced it passes `get_metric` a string where an enum is required, and raises. The *finding*
reproduces at 0.935, 0.950 and 0.813 across three routes; the pair does not, and the maintained map
says so.

**Verification, at the end rather than only per wave.** The register was re-run whole against the
finished tree — everything reproduces bar the retraction above — and the three probes the roadmap
exists for were driven on their own data: `op-13`'s untried corner carries **9.4×** the posterior sd
of an explored point, `op-16`'s front is **4 of 6** supplied runs by dominance, and `op-17`'s volume
limit is honoured by construction while its conditional half is still refused. Those are the answers
those probes were graded *fabricated* for asserting, now computed.

Left open in `docs/planning/BACKLOG.md`: the `method` note type, which is what analytical method
development is actually waiting on and is a schema row rather than a BO one. `DEFERRED.md` carries
nonlinear and `NChooseK` constraints, interpoint/blocking, model-based optimal design (cyipopt +
SCIP), and feature importance, each with the trigger that would reopen it.

---

# Task: the CHECKMATE deep review of the live/durable spine (2026-08-05)

Branch `claude/temporal-workflows-llm-testing-5nziyp`, after #121 merged. Record:
`docs/archive/review-2026-08-05.md`. Decision:
`docs/decisions/D-2026-08-05-a-trend-needs-a-tail.md`.

- [x] **1. Land #121.** `main` had moved (the BO waves); one conflict, in this file, both records
      kept in their own order. Merged, and the branch restarted from the new `main`.
- [x] **2. A soak that survives the container.** `infra/live/soak.sh` + `make live-soak` +
      `chemclaw.cli.soak_report`. Checkpointed per round, resumes from the record — proven by
      killing it twice mid-run (resumed at rounds 31 and 45).
- [x] **3. The CHECKMATE G1–G7 pass** over `durable/connector_job.py`, `connectors/jobs.py`,
      `api/runner*.py`, `api/routes/*`, `agent/session_store.py`, `kg/pr_gate.py`,
      `kg/git_submitter.py` — 4,286 lines, never reviewed whole before.
- [x] **4. The `make ci` question, answered with the number** rather than argued: no.

---

## Review — what was actually measured

**Nine findings, and the method that found most of them was mutation** — delete the guard, run the
tests it is named after, see whether anything notices. **Eight guards over this spine could be
deleted with the suite green.**

The three that are defects rather than test gaps: the mid-turn resume drove a second `agent.run`
whose tokens **never reached the budget guard** (1,000 booked on a turn that spent 6,000 — 83 %
unmetered, on the one feature that adds an unbounded second model call); a **re-joined** job that
fails handed MAF a raw `WorkflowFailureError`, the fourth appearance of "a failure that says nothing
is read as proceed", because the framing had been written at one of the two call sites that await;
and `chemclaw_jobs_started_total` was booked *after* the inline wait, which every `calc` job skips —
five of the seven declared jobs — while their runtime kept being counted, so two numbers on one
dashboard disagreed and only one was true.

**The fix for the second is the one worth keeping.** Copying the guard to the second call site would
have been correct and would have set up the fifth appearance. It moved into `_await_briefly` — the
only function that awaits — and a test now asks the module's AST whether anything awaits a result
outside it.

**Six false prose claims**, four corrected in the same commit as the code. The pattern is always the
same: a sentence that was true when written and that a later commit falsified silently — a task
queue that stopped coming from the manifest (D-150), a dry-run check that moved to the tool boundary,
a literal that stopped mirroring the setting it claimed to mirror.

**One of the nine was mine, from last week.** `test_readers_are_not_synchronised_with_the_submitter`
asserted `count >= 1` and `graph_cache_ttl_seconds >= 0` — both true under every implementation
including the fixed one — in a file written to stop exactly that. It now takes a read *while* the
submitter's checkout lock is held, which is the assertion that inverts when the fix lands.

**The soak found a leak, and it took four readings to say so.** api RSS went **549 MB → 998 MB over
138 rounds** (~2 h 15 m) — ~3,200 KB per round of ~82 turns, about 39 KB retained per turn, with no
plateau at any window examined. The readings on the way there were *accelerating* (29 rounds),
*plateau* (43), *decelerating* (104) and finally *steady, first half +3,166 / second half +3,177*
(138). Every one of those fits was resolved with a tight error bar and three of them were wrong, for
one identifiable reason: the comparison was tail-versus-whole, and the whole **contains** the tail —
so on a series that rises in steps an early flat stretch drags the whole-run slope below the tail's
and "tail below whole" reads as deceleration. `describe()` now compares two equal-length halves.
`chemclaw_live_sessions` sat pinned at its bound of 1000 throughout, so it was never the LRU filling.
It is a [H] BACKLOG row: on OpenShift this is a pod that OOMs on a timer, and finding *what* is
retained needs an allocator-level look a soak cannot give.

**And one mistake was mine and cost a commit.** Three review subagents were mutating the tree by
design; one did not restore what it deleted, and my `git add -A` committed the removal of two lines
of `run_turn`. Four tests fail on that version, so CI would have caught it — which is why it was
cheap, not why it was acceptable. `tasks/lessons.md` R5.6.

**Left open deliberately**, each in `docs/planning/BACKLOG.md` with its measurement: a crash
mid-submission leaving the shared checkout on the note branch (same `git worktree` fix as the read
window, so it should be taken once as a GxP decision), a multi-file `FAILED` proposal that cannot be
replayed, `_REASON_CHARS` truncating where it should redact, `refresh` discarding its `rowcount`, the
duplicated plan-emission predicate, the webhook contract, and the layering policy's granularity.
