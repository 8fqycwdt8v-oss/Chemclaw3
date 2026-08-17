# Verdicts — science/bo + science/fingerprints — CORRECTNESS (repro lens)

In scope: the two findings marked **critical** and **high**. The third finding is *low* and was not
examined. All scripts written from scratch under `/tmp/v1_*.py`, `/tmp/v2_*.py`; none of the
reporter's `/tmp/audit/` scripts were run or read.

Working tree was clean for every file involved
(`git diff --stat HEAD -- src/chemclaw/science/bo src/chemclaw/connectors/bo tests/test_bo_doe.py`
printed nothing), so what I read is HEAD.

---

## A "full factorial" over mixed categorical + continuous factors is not the Cartesian product — factors come out perfectly aliased and the design is labelled exhaustive

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical

- **What I did**

  1. Wrote `/tmp/v1_full.py`: builds problems with `n_cat` two-level categoricals plus `n_cont`
     continuous factors, calls `factorial_design`, and compares the set of distinct rows against
     `itertools.product` over the levels/bounds. `uv run python /tmp/v1_full.py` printed:

     ```
     cats=1 conts=1 {}: rows=4  distinct=2 corner-distinct=2 expected_combos=4  MISSING=2
     cats=2 conts=1 {}: rows=8  distinct=4 corner-distinct=4 expected_combos=8  MISSING=4
     cats=2 conts=2 {}: rows=16 distinct=4 corner-distinct=4 expected_combos=16 MISSING=12
     cats=3 conts=1 {}: rows=16 distinct=8 corner-distinct=8 expected_combos=16 MISSING=8
     cats=3 conts=2 {}: rows=32 distinct=8 corner-distinct=8 expected_combos=32 MISSING=24
     cats=3 conts=1 {'n_center': 2}: rows=32 distinct=8 corner-distinct=4 expected_combos=16 MISSING=12
     3-level cat x 1 cont: rows=6 distinct=6 exp=6 MISSING=0
     ```

     Rows 1–5 are the reporter's numbers exactly. Row 6 differs only because I separated corner rows
     from the midpoint rows `n_center` adds; the defect is the same. The last line is my own control
     and confirms the `gcd(N,C)==1` escape (3-level categorical × 1 continuous is complete).

  2. `/tmp/v1_alias.py` on solvent × base × T(20–120). It printed:

     ```
     Full factorial over 3 factor(s), 8 run(s) in total. Exhaustive over the levels stated:
     every combination of them is run. T is continuous and held at the two ends …
     resolution: None
        {'solvent': 'DMF', 'base': 'Cs2CO3', 'T': 120.0}   (x2)
        {'solvent': 'THF', 'base': 'Cs2CO3', 'T': 20.0}    (x2)
        {'solvent': 'DMF', 'base': 'K2CO3',  'T': 120.0}   (x2)
        {'solvent': 'THF', 'base': 'K2CO3',  'T': 20.0}    (x2)
     distinct: 4
     solvent -> T levels: {'DMF': {120.0}, 'THF': {20.0}}
     ```

     T is a *function* of solvent — perfect aliasing — under a sentence that says every combination
     is run. Same script on the repo's own fixture
     (`tests/test_bo_doe.py::test_a_continuous_factor_is_screened_at_its_two_bounds_and_said_to_be`)
     gives distinct rows `{(THF,20), (toluene,100)}` — 2 of the 4 combinations. `uv run pytest
     tests/test_bo_doe.py -q` → **22 passed**, so the suite is green on the aliased design, exactly
     as the finding states.

  3. Verified the quoted upstream mechanism against the installed source rather than the finding's
     excerpt: `inspect.getsource(bofire.strategies.fractional_factorial.FractionalFactorialStrategy._ask)`
     from `/home/user/Chemclaw3/.venv/lib/python3.11/site-packages/bofire/strategies/fractional_factorial.py`
     contains the quoted `pd.concat([design]*len(categorical_design)) | pd.concat([categorical_design]*len(design))`
     verbatim. Both halves are *tiled*, so row *i* is `(continuous[i % N], categorical[i % C])`;
     a cross product needs one side repeated element-wise (`np.repeat`) and only the other tiled.
     It also confirms the two escape hatches the finding names: all-continuous returns before the
     combine, all-categorical returns after `_get_categorical_design()`.

  4. Confirmed the defect survives the tool boundary: `/tmp/v1_tool.py` calls
     `connectors.bo.server.tools.generate_screening_design` on the same problem →
     `runs: 8 distinct: 4` with the same "Exhaustive over the levels stated" summary.

  5. Line numbers/symbols are current: `_full_design` at `engine.py:831`, `factorial_design` at
     `engine.py:870` (the finding says 928 — that is the `_full_design` call site inside it, not the
     `def`), `generate_screening_design` at `tools.py:672`, `ScreeningDesign.summary` at
     `problem.py:617` with the "Full factorial" head at ~628.

