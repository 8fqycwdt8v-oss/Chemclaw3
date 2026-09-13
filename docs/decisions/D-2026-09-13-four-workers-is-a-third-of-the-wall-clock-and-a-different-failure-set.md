# D-2026-09-13-four-workers-is-a-third-of-the-wall-clock-and-a-different-failure-set — the suite runs on four workers, and the failure set is the same one

A `BACKLOG.md` row has held `pytest-xdist` open since
`D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one` took the free half of
the CI-duration problem. Its reason for staying open was explicit and honest: *"'looks safe' is not a
number, and the sandbox this was reviewed in ran the suite far slower than a GitHub runner does, so a
local figure would say nothing about CI"*, and its closing condition was one experiment — add the
plugin, run it, compare the wall time **and the failure set** against serial on the same commit.

## What was measured

Two premises of the row need correcting before the numbers mean anything.

**The machine is the same shape as the runner.** This box is **4 cores / 16 GB**; `check` runs on
`ubuntu-latest`, which is 4 vCPU / 16 GB. A local figure is still not CI, but it is no longer a
different class of machine, which is what the row was guarding against.

**CI does not run `make test`.** `.github/workflows/ci.yml`'s `check` job runs **`make cov`**, and the
row's experiment named `-n auto` on the suite. Coverage is where the parallel-safety question actually
bites, because `pytest-cov` has to combine across workers or the 84 % floor becomes a different
quantity.

All four arms on `08023695`, Docker/Postgres/Temporal up:

| arm | wall clock | CPU (user) | result | coverage |
| --- | --- | --- | --- | --- |
| `make test`, serial | **18:13** | 16m04 | 1 failed, 8834 passed, 4 skipped | — |
| `pytest -n 4` | **09:30** | 32m13 | 1 failed, 8834 passed, 4 skipped | — |
| `make cov`, serial | **27:25** | 23m40 | 8833 passed, 4 skipped | **90.53 %** |
| `make cov -n 4` | **12:28** | 41m24 | 1 failed, 8834 passed, 4 skipped | **90.53 %** |

**1.92x on the suite, 2.20x on the gate CI actually runs**, and the coverage total is identical to two
decimal places — so `pytest-cov` does combine across workers, and the floor survives with the same
margin it had.

**The failure set is identical**, which is the half of the experiment that decides it. The single
failure in every arm is `tests/test_calc_fake_identity.py`, which fails for an environmental reason
this branch did not introduce: it drives the sibling `Chemclaw3-mcp` checkout's own key derivation and
this host has neither `xtb` nor `crest` on `PATH`. Verified by running that file at the merge base in a
separate worktree — same failure. The skip set is also identical: 4, none of them Postgres or Temporal.
(The serial `cov` arm's second failure was `test_the_shipped_operator_documents_name_only_things_that_exist`
on the then-unwritten citation to this file.)

**Four timing failures were expected and did not happen.** A prior reading of this predicted
`test_a_burst_of_cold_prefix_measurements_leaves_the_loop_schedulable`,
`test_redaction_cannot_be_made_quadratic_by_a_log_line`, `test_the_proposing_path_honours_the_constraint`
and `test_the_fit_score_does_not_reproduce_...` failing under contention, and a `--dist loadgroup` hint
to fix them. None of the four failed in either parallel arm, so **no grouping hint is added** — the
remedy for a problem that did not occur is a fixture nobody can later explain.

## The decision

**`make test` and `make cov` run on four workers, and `PYTEST_WORKERS=0` makes either serial.**

**4 and not `auto`, because `-n auto` prices a resource nobody counts.** It takes `os.cpu_count()`,
and every worker is its own process that draws its own Postgres pool: at `CHEMCLAW_PG_POOL_MAX_SIZE`'s
default of 16 that is up to 16 backends per worker against one server, so a 16-core developer box would
ask a stock `max_connections` of 100 for four times what it has, while the 4-core runner sits inside
it. 4 is the width this was measured at and the width `check` has. It is a named, overridable
Makefile variable with that reason written beside it, not a literal in a recipe.

**`PYTEST_WORKERS=0` exists for one specific act**: a failure that appears only in parallel is
evidence about the scheduler, not about the code, and the serial run is what tells those apart. The
flag is *absent* rather than `-n 0` in that case, because xdist reads `0` as "no workers" and still
installs its machinery.

**No `xdist_group` marker anywhere in `tests/`, and the reason is a coupling worth stating.** This
suite runs under `--strict-markers` with **no** markers registered at all — deliberately, so that any
unknown `@pytest.mark.*` is a typo rather than a silent no-op. `xdist_group` is registered by
pytest-xdist, so a single use of it in `tests/` would make the suite unrunnable for anyone who
installed the project without the `dev` group. Nothing needs one (see above), and if something ever
does, that coupling is the cost to weigh.

## What it costs

**Parallelism buys wall clock with CPU, and the figures say how much.** Serial `cov` spends 23m40 of
CPU over 27:25; four workers spend **41m24** over 12:28 — 1.75x the CPU for 0.45x the wall clock,
almost all of it coverage tracing duplicated per worker. On a shared runner that is the right trade
(the job's wall clock is what blocks a merge); on a laptop running other things it is not, which is
what `PYTEST_WORKERS` is for.

**A parallel run's output is harder to read.** Failures arrive interleaved and without the
per-file progress line, so the file a failure is in comes from the short summary rather than from
scrollback. `tests/conftest.py`'s epilogue is unaffected — it runs on the controller over aggregated
stats, and the skip sections and timeout section printed correctly in both parallel arms.

**One residue, recorded rather than fixed**: four worker processes each draw their own
`tests/pg.py::TEST_SCHEMA` (a fresh `uuid4` per process, which is why the suite was parallel-safe
already), so a run that is killed leaves up to four orphan `chemclaw_test_*` schemas where a serial
run leaves one. `tests/pg.py` already documents that nothing sweeps orphans; four workers make that
four times likelier, not newly true.

**`pytest-randomly` × xdist is unmeasured.** Every arm above ran `-p no:randomly` to keep the
comparison about workers rather than about order. The plugin is not installed in this project, so
nothing is regressed; it is named here because the combination is the obvious next thing somebody
reaches for.

## What keeps it true

Nothing new, and that is deliberate: the subject is a *developer entrypoint*, and what holds it is the
entrypoint itself plus the existing gate. `tests/test_repo_map.py` derives the validator list from the
`ci` target, so the Makefile is already read by the suite; `make -n test` and `make -n test
PYTEST_WORKERS=0` print `uv run pytest -n 4` and `uv run pytest` respectively, which is the whole of
the new logic and is checked by using it.

A test asserting "the suite passes in parallel" would be a test that runs the suite, so the evidence
for this decision is the table above rather than a ratchet. What a future regression looks like is a
red `check` job, which is the instrument that already exists.
