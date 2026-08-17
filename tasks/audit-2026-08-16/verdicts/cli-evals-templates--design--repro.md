# Repro verdicts — `cli-evals-templates--design.md`

**Scope note.** The findings file contains exactly **one** finding at critical/high severity
(`check_result_cached counts the whole table…`, high). Every other finding in the file is marked
medium or low and is out of scope for this pass; none were verified.

---

## `check_result_cached` counts the whole table, so it passes on any earlier run's residue

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  First, the tree I read is clean:

  ```
  $ diff /tmp/.../scratchpad/pristine/src/chemclaw/cli/live_jobs.py src/chemclaw/cli/live_jobs.py
  IDENTICAL_TO_PRISTINE      (HEAD = 85735693)
  ```

  Line numbers and symbols check out against the current file: `check_result_cached` is defined at
  `src/chemclaw/cli/live_jobs.py:218`, its query is at 225–227, it takes **no arguments** and is
  registered in `run_smoke`'s check list at line 361 as the bare callable `check_result_cached`
  (every other check is a `lambda` closing over `run`). `main()` returns `0 if run.ok else 1`
  (line 415) and `SmokeRun.ok` is `all(check.passed …)` (line 168).

  I did not run the reporter's script. I wrote my own (`/tmp/repro_ccached.py`, since deleted),
  which imports `check_result_cached` and calls it directly — **no Temporal launch, no smoke, no
  job in this process at all** — against the live Postgres (`infra-postgres-1`, up 4h), then plants
  a single row that has nothing to do with any smoke and calls it again:

  ```
  pre-existing xtb* rows: 0  (total rows: 0)
  [state A] passed=False observed='0 xtb* row(s) in calculation_results'
  [state B: one 400-day-old unrelated row planted] passed=True observed='1 xtb* row(s) in calculation_results'
  probe row cleaned up, remaining: 0
  ```

  The planted row was `key='audit-verifier-probe-key', calc_type='xtb_energy',
  calc_version='0.0-audit', input_hash='no-such-molecule', created_at = now() - interval '400 days'`
  — a row for a molecule that does not exist, written by no job, older than the repository's own
  history in this checkout. It flips the check to PASS. The row was deleted afterwards and the table
  is back to 0 rows (the count above is the shared checkout's actual state, not a fixture).

  I also checked for an upstream guard that would make this unreachable: there is none.
  `make live-jobs` is `uv run python -m chemclaw.cli.live_jobs` with no database reset
  (`Makefile:314`), and neither `infra/live/bootstrap.sh` nor `infra/live/processes.sh` truncates or
  drops anything (`grep -n 'drop\|truncate' infra/live/*.sh` → no schema resets). `durable/retention.py`
  explicitly **refuses** to prune `calculation_results`, so residue is permanent by design.

- **Why**

  The mechanism is exactly as described and stronger than the write-up says.

  1. **The query has no bound of any kind** — not on `run.workflow_id`, not on `created_at`, not on
     the species or method the smoke actually asked for. It cannot: the function does not receive
     `run`. It asks a global, all-time question and reports the answer as this run's evidence, under
     a docstring claiming it is "the D-011 guarantee made observable".

  2. **The consequence reproduces with zero scaffolding.** One arbitrary pre-existing `xtb%` row —
     from any run, any species, any decade — is sufficient and I demonstrated it. The reporter's
     reachability argument (temperature varies the *workflow id* but `calculation_results` is keyed
     on species+method, so repeats write nothing new) is a secondary path and I did not need it:
     the check is defeated by residue whether or not the current run wrote anything.

  3. **The thing the reporter missed makes it worse: no other check in the lane covers the gap.**
     Consider a regression where the job computes correctly and returns a number but persists
     nothing (exactly the failure this check's docstring names — "a workflow that returned a number
     without persisting it would look identical from the summary alone"). Walk the five checks:
     `check_workflow_completed` passes (the workflow did complete); `check_job_recorded` passes
     (`job_records` is a different table, written by `record_job`); `check_idempotent` passes
     because its verdict is `same_id and before == after` (line 275–279) and a system that never
     writes gives `before == after` trivially; `check_pending_when_worker_wedged` passes. And
     `check_result_cached` passes off the residue. **All five checks green, exit 0, on a system
     where the D-011 persistence guarantee is entirely broken.** `check_idempotent` — which the
     finding holds up as "the model already in this file" — is itself only a delta check and is
     equally blind here; it is `check_result_cached` alone that is supposed to assert persistence
     happened, and it asserts nothing.

  The one thing I would soften in the write-up is nothing material: the claim "from run two onward
  this check is structurally incapable of failing" is true only for a database that has ever held an
  `xtb%` row — which, given retention refuses to prune the table, is every database the lane has ever
  been run against more than once. That is a distinction without a difference.

  Severity: I keep **high**. This is harness code rather than production code, which normally argues
  down — but the lane's entire product is its exit code, the defect converts the sole check of a
  named architectural guarantee into a tautology, and no sibling check compensates. The proposed fix
  (capture `select now()` before `_launch`, bound the count on `created_at >= %s`, and state whether
  the run computed or the cache answered) is the right shape and is what `family_d_durable` already
  does at `live_storm.py:351,355`.

- **Working-tree hygiene**: no source file was modified. The one database row I planted was deleted
  in a `finally` block and its absence verified in the same run. No `git stash`/`checkout` was run.