- **Why**

  Reproduces from scratch, on the engine and through the connector tool, with the reporter's own
  test fixtures inside the broken region. The cited code does what the finding says on the arguments
  it says, the upstream mechanism is verbatim-current, and the escape conditions (`gcd == 1`,
  all-categorical, all-continuous, `n_generators >= 1`) are exactly as described — I checked the
  3-level case myself and it is clean, which is what makes this an *intermittently* correct function
  rather than an obviously broken one.

  Critical is the right severity and I would add one thing the reporter did not: `resolution` is
  `None` on this path, so `ScreeningDesign` carries **no** field a caller could use to notice.
  `two_level_continuous` names the collapsed factor but says nothing about the pairing, and the
  duplicated rows are not marked as replicates (`n_repetitions` is 1). Every disclosure surface the
  design object has reports it as complete. The one signal available — 8 rows where 4 are distinct —
  requires the reader to deduplicate the runs themselves, and nothing in the summary suggests doing
  so.

---

## An excluded pairing in the run history makes the optimizer declare a finite space exhausted while feasible conditions remain unrun

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. `/tmp/v2_excl.py`, built from the real signatures (`ExcludeConstraint(parameters=[...],
     options=[[...],[...]])`, `Observation(params=..., value=...)` — the finding's prose shorthand
     is not the constructor, so this is my own construction). It printed:

     ```
     feasible space: 3
     distinct in history: 3
     space_exhausted(batch=1): True
     propose_candidates RAISED ValueError: this decision space holds 3 distinct condition(s) and
       all 3 have been run, so there is no fresh point left to propose. The screen is complete …
     without guard: [Candidate(params={'cat': 'Pd2dba3', 'solv': 'DMF'},
       predicted_value=32.33, predicted_sd=22.31, ...)]
     ```

     The bypass replaced `engine._require_fresh_points_exist` with a no-op for one call and restored
     it in a `finally` — no source file was mutated. BoFire answers the identical call with the
     feasible, never-run cell, so the guard is the only thing producing the refusal.

  2. Reachability through the chemist-facing tool, not just the engine: `/tmp/v2_tool.py` calls
     `connectors.bo.server.tools.suggest_next_experiment(problem, observations, count=1)` with the
     same three observations →

     ```
     TOOL RAISED ValueError: this decision space holds 3 distinct condition(s) and all 3 have been
       run, so there is no fresh point left to propose. The screen is complete …
     ```

     `_require_observed_params_match` (`tools.py:239`) delegates to `_require_params_match`, which
     compares parameter *names* only — an infeasible historical run passes untouched, as claimed.

  3. Read both sides of the comparison. `discrete_candidate_count` (`problem.py:1048`) enumerates
     `product(...)` and skips cells any `ExcludeConstraint.forbids`; `distinct_candidate_count`
     (`problem.py:1115`) is `len({params_key(o.params) for o in observations})` with no feasibility
     filter at all. They are compared directly in `_require_fresh_points_exist` (`engine.py:420`,
     `run >= space`) and in `space_exhausted` (`problem.py:1120`, `distinct + batch > space`). The
     mismatch is structural, not incidental.

  4. Blast radius beyond the inline tool: `space_exhausted` is read by `campaign.py:100` and
     `connectors/bo/workflows.py:138`, so the durable campaign loop stops on the same miscount.

  5. The progress sentence: `/tmp/v2_prog.py` with four runs (one excluded) over the 3-cell feasible
     space printed `n_distinct: 4  design_space: 3` — the "N out of M" clause in
     `progress.py:155` is then arithmetically impossible, as the finding says.

- **Why**

  Reproduces end-to-end on the path a chemist actually reaches, with BoFire itself as the control
  showing the dropped point is real and proposable. The trigger is not contrived: nothing in the
  stack rejects an observation the problem's own exclusion forbids, and running the pairing is the
  ordinary reason to write the exclusion in the first place.

  I keep the reporter's **high** rather than raising it, because the failure mode is a loud refusal
  and a false sentence, not a wrong number written into a record — a chemist who reads "the screen
  is complete" against a 2×2 they can count in their head will notice. It stays high rather than
  medium because the durable loop stops *silently* on the same miscount (`space_exhausted`) with no
  message at all, and because the error text is a confident factual claim about the chemistry
  ("all 3 have been run"), which is the class of statement this repo's summaries exist to make
  trustworthy.

  One correction to the finding's framing: the guard is not wrong to exist and its docstring's
  reasoning about the `KeyError` holds; what is wrong is only that the two counts range over
  different sets. The proposed fix (one shared feasible-key helper read by
  `_require_fresh_points_exist`, `space_exhausted` and `CampaignProgress.n_distinct`) is the right
  shape.
