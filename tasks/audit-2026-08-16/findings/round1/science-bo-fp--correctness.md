# science/bo + science/fingerprints — CORRECTNESS

Slice: `src/chemclaw/science/bo/` (problem, engine, campaign, objectives, featurize, progress,
campaign_record, campaign_record_store, benchmarks) and `src/chemclaw/science/fingerprints/`
(store, molfp, rxnfp).

Every finding below was reproduced by running the code. Scripts live under `/tmp/audit/`.

---

## A "full factorial" over mixed categorical + continuous factors is not the Cartesian product — factors come out perfectly aliased and the design is labelled exhaustive

- **Severity**: critical
- **Location**: `src/chemclaw/science/bo/engine.py:831` (`_full_design`), reached from
  `engine.factorial_design:928` and `connectors/bo/server/tools.py:672`
  (`generate_screening_design`). The false claim is emitted by
  `src/chemclaw/science/bo/problem.py:628` (`ScreeningDesign.summary`).
- **Trigger**: any `factorial_design(problem)` with `n_generators=0` (the default) where the
  problem declares **at least one categorical and at least one continuous** parameter and the
  number of continuous design rows shares a factor with the number of categorical combinations
  (i.e. `gcd(N_cont_rows, N_cat_combos) > 1` — which is every all-two-level problem).
- **Consequence**: the returned runs are a *diagonal*, not a cross product. Combinations are
  missing, the surviving rows are duplicated, and one factor ends up perfectly confounded with
  another — while `ScreeningDesign.resolution` is `None` and `summary` says
  *"Full factorial … Exhaustive over the levels stated: every combination of them is run."*
  A chemist runs the plate, sees a solvent effect, and it is a temperature effect. This is a
  screening design whose entire purpose is to attribute effects to factors, and it cannot.
