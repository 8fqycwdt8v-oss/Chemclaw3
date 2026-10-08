# D-2026-10-08-the-test-gate-runs-in-parallel — the gate runs on xdist workers, and a parallel-only failure is a concurrency defect to fix

**Status:** accepted · **Date:** 2026-10-08

## Context

`D-2026-09-13-a-stable-failure-set-is-not-two-green-runs` kept the gate serial. Four workers measured
18:13 → 09:30 on `make test`, but two tests failed only in parallel (2 in 5 and 1 in 5 runs). W1
isolated one of them, the retention VACUUM test, in a private database. The other did not reproduce
under load. The architecture programme (W3) is about exactly the defect class a parallel-only failure
belongs to: state shared across processes.

## Options

1. **Stay serial** (D-2026-09-13). The gate stays deterministic, it costs ~18 minutes per run, and a
   real concurrency defect stays invisible until production.
2. **Go parallel and retry or quarantine the flaky tests.** This is fast, but a retried failure is
   a finding nobody reads.
3. **Go parallel and root-cause every parallel-only failure.** This is fast, and each such failure
   is treated as a defect in test isolation or in the product.

## Decision

**Option 3, by owner decision.** The flip happens in programme items W3.10–W3.12:
- Collect the failure set over 10 parallel runs on CI's runner class.
- Fix each failure at its cause: per-worker state, events instead of sleeps, or the product code.
- Then make `PYTEST_WORKERS ?= 4` the default for `test` and `cov`, and run CI the same way.

No test is skipped, retried or quarantined. A test that is about server-global state runs in one
`xdist_group`, and its file says why.

## Consequences

- Gate wall time is expected at ≤10 minutes (measured 09:30 at 4 workers).
- A nightly serial `make cov` stays as a cross-check: a serial-only failure is its own finding.
- `CLAUDE.md`'s command line and `CURRENT.md` change in the PR that flips the default.
- Until that PR, the gate stays serial.
- `tests/conftest.py` caps each worker's Postgres pool, so N workers do not exhaust
  `max_connections`.

Supersedes `D-2026-09-13-a-stable-failure-set-is-not-two-green-runs`.

Revisit when: a parallel-only failure recurs after it was root-caused. That is two red parallel runs
in CI on one test within a week, recorded in the nightly serial cross-check's report.
