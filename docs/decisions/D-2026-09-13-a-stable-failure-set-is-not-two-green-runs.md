# D-2026-09-13-a-stable-failure-set-is-not-two-green-runs — xdist is 2x and stays opt-in, because the gate's answer has to mean one thing

A `BACKLOG.md` row has held `pytest-xdist` open since
`D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one` took the free half of
the CI-duration problem. Its closing condition was one experiment — add the plugin, run it, and compare
the wall time **and the failure set** against serial on the same commit — and its instruction for a
negative result was explicit: *"if it is not a clear win, say so and delete this row."*

The wall time is a clear win. The failure set is not stable. This records both, and it records that the
first version of this ADR said the opposite on the strength of two runs.

## What was measured

Two premises of the row needed correcting before any number meant anything. **The machine is the same
shape as the runner** — this box is 4 cores / 16 GB and `check` runs on `ubuntu-latest`, which is
4 vCPU / 16 GB — so a local figure is no longer a different class of machine, which is what the row
was guarding against. And **CI does not run `make test`**; it runs `make cov`, which is where the
parallel-safety question actually bites, because `pytest-cov` has to combine across workers or the
84 % floor becomes a different quantity.

The speed, on `08023695`, Docker/Postgres/Temporal up:

| arm | wall clock | CPU (user) | coverage |
| --- | --- | --- | --- |
| `make test`, serial | **18:13** | 16m04 | — |
| `pytest -n 4` | **09:30** | 32m13 | — |
| `make cov`, serial | **27:25** | 23m40 | **90.53 %** |
| `make cov -n 4` | **12:28** | 41m24 | **90.53 %** |

**1.92x on the suite, 2.20x on the gate CI runs, and the coverage total is identical to two decimal
places** — so `pytest-cov` does combine across workers and the floor keeps its margin.

The stability, over **five** full parallel runs at `-n 4`:

| test | parallel runs failed | serial |
| --- | --- | --- |
| `test_context_budget.py::test_a_burst_of_cold_prefix_measurements_leaves_the_loop_schedulable` | **2 of 5** | passes |
| `test_retention.py::test_a_pass_reports_bytes_beside_rows_and_stops_the_table_growing` | **1 of 5** | passes |

Both were run serially, on their own, after each parallel failure: both pass. So the parallel gate
reds for a reason that is not a finding in roughly **two runs in five**.

One of those is a test whose docstring *already* records being corrected for exactly this, at a lighter
load: *"what the old form actually measured was how much the twelve busy-waits happened to overlap —
thread-pool scheduling luck against core count and machine load — and it failed about one run in five,
on CI at 129 ms against a 128 ms line, a 0.8 % miss."* Its correction made the comparison
in-process — async against a synchronous control doing the same 96 conversions — which is load-immune
in principle and is not immune to three other pytest workers saturating four cores.

(Every arm above ran `-p no:randomly`, and the one failure present in all of them is environmental and
pre-existing: `tests/test_calc_fake_identity.py` drives the sibling `Chemclaw3-mcp` checkout's own key
derivation and this host has neither `xtb` nor `crest` on `PATH`. Verified by running that file at the
merge base in a separate worktree — same failure.)

## The decision

**`pytest-xdist` is installed and `PYTEST_WORKERS` defaults to 0.** `make test PYTEST_WORKERS=4` is
the opt-in, and the speedup is real enough to be worth reaching for in a local iteration loop. The
**gate stays serial**, because a gate that reds two runs in five for a scheduling artefact is worse
than a slow one: the first spurious red teaches everybody to re-run, and then a real red teaches them
the same thing. This is the row's "say so" branch, with the blocker named rather than the capability
deleted.

**Not `auto` even when opting in.** `-n auto` takes `os.cpu_count()`, and every worker is its own
process drawing its own Postgres pool — at `CHEMCLAW_PG_POOL_MAX_SIZE`'s default of 16 that is up to 16
backends per worker against one server, so a 16-core developer box would ask a stock `max_connections`
of 100 for four times what it has. 4 is the width measured and the width the runner has.

**`--dist loadgroup` is declined, and the reason is that it does not address this.** The hint
co-locates named tests on one worker; it does not reduce the load the *other* workers put on the
machine, which is what these two tests are sensitive to. A fixture that looks like a remedy and is not
one is worse than the flake, because it ends the investigation.

**No `xdist_group` marker anywhere in `tests/`**, which the above makes moot and which would have a
cost anyway: this suite runs under `--strict-markers` with **no** markers registered at all —
deliberately, so an unknown `@pytest.mark.*` is a typo rather than a silent no-op — so one use of
`xdist_group` would make the suite unrunnable for anyone who installed the project without the `dev`
group.

## What the first version of this ADR got wrong, and why it is worth writing down

It was named `four-workers-is-a-third-of-the-wall-clock-and-a-different-failure-set`, it changed the
default to four workers, and its headline claim was *"the failure set is identical in every arm"*. It
was written after **two** parallel runs, both of which happened to be clean. The very next full run —
the verification run taken *because* the default had changed — failed two extra tests, one of them on
the list of four a prior reading had predicted and this ADR had dismissed as a problem that did not
occur.

The defect is not the wrong conclusion; it is the sample size. Two green runs of a 8,800-test suite is
a perfectly ordinary outcome for a 40 %-per-run flake, and the claim made from them ("identical")
asserted *stability*, which two samples cannot establish in the direction of "always". The brief's
prediction was right and the measurement that contradicted it was underpowered.

**The rule this leaves**: a claim that a *set* is stable needs repetition, and the repetition count
belongs in the claim. `tasks/lessons.md` carries it.

## What it costs, and what is left

Parallelism buys wall clock with CPU: serial `cov` spends 23m40 over 27:25, four workers **41m24** over
12:28 — 1.75x the CPU for 0.45x the wall clock, almost all of it coverage tracing duplicated per
worker. Output is interleaved, so a failure's file comes from the short summary rather than scrollback.
`tests/conftest.py`'s epilogue is unaffected: it runs on the controller over aggregated stats, and its
skip sections printed correctly in every parallel arm.

Four worker processes each draw their own `tests/pg.py::TEST_SCHEMA` (a fresh `uuid4` per process,
which is why the suite is parallel-safe at all), so a killed parallel run leaves up to four orphan
`chemclaw_test_*` schemas where a serial run leaves one. `tests/pg.py` already documents that nothing
sweeps orphans.

**What would make the gate parallel** is the two named tests becoming insensitive to machine load —
which for the first means a bound that is not a latency ratio at all, and is a change to a test whose
docstring argues carefully for the ratio it has. That is a `BACKLOG.md` row, not a passing edit.
`pytest-randomly` × xdist remains unmeasured (the plugin is not installed here).

## What keeps it true

Nothing new, deliberately: the subject is a developer entrypoint plus a posture. `make -n test` and
`make -n test PYTEST_WORKERS=4` print `uv run pytest` and `uv run pytest -n 4`, which is the whole of
the new logic. A test asserting "the suite passes in parallel" would be a test that runs the suite —
and, on these numbers, would itself fail two runs in five.