- **Evidence**:

  `_full_design` hands the whole mixed domain to BoFire and returns whatever `ask()` gives:

  ```python
  frame = strategies.map(
      FractionalFactorialStrategy(
          domain=_to_domain(problem), n_center=n_center, n_repetitions=n_repetitions
      )
  ).ask()
  runs = [{p.name: _cast(p, row[p.name]) for p in problem.parameters} for _, row in frame.iterrows()]
  ```

  BoFire's combining step (`bofire/strategies/fractional_factorial.py`, `_ask`) **tiles both**
  frames instead of repeating one and tiling the other:

  ```python
  design = pd.concat(
      [pd.concat([design] * len(categorical_design), ignore_index=True),
       pd.concat([categorical_design] * len(design), ignore_index=True)],
      axis=1,
  ).sort_values(by=..., ignore_index=True)
  ```

  Row *i* therefore pairs `continuous[i % N]` with `categorical[i % C]`. That enumerates the cross
  product only when `gcd(N, C) == 1`; otherwise it yields `lcm(N, C)` distinct rows, each repeated
  `gcd(N, C)` times.

  Measured (`/tmp/audit/p11.py`, `/tmp/audit/p12.py`, two-level categoricals throughout):

  ```
  cats=1 conts=1 {}: rows=4  distinct=2  expected_combinations=4  MISSING=2
  cats=2 conts=1 {}: rows=8  distinct=4  expected_combinations=8  MISSING=4
  cats=2 conts=2 {}: rows=16 distinct=4  expected_combinations=16 MISSING=12
  cats=3 conts=1 {}: rows=16 distinct=8  expected_combinations=16 MISSING=8
  cats=3 conts=2 {}: rows=32 distinct=8  expected_combinations=32 MISSING=24
  cats=3 conts=1 {'n_center': 2}: rows=32 distinct=8 expected_combinations=16 MISSING=8
  ```

  The confounding, on a realistic problem (solvent × base × temperature 20–120):

  ```
  Full factorial over 3 factor(s), 8 run(s) in total. Exhaustive over the levels stated:
  every combination of them is run. T is continuous and held at the two ends …
     {'solvent': 'DMF', 'base': 'Cs2CO3', 'T': 120.0}
     {'solvent': 'DMF', 'base': 'Cs2CO3', 'T': 120.0}
     {'solvent': 'THF', 'base': 'Cs2CO3', 'T': 20.0}
     {'solvent': 'THF', 'base': 'Cs2CO3', 'T': 20.0}
     {'solvent': 'DMF', 'base': 'K2CO3',  'T': 120.0}
     {'solvent': 'DMF', 'base': 'K2CO3',  'T': 120.0}
     {'solvent': 'THF', 'base': 'K2CO3',  'T': 20.0}
     {'solvent': 'THF', 'base': 'K2CO3',  'T': 20.0}
  ```

  DMF is **only ever run at 120 °C** and THF **only at 20 °C**. Four of the eight runs are exact
  duplicates; THF@120 and DMF@20 are never run for either base.

  **The repo's own test fixtures are inside the broken region, and the tests pass** because they
  assert run *counts* and per-column value *sets*, never the set of distinct rows
  (`/tmp/audit/p13.py`):

  - `tests/test_bo_doe.py::test_a_continuous_factor_is_screened_at_its_two_bounds_and_said_to_be`
    asserts `len(design.runs) == 4`, `{20.0, 100.0}` and `{THF, toluene}`. The actual design is
    `{(20, THF), (100, toluene)}` each twice — 2 of the 4 combinations, temperature perfectly
    aliased with solvent. Every assertion passes.
  - `_mixed_problem()` (2 continuous + 1 two-level categorical), used by five tests including
    `test_the_default_is_no_centre_runs_although_bofire_defaults_to_one` (`len(runs) == 8`), yields
    4 distinct rows of 8, with `equiv` perfectly aliased to `solvent`:

    ```
    THF     [(20.0, 1.0), (120.0, 1.0)]
    toluene [(20.0, 3.0), (120.0, 3.0)]
    ```

  - The one test that *does* check the Cartesian product,
    `test_factorial_design_enumerates_every_combination`, uses an **all-categorical** problem —
    the path that is not broken. So does the 3-level-categorical case (`gcd(2,3)=1`), which is why
    a spot check with an odd level count looks correct.

  `_fractional_design` (`n_generators >= 1`) is **not** affected: it re-encodes every factor as a
  `ContinuousInput` on `[0,1]`, so BoFire returns the continuous design directly and never reaches
  the combining step. Verified correct for `(cats, conts, n_generators)` = (3,1,1), (2,2,1),
  (4,2,2) — full distinct fractions with the right resolution. All-categorical and all-continuous
  full grids are also correct.

  Note that `factorial_design`'s docstring cites a measurement of this path
  (*"measured: 4·2^k + n_center·2^k over k categoricals"*) — the run **count** was measured and
  matches; the run **content** was never checked.

- **Fix**: stop letting BoFire pair the two halves. In `_full_design`, mirror what
  `_fractional_design` already does in reverse: build the continuous rows from a
  continuous-only `Domain` (so `FractionalFactorialStrategy` returns `_get_continuous_design()`
  directly, honouring `n_center`/`n_repetitions`), then cross those rows with
  `itertools.product(*(p.categories for p in categoricals))` in Python. That is one explicit
  Cartesian product and it also makes the "centre runs per categorical combination" behaviour
  something this repo states rather than inherits. When the problem has no categorical (or no
  continuous) parameter, the current single-strategy call is already correct and can stay.

  The regression test must assert `{frozenset(run.items()) for run in runs}` equals the Cartesian
  product of the levels — length assertions and per-column `set()` assertions both pass on the
  diagonal, which is how this survived.

---

## An excluded pairing in the run history makes the optimizer declare a finite space exhausted while feasible conditions remain unrun

- **Severity**: high
- **Location**: `src/chemclaw/science/bo/engine.py:420` (`_require_fresh_points_exist`) and
  `src/chemclaw/science/bo/problem.py:1120` (`space_exhausted`), both comparing
  `distinct_candidate_count(observations)` against `discrete_candidate_count(problem)`.
- **Trigger**: an all-categorical problem carrying an `ExcludeConstraint`, with a run history that
  contains at least one run of the excluded pairing. This is the *normal* way an exclusion arrives:
  the chemist forbids Pd(OAc)₂/DMF **because** they ran it and it decomposed, and that run is still
  evidence they hand to `suggest_next_experiment`. The tool boundary
  (`_require_observed_params_match`, `connectors/bo/server/tools.py:239`) checks parameter *names*
  only, so an infeasible historical run passes straight through.
