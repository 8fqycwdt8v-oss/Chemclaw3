# Task: a live-test lane for Temporal + durable workflows + LLM + Postgres

Branch: `claude/temporal-workflows-llm-testing-5nziyp`. Decision:
`docs/decisions/D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md`.

**The question was whether the durable engine can be live-tested together with the model and the
database. It could not be — not because the pieces were missing, but because nothing crossed
them.** The Temporal tests use the time-skipping test server with no model and no database; the
live probe run uses a real model and a real database with no broker, and said so in its own probe
headers; the Postgres tests use neither. The path a durable capability actually takes had been
exercised only in pieces.

_(The previous occupant of this file was the BoFire capability map and roadmap (#111, `3ed77cf`),
which landed on `main` while this branch was in flight; it is in `git log`, and its outcome is in
`docs/reference/bo-capability-map.md` and its own ADR. Before that, the 2026-08-03
grounded-live-run fix list.)_

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

### What is not done

Stage B (`make live-probes`) needs an `ANTHROPIC_API_KEY`, which this container does not have —
`ANTHROPIC_BASE_URL` is set without one. Everything it depends on is verified and it runs
unchanged the moment a key is present. That and two smaller follow-ups are in
`docs/planning/BACKLOG.md`.
