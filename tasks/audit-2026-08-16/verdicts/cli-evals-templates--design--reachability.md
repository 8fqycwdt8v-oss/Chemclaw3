# Verdicts — `cli/`, `evals/`, `templates/` — design (reachability lens)

Scope: only findings marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/cli-evals-templates--design.md`.
That file has **one** — the `check_result_cached` finding (high). The other thirteen are
medium/low and are out of scope; no verdict is given for them.

---

## `check_result_cached` counts the whole table, so it passes on any earlier run's residue

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

1. Read `src/chemclaw/cli/live_jobs.py` in full. The quoted code is verbatim: line 225-227 is an
   unqualified `select count(*) from calculation_results where calc_type like 'xtb%'`, with no
   bound on `run.workflow_id`, `created_at` or species. `check_result_cached` is also the only
   check in `run_smoke`'s list that takes no `run` argument (`live_jobs.py:361`), so it
   structurally cannot see the run it is reporting on.

2. Reproduced the mechanism against the live Postgres (`infra-postgres-1`, up and healthy),
   with no smoke run in the process:

   ```
   $ uv run python /tmp/probe_cached.py
   pre-existing xtb* rows: 0
   no-smoke check -> False | 0 xtb* row(s) in calculation_results
   after planting ONE unrelated row -> True | 1 xtb* row(s) in calculation_results
   after deleting it -> False | 0 xtb* row(s) in calculation_results
   ```

   (planted `key='audit-probe-key-reach', calc_type='xtb_energy'`, deleted in the same script;
   the table is back at 0 rows.)

3. Checked the reporter's supporting claim that a repeat run writes no new cache row. It holds,
   and by reading rather than by citation: `SMOKE_PAYLOAD` is `level: "quick"`, so
   `reaction_energy` sets `thermo = None` (`connectors/calc/compose.py:717`) and `_species_energy`
   takes the `thermo is None` branch calling only `relax` (`compose.py:614-615`). The resulting
   `xtb.opt` key is built from structure + solvent; `temperature_k` never reaches a
   `CalculationKey`. So `_RUN_TEMPERATURE_K` moves the workflow id and nothing else, and every run
   after the first writes zero new `xtb%` rows. `calc_type` values really are `xtb.opt`/`xtb.hess`/
   `xtb.energy`, so the `like 'xtb%'` predicate does match.

4. Checked what else would have to break for this check to be the deciding one, since that is the
   reporter's stated consequence. `cached_compute` (`science/calc/store.py`) does not swallow a
   persist failure — `store.put` raising propagates out of the activity, so a *hard* cache-write
   failure fails the workflow and is caught by `check_workflow_completed`. `PostgresStore.put`
   swallows nothing either. And the durable path has no in-memory fallback: `connectors/calc/
   activities.py` and `connectors/calc/server/tools.py` both construct `PostgresStore`
   unconditionally, so "the deployment was configured with the wrong store" is not a reachable
   trigger.

5. Checked whether an automated gate is fooled. It is not: `Makefile:287` states "none of these is
   in `make ci`", `grep -rln "live-jobs\|live_jobs" .github/` returns nothing, and `make live-jobs`
   is a manual target run against a deployment.

6. Checked the severity against the reporter's own scale in the same file.
   `family_b_tool_truth` is the byte-identical defect — `select count(*) from audit_events where
   tool = %s`, unqualified, over a table with residue, in a lane whose `main` also returns
   `0 if all(f.ok for f in findings) ... else 1` (verified at the tail of `live_storm.py`) — and
   the reporter rates it **medium**. The reporter also rates `judge_outcome` crashing and
   destroying an entire paid live grading run **medium**.

### Why

The mechanism is real, reproduces exactly as written, and the trigger is reachable by the intended
real caller (an operator running `make live-jobs` against a database that has been smoked before —
i.e. every run after the first). I do not dispute any of that, and the fix the reporter proposes is
the right one.

Two things do not hold.

**The consequence is narrower than "the difference between a green lane and a red one."** For this
check to flip the run's verdict it has to be the *only* failing check, and the failure classes that
produce that are few once you trace them. A hard persist failure raises out of `cached_compute` and
fails the workflow, so `check_workflow_completed` catches it first. There is no store-backend
misconfiguration to catch, because the backend is not configurable. What is genuinely masked is
narrower: DSN drift between the connector worker and the smoke process, or `calc_type` naming drift
from the physics server now living in `Chemclaw3-mcp` (`remote_key` reads `key["calc_type"]` off the
wire, so the prefix this check hard-codes is no longer a fact this repository owns). Those are real
and worth catching — but "a false pass here is the difference between a green lane and a red one"
reads as though any cache regression slips through, and most of them do not, because a different
check fires first.

**The `high` label is not supportable on the reporter's own scale.** This is harness code. No
chemist is shown anything derived from it, no answer is wrong, no data is lost, no production path
is involved, and no CI gate is deceived — the failure mode is loss of signal in a manually invoked
smoke lane. The reporter offers exactly one argument for the elevation, `main()`'s exit code, and
that argument applies word for word to `family_b_tool_truth`, which is the same unqualified-count
defect in the same family of lanes with the same exit-code consequence, and which the same file
rates medium. A file cannot rate one instance of a defect medium and a second instance of the same
defect high on a differentiator both instances share.

Medium, fix it, apply the same `since = select now()` pattern `family_d_durable` already uses — and
apply it to `family_b_tool_truth` in the same change, since it is the same bug.

### Worth adding, which the finding missed

`check_idempotent` is degenerate for this purpose too, and the finding cites it as "the model
already in this file". It relaunches the *identical* payload, so `job_workflow_id` derives the same
id and the launch rejoins rather than computing — meaning `before == after` holds whether the cache
is working or entirely broken. It is a real check of the idempotency contract; it is not a second,
independent observation of the cache write, and a reader could mistake it for one.