- **Consequence**: `discrete_candidate_count` deliberately subtracts excluded cells (W4), while
  `distinct_candidate_count` counts every distinct history row including the infeasible ones. The
  two are compared as if they were the same set, so `run >= space` fires early.
  `propose_candidates` then raises *"this decision space holds 3 distinct condition(s) and all 3
  have been run … The screen is complete"* — a confident, wrong statement — and the durable/inline
  campaign loop stops via `space_exhausted`. A genuinely unrun, feasible condition is silently
  dropped, and BoFire would have proposed it.
- **Evidence** (`/tmp/audit/p6.py`, `/tmp/audit/p7.py`):

  ```
  feasible space: 3                       # 2x2 minus the (PdOAc2, DMF) pairing
  distinct in history: 3                  # PdOAc2/THF, PdOAc2/DMF (the excluded run), Pd2dba3/THF
  never run, feasible: {'cat':'Pd2dba3','solv':'DMF'}
  space_exhausted(batch=1): True
  propose_candidates RAISED ValueError : this decision space holds 3 distinct condition(s) and
    all 3 have been run, so there is no fresh point left to propose. The screen is complete …
  ```

  With the guard bypassed, BoFire answers the same call correctly:

  ```
  BoFire without the guard proposes: {'cat': 'Pd2dba3', 'solv': 'DMF'}
  ```

  The same mismatch reaches the chemist as a nonsense sentence from
  `CampaignProgress._space_clause` (`progress.py:155`): with four runs (one of them excluded) over
  a 3-cell feasible space it renders *"4 distinct condition(s) out of the 3 the full grid holds"*
  (`/tmp/audit/p14.py` — `n_distinct: 4  design_space: 3`).

- **Fix**: count the history against the same set `discrete_candidate_count` counts. Add a
  feasibility filter used by both sides — e.g. a `feasible_candidate_keys(problem, observations)`
  helper in `problem.py` that drops observations forbidden by any `ExcludeConstraint` (the
  predicate already exists, `ExcludeConstraint.forbids`) before `params_key`-deduplicating them,
  and have `_require_fresh_points_exist`, `space_exhausted` and `CampaignProgress.n_distinct` all
  read it. `discrete_candidate_count` already enumerates the feasible cells; reusing that
  enumeration is the honest comparison. The refusal message should then be reachable only when the
  feasible cells really are all run.

---

## A replicated or centre-padded screen reports a run count that contradicts its own "not exhaustive" sentence

- **Severity**: low
- **Location**: `src/chemclaw/science/bo/problem.py:627-641` (`ScreeningDesign.summary`)
- **Trigger**: `factorial_design(problem, n_generators>=1, n_repetitions=r)` with `r > 1`, or the
  full grid with `n_center > 0`.
- **Consequence**: `summary` compares `len(self.runs)` — which includes replicate and centre rows —
  against `2**factors`, the size of the *unreplicated* full two-level grid. The two numbers are not
  commensurable, so the sentence can state that a half-fraction needs as many runs as the full grid
  while simultaneously saying most combinations are deliberately not run. A chemist planning plate
  capacity from that sentence has no way to read it.
- **Evidence** (`/tmp/audit/p9.py`, 3 two-level categoricals + 1 continuous):

  ```
  REDUCED n_repetitions=2 -> runs: 16
  Fractional factorial, resolution IV: 16 run(s) against the 16 a full two-level grid over
  4 factors would need. NOT exhaustive — most combinations are deliberately not run. …
  The factorial part is replicated 2 times, …
  ```

  The same shape appears on the full grid with `n_center=2`: *"Full factorial over 4 factor(s),
  32 run(s) in total. Exhaustive over the levels stated: every combination of them is run"* — 32
  rows for a 16-corner grid, where the extra rows are precisely the ones that are *not*
  combinations of the stated levels.
- **Fix**: compare like with like. Compute the factorial part's size
  (`len(runs) - centre_rows` divided by `n_repetitions`) and quote that against `2**factors`, or
  drop the comparison and state the fraction directly (`2**-n_generators` of the grid), then report
  the total run count separately as "plus R replicates and C centre runs, N runs in total".

---

## Checked and found sound

Recording these so the negative result is usable rather than an absence.

