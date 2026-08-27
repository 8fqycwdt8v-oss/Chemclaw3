# BoFire deep audit — fixes

Six parallel research passes over the BO layer (engine, connector, persistence, decision history,
tests, frontend), then every finding either fixed, or refused with the reason written down. The
decision record is
`docs/decisions/D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel.md`.

## Fixed

- [x] **Evaluation budget is bounded** — `require_evaluations_within_budget` over
      `n_initial + n_rounds * batch` against the new `bo_max_evaluations`. `bo_max_rounds` bounded a
      loop counter while `batch` was unbounded, so a spec *inside* the 500-round ceiling could ask
      for 20 000 objective evaluations.
- [x] **Exhaustion counts both sides the same way** — `point_is_feasible` +
      `distinct_feasible_candidate_count`. `discrete_candidate_count` counts feasible cells;
      the history it was compared against did not, so a run an exclusion later forbade consumed a
      cell it was never part of and stopped a campaign early.
- [x] **The same asymmetry in the progress *report*** — found while fixing the above, not in the
      audit. `n_distinct / design_space` could render "7 distinct out of the 6 the grid holds".
      New `n_distinct_in_space` field; `n_distinct` keeps meaning what was run.
- [x] **`campaign_progress` off the event loop** — the one compute-bearing tool on the surface
      still doing its work on the loop thread.
- [x] **The cross-product walk is bounded** — `bo_max_enumerated_cells`; ten categorical parameters
      of ten options is 10^10 cells, reached synchronously from a request.
- [x] **Screening-design size is bounded** — `_require_design_fits_the_ceiling`,
      `bo_max_design_runs`. **My first version of this guard had the very bug class it was written
      to prevent** (an early break made the product partial, so a reduced design could shift back
      under the ceiling: measured, 40 factors at 1 generator passed on 8 192 against a true 2^39).
      Fixed and pinned by its own test.
- [x] **The durable campaign records per round** — was one write after the loop, so a cancelled,
      terminated or non-retryably failed campaign answered `resume_campaign` with "no such
      campaign" about hours of paid evaluation. Keyed `"{workflow_id}:r{n}"`; `CampaignCarryOver`
      carries `rounds_done` so a continue-as-new does not collide. Proven end to end against a real
      Temporal server.
- [x] **The fork flag comes from the write** — `RETURNING (xmax = 0)` instead of a `SELECT` before
      the upsert that raced it. `campaign_is_known` deleted (no other caller).
- [x] **`CampaignThread` drops `opened_by`** — the column stays for the audit trail; an
      `unverified:` self-assertion must not travel back out as provenance.
- [x] **One statement of the single-best-point rules** — `require_problem_yields_one_best_point`,
      shared by `optimize` and `require_campaign_startable`, which had two wordings of one refusal.
- [x] **Retention names every table** — the docstring implied an exhaustive refusal list and named
      3 against 33. `_NOT_PRUNED` plus a test derived from the migrations on disk.
- [x] **NaN threshold refused in fingerprint search** — `min(max(nan, 0), 1)` is `nan`; an exact
      self-match came back empty *with a verdict announcing a genuine negative*.
- [x] **Temporal skips are counted** — `_report_temporal_skips`, mirroring the Postgres epilogue.
      Three real-workflow tests skipped silently when the test-server binary can't be fetched.
- [x] **The frontend renders a campaign** (`Chemclaw3_ui`) — best-so-far step chart with the assay
      noise band, Pareto scatter for exactly two objectives, candidate cards with `predicted_sd`
      tied to its value, extrapolation marked. Three verdict states, never two: a withheld plateau
      verdict is not "still improving".

## Refused, with the reason

- [x] **Multi-objective on the durable path** — not an oversight. `bo.objectives` maps a name to a
      scalar-returning callable and holds two entries, both scalar; a multi-output registry would
      be an abstraction with zero callers, built to make a refusal message unnecessary. The
      refusal already names the inline tool that does do it. ADR §"What was deliberately not done".
- [x] **A pause signal on the workflow** — cancel-then-resume *is* pause once the history survives
      the cancel, and Temporal's cancellation is already exposed through core's `cancel_job`.
- [x] **Deleting `science/bo/campaign.py`** — three test callers carry real weight
      (`test_reizman.py`'s beat-the-median bar); deleting it inlines one loop into three files. The
      defect it was flagged for was the triplicated preconditions, fixed above.
- [x] **NChooseK constraints** — `bo-capability-map.md` already records `REFUSED — no story`.
      Building it would be capability with no caller.

## Verification

- `make lint` / `ruff format --check` / `mypy --strict` — clean (398 source files).
- Backend BO suite + touched neighbours — see the review section below for the real numbers,
  including what skipped.
- `Chemclaw3_ui`: 548 tests, typecheck and lint clean.
- Postgres and Temporal both **started and used** (`dockerd`, `make up`, `make db-migrate`), so the
  durable and store-backed tests genuinely ran rather than skipping.

## Review

Four of the seven backend fixes are one mistake wearing different clothes: a quantity that is
checked and a quantity that is spent, differing by a factor nobody multiplied. Rounds against
evaluations. Feasible cells against distinct runs. A ceiling on a count against the product it came
from. The progress report's coverage sentence. That is worth carrying beyond this branch, and it is
why the ADR ends on it rather than on a list of files.

The sharpest evidence for it: **the guard I wrote to bound design size shipped, in its first
version, with exactly that defect inside it** — a partial product compared against a ceiling. It was
caught by writing the arithmetic out and running it, not by reading it. Same lesson the repo already
states about prose; a bound's name is prose too.
