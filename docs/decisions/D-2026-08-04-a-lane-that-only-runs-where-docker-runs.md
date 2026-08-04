# D-2026-08-04-a-lane-that-only-runs-where-docker-runs — a lane that only runs where Docker runs is a lane that does not run

**Status:** accepted · **Date:** 2026-08-04

## Context

Three legs of this system each had a live path, and no path crossed them:

| leg | how it was tested | what it never touched |
|---|---|---|
| Temporal + durable workflows | 13 modules via `WorkflowEnvironment.start_time_skipping()` | no model, no front door, no database |
| model + front door + Postgres | `evals/live` → `cli.live_probes`, real Anthropic, real pgvector | **no Temporal** |
| Postgres | 22 files via `tests/pg.py::migrated_db_or_skip` | no Temporal, no model |

So the path a durable capability actually takes in production — agent tool →
`ConnectorJobWorkflow` on `background-jobs` → the bundle's workflow on `connector-<name>` → the
calculation cache → `job_records` → the audit chain — had been exercised only in pieces, and never
against a real broker.

This was not an oversight anyone hid. The probe corpus said so in its own headers:
*"`start_optimization_campaign` is a Temporal durable job and Temporal is NOT running in this test
run"* (`data/evals/probes/optimization.yaml`), and the same sentence in `reporting.yaml` and
`reaction.yaml`. The last live pass recorded connectors healthy and **Temporal absent**
(`docs/archive/live-grounded-2026-08-03.md`). Six probe *directions* had been written against that
absence, which made them grading keys for one deployment.

The cost is named precisely by BACKLOG's own LIVE-8: *"a configuration that only production sets is
a configuration nothing tests."* The durable path is that configuration — and its failure mode is
not a crash. In the 2026-08-03 run an unreachable broker reached the model as MAF's opaque
"Error: Function failed.", and the model responded by **writing an entire development report by
hand** and presenting it as having entered the PR-gate.

Two structural reasons it stayed that way, both worth naming because both are fixable:

1. **No `make` target started a worker or the front door.** README lines 44-51 listed five
   processes to launch by hand, so every recorded live pass started them by hand, so every one ran
   with some subset missing.
2. **The environments where the lane most needed to run had no Docker daemon.** `make up` is the
   only stack definition, and it is a compose file. CI runners without a privileged socket and the
   agent containers this repository is largely developed in cannot use it.

## Decision

**A scripted live lane, in two stages, that runs with or without a Docker daemon.**

### 1. The durable stage carries no model

`make live-jobs` (`chemclaw.cli.live_jobs`) launches a real declared job through the *real*
generated tool — `connectors.jobs.build_job_tool`, so the pre-flight, the idempotency key, the
actor rule and the rationale requirement are the product's rather than a copy — and then asks the
live system six questions with mechanical answers: the workflow's terminal state from Temporal;
the cache row and the `job_records` row from Postgres; whether a duplicate launch rejoins rather
than recomputes (counted, not asserted); whether a job whose worker is wedged comes back *pending*
rather than hanging or crashing; and whether the audit chain still verifies.

The obvious alternative — ask the agent to run a job and grade the answer — conflates a broker that
did not run the job with a model that did not ask it to. Splitting them means a red result names
the durable spine and nothing else, **and** makes the durable half runnable where no model
credential exists, which is most CI runners and every agent container here. That second property is
not a consolation prize; it is what makes this stage reachable at all.

Nothing is scored from prose, which is D-2026-08-03's correction applied from the start.

### 2. The model stage checks the broker, not the turn

`data/evals/probes/durable.yaml` adds four probes that require durable work, and `Probe.expects_job`
makes the runner resolve every launched workflow id against Temporal
(`evals/live.py::_job_outcomes`). This closes a hole the event stream cannot: a job tool returns an
id the moment the launch is **accepted**, so an answer can be entirely honest in saying "I started
a job" about work the broker never ran, and no judge reading prose could tell. `RUNNING` is not
scored as failure — a campaign outlives its turn by design — but `FAILED`, `TIMED_OUT` and an id
the broker never heard of are findings that previously had no way of being found.

An unreachable broker records `unreachable` rather than a dead job: "the eval could not tell" and
"the job did not run" are different findings, and only one is about the system under test.

### 3. The bootstrap prefers Docker and does not require it

`infra/live/bootstrap.sh` execs `docker compose` whenever a daemon answers. Otherwise it builds
pgvector and the Temporal CLI from git clones and starts a native cluster plus `temporal server
start-dev`, on the same ports the compose file binds — so `settings.postgres_dsn`,
`settings.temporal_address` and every downstream reader are identical either way.

The acquisition route is measured, not preferred: `temporal.download` answers **403 to CONNECT**
behind a filtering egress proxy and `codeload.github.com` archives are denied the same way, while
git-over-HTTPS and `proxy.golang.org` are reachable. `go install` additionally refuses the Temporal
CLI module because its `go.mod` carries replace directives, so a clone plus `go build` is not a
stylistic choice — it is the only route that works, and it keeps working when the binary host is
blocked.

### 4. Probe directions describe behaviour, not deployments

The six directions asserting Temporal's absence are rewritten to grade the same thing in either
configuration: does the turn's account of the job match what became of it? A started job named as
started, a failed one named as failed, and never a result the job did not return. A direction that
names one deployment is a grading key that silently stops being true — it would have failed a
*successful* launch the day the workers came up. `tests/test_live_probes.py` now pins that no
direction asserts which deployment it meets.

## Consequences

- Six checks pass against a live stack in this container: Postgres 16 + pgvector 0.8.6, Temporal
  Server 1.31.2 via CLI 1.8.2, all four workers, no Docker. Recorded in `tasks/todo.md`.
- The lane's payload varies per run (a real physical input — the reaction temperature — not a
  nonce). With a fixed payload the *second* `make live-jobs` against one database would rejoin the
  first run's workflow, compute nothing and pass every check on residue: a lane that goes green
  while exercising nothing, which is the exact failure this ADR exists to remove. Pinned by
  `tests/test_live_jobs.py`.
- **None of this is in `make ci`.** It is a gate you run against a deployment, not on a diff — the
  same line `evals/live` already draws.
- Stage B still needs a real model credential, so it is the one part not exercised here.
- The Temporal-backed tests continue to skip in blocked-egress environments, because they fetch the
  *time-skipping test server* from `temporal.download` and a live broker cannot substitute for time
  skipping. Pointing the subset that does not skip time at a real broker is a separate question,
  left in `docs/planning/BACKLOG.md` rather than guessed at here.