- **Constraint mapping senses** (`engine._constraint`, `engine._exclusion`). Verified against
  BoFire's own `is_fulfilled` (`/tmp/audit/p1.py`): `>=` negation, `<=`, `==` and the
  `CategoricalExcludeConstraint` `AND` semantics all agree with the neutral types, and
  `ExcludeConstraint.forbids` is the exact complement of BoFire's predicate. `RandomStrategy`
  seeding honours the exclusions — 0 violations at n = 2, 4, 6 over a 6-cell feasible space, all
  points distinct (`/tmp/audit/p4.py`).
- **`engine._resolution`.** Checked against `bofire.utils.doe.get_alias_structure` for every
  `(n_factors, n_generators)` BoFire will generate for 3–8 factors (`/tmp/audit/p2.py`). Every
  derived resolution matches the alias structure; no combination reaches the empty-`min()` path.
- **`_fractional_design` decoding.** The `[0,1]` re-encoding and the `< 0.5` mapping back to
  category labels round-trip correctly, and the reduced designs are full distinct fractions of the
  right size and resolution (`/tmp/audit/p3.py`, `/tmp/audit/p12.py`).
- **`_frame_to_candidates`.** A `CategoricalDescriptorInput` domain returns category *labels* in
  the parameter column (not descriptor values), and `<obj>_pred` / `<obj>_sd` are read off the
  right columns (`/tmp/audit/p5.py`).
- **`_fit_quality_from` / `_metric`.** `TrainableSurrogate.cross_validate` returns
  `(train, test, hooks)` in bofire 0.4.1, so `_, test, _` takes the held-out results, and
  `CvResults.get_metric` does default to `combine_folds=True` as the comment claims.
- **`stable_hash` / `campaign_id_for`.** `stable_hash` uses `sort_keys=True`, so the `structures`
  and `descriptors` dicts cannot fork a campaign id by insertion order; the categories and
  constraint canonicalization close the ordering paths they claim to.
- **`progress.campaign_progress` anchor logic.** The "gain measured from the last real gain"
  invariant holds: the anchor is monotone in the problem's direction, so a monotone sub-noise climb
  resets the counter once the accumulated climb beats the noise, and a single early spike keeps
  `evaluations_since_improvement` rising. (The `n_distinct` / `design_space` mismatch under an
  exclusion is folded into finding 2.)
- **Fingerprint store, in-memory vs live Postgres.** Ran 200 random 2048-bit records plus a query
  against both backends at thresholds 0.0/0.05/0.1/0.2 and k = 1/5/10/50 (`/tmp/audit/p15.py`,
  against the running `infra-postgres-1`). Hit sets and ordering are **identical** in all 16
  configurations; the largest similarity difference is 5.6e-17 (pgvector's `1 - (1 - c/u)` double
  round trip), and the in-memory value is the exact Tanimoto to within 1e-15. The round trip never
  flips a threshold decision: simulated over every `(c, u)` with `u <= 2048` at ten thresholds
  including the configured 0.3 — **0** flipped comparisons.
- **`tanimoto` / `tanimoto_bits`.** All-zero guard, width check placement, and the
  parse-once-per-query optimization are all consistent between the two forms. No real molecule
  standardizes to an all-zero fingerprint (checked salts, single ions, water, H₂).
- **`find_matches` truncation probe.** `k+1`-then-slice correctly distinguishes "a full page" from
  "more qualified", and the `[1, fingerprint_max_top_k]` / `[0, 1]` clamps are applied before the
  SQL `LIMIT` and comparison.
- **`_scan_for_matches`.** The cap bounds what is *returned* while the scan continues, so
  `hits_truncated` is observed rather than inferred from `len == cap`; unreadable rows are counted
  and folded into `scan_truncated` as documented.
- **`PostgresCampaignStore.record`.** The `ON CONFLICT (campaign_id, job_id) WHERE job_id <> ''`
  inference matches `bo_suggestions_job_idx` in `infra/sql/037_bo_suggestion_provenance.sql`; both
  writes are inside one `conn.transaction()`; the `DO NOTHING` → re-`SELECT` path returns the
  original id, and `InMemoryCampaignStore` reproduces the same idempotency key and the same
  newest-first ordering as `ORDER BY id DESC`.
