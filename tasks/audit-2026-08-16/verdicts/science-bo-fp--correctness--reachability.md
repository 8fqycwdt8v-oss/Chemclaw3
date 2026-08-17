# science/bo + science/fingerprints — CORRECTNESS · verifier lens: reachability & consequence

Scope: the two findings marked **critical**/**high**. The third (`A replicated or centre-padded
screen reports a run count…`, low) is out of scope and was not examined.

No source file was mutated. Scripts under `/tmp/audit2/`. Working tree at `577b88c` plus an
unrelated `M src/chemclaw/agent/plan_gate.py` from another agent, untouched by me.

---

## A "full factorial" over mixed categorical + continuous factors is not the Cartesian product — factors come out perfectly aliased and the design is labelled exhaustive

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical
- **What I did**:

  1. Read the installed BoFire (`0.4.1`,
     `.venv/lib/python3.11/site-packages/bofire/strategies/fractional_factorial.py`, `_ask`). The
     combining step quoted in the finding is verbatim what ships: both frames are `pd.concat`-**tiled**
     (`[design]*len(categorical_design)` and `[categorical_design]*len(design)`), never
     `np.repeat`-ed, so row *i* pairs `cont[i % N]` with `cat[i % C]`. The early returns above it are
     the reason all-continuous and all-categorical are unaffected.

  2. `/tmp/audit2/r1.py` — drove `factorial_design` over the finding's grid:

     ```
     cats=1 conts=1 {}: rows=4  distinct=2  expected=4  MISSING=2
     cats=2 conts=1 {}: rows=8  distinct=4  expected=8  MISSING=4
     cats=2 conts=2 {}: rows=16 distinct=4  expected=16 MISSING=12
     cats=3 conts=1 {}: rows=16 distinct=8  expected=16 MISSING=8
     cats=3 conts=2 {}: rows=32 distinct=8  expected=32 MISSING=24
     cats=3 conts=1 {'n_center': 2}: rows=32 distinct=8 expected=16 MISSING=8
     cats=0 conts=3 {}: rows=8  distinct=8  expected=8  MISSING=0
     cats=3 conts=0 {}: rows=8  distinct=8  expected=8  MISSING=0
     ```

     Every number matches the filed table, including the two correct control cases.

  3. **Reachability traced to the outermost entry point**, not to the private function.
     `/tmp/audit2/r9.py` calls the MCP tool `generate_screening_design` itself
     (`connectors/bo/server/tools.py`, listed under `tools:` in `connectors/bo/connector.yaml`)
     with a plain JSON problem — i.e. exactly what an agent emits from a chemist's request:

     ```
     Full factorial over 3 factor(s), 8 run(s) in total. Exhaustive over the levels stated: every
     combination of them is run. T is continuous and held at the two ends of the declared range …
        {'solvent': 'DMF', 'base': 'Cs2CO3', 'T': 20.0}
        {'solvent': 'DMF', 'base': 'Cs2CO3', 'T': 20.0}
        {'solvent': 'THF', 'base': 'Cs2CO3', 'T': 120.0}
        {'solvent': 'THF', 'base': 'Cs2CO3', 'T': 120.0}
        {'solvent': 'DMF', 'base': 'K2CO3', 'T': 20.0}
        {'solvent': 'DMF', 'base': 'K2CO3', 'T': 20.0}
        {'solvent': 'THF', 'base': 'K2CO3', 'T': 120.0}
        {'solvent': 'THF', 'base': 'K2CO3', 'T': 120.0}
     distinct: 4 of 8
     ```

     (The reporter's paste has the DMF/THF↔20/120 assignment flipped; immaterial — the aliasing is
     the same.)

  4. `/tmp/audit2/r3.py` reproduced the `_mixed_problem()` fixture result: `equiv` perfectly aliased
     to `solvent` (`THF [(20.0, 1.0), (120.0, 1.0)]` / `toluene [(20.0, 3.0), (120.0, 3.0)]`),
     4 distinct of 8 rows. `uv run pytest tests/test_bo_doe.py -q` → **`22 passed`**.

- **Why**:

  I attacked reachability and found nothing standing in the way. `n_generators=0` is the tool's
  *default*, mixed categorical+continuous is *explicitly documented as supported* by both the engine
  docstring ("A continuous factor is admitted and held at its two bounds", W2/D-092) and the tool
  docstring, and `_require_knobs_are_honoured` only constrains `n_center`/`n_repetitions` on
  all-categorical problems. The only refusal on this path is `problem.constraints`, which is
  orthogonal. So the trigger is not an edge case reached by calling a private helper — it is the
  default invocation of a published MCP tool on the most ordinary DoE shape there is
  (catalyst × base × temperature).

  I attacked the consequence and it is if anything understated. There is no partial mitigation: the
  `two_level_continuous` clause discloses that `T` was collapsed to two levels, which is true and
  irrelevant, while the sentence next to it — `"Exhaustive over the levels stated: every combination
  of them is run"` — is flatly false about the very rows it accompanies, and `resolution is None`
  suppresses every confounding warning the class was built to emit. That summary is a
  `computed_field`, so it is serialized into the model's context at answer time by design. What the
  chemist is shown is a plate labelled exhaustive in which solvent and temperature cannot be
  separated.

  **One thing the reporter missed that makes it worse.** The `n_center` case is not merely
  over-counted; the centre runs land on the wrong cells. `/tmp/audit2/r10.py`, one continuous factor
  and two two-level categoricals with `n_center=2`:

  ```
  centre rows (T=70) attached to: [('THF', 'K'), ('tol', 'K')]
  corner rows attached to:        [('THF', 'Cs', 20.0), ('tol', 'Cs', 120.0)]
  ```

  Base `Cs` receives **no centre run at all** and base `K` receives **nothing but** centre runs — so
  no categorical combination has both a corner and a midpoint, and the curvature detection centre
  points exist for is impossible from this design for either base. That also falsifies, on this
  path, the claim both docstrings make that "BoFire adds them per combination of the categorical
  factors": they were added per combination and then re-paired by the tiling.

  The proposed fix (build the continuous rows from a continuous-only `Domain`, cross with
  `itertools.product` over the categorical levels in Python) is the right shape and is what
  `_fractional_design` already proves works. The regression assertion must be on
  `{frozenset(run.items())}`, as filed — I re-verified that the existing count- and per-column-set
  assertions all pass on the diagonal.

---

## An excluded pairing in the run history makes the optimizer declare a finite space exhausted while feasible conditions remain unrun

- **Verdict**: CONFIRMED (with one scope correction, below — the severity is unchanged)
- **Severity I would assign**: high
- **What I did**:

  1. Read `discrete_candidate_count` (`problem.py:1050-1080`) — it enumerates cells and drops those
     any `ExcludeConstraint.forbids` — against `distinct_candidate_count` (`problem.py:1114`), which
     is `len({params_key(o.params) for o in observations})` with no feasibility filter at all. The
     two are compared directly in `_require_fresh_points_exist` (`engine.py:448-455`) and in
     `space_exhausted` (`problem.py:1127`). The mismatch is in the code, not inferred.

  2. `/tmp/audit2/r5.py`, the finding's 2×2-minus-one-pairing problem with the excluded run in
     history:

     ```
     feasible space: 3
     distinct in history: 3
     never run, feasible: {'cat':'Pd2dba3','solv':'DMF'}
     space_exhausted(batch=1): True
     propose_candidates RAISED ValueError: this decision space holds 3 distinct condition(s) and
       all 3 have been run, so there is no fresh point left to propose. The screen is complete — …
     ```

  3. **Guard bypassed** (`/tmp/audit2/r6.py`, calling `engine._fitted_strategy` + `strategy.ask(1)`
     directly): BoFire answers the same question correctly —

     ```
     Candidate(params={'cat': 'Pd2dba3', 'solv': 'DMF'}, predicted_value=32.33, predicted_sd=22.31)
     ```

     and in my history that unrun cell is the one the two best runs point at.

  4. **Reachability at the outermost entry point** (`/tmp/audit2/r7.py`): called the MCP tool
     `suggest_next_experiment` itself with plain JSON — problem, exclusion and the three
     observations. Through `OptimizationProblem.model_validate`, `require_names_do_not_clash`,
     `_require_observed_params_match`, `require_observations_cover_objectives`,
     `featurize_problem`, `require_descriptors_distinguish_categories`, all of which pass:

     ```
     TOOL RAISED ValueError: this decision space holds 3 distinct condition(s) and all 3 have been
       run, so there is no fresh point left to propose. The screen is complete — report the best
       runs rather than asking for another, or widen the space …
     ```

     Nothing between the wire and the guard filters an infeasible observation. I checked the
     candidates specifically: `_require_observed_params_match` delegates to `_require_params_match`,
     which compares parameter *names* only; BoFire's own `validate_experimental` checks that values
     are declared *levels*, not that the row satisfies a constraint — proven by step 3, where it
     accepted and fitted on the infeasible row without complaint.

  5. `/tmp/audit2/r8.py` reproduced the progress sentence verbatim:

     ```
     n_distinct: 4 design_space: 3
     Best yield so far: 60 over 7 evaluation(s) (4 distinct condition(s) out of the 3 the full grid
     holds). …
     ```

- **Why**:

  Mechanism, trigger and consequence all hold, and I reproduced the whole chain from the tool
  boundary rather than from a private helper. The refusal is not a crash the caller might catch: it
  is a `ValueError`, which `connectors.server` forwards verbatim by design, so the sentence
  *"The screen is complete"* is what reaches the model composing the chemist's answer — a confident,
  false claim about a screen that has a feasible, better-looking condition left in it. The
  `campaign_progress` sentence is arithmetically impossible on its face ("4 … out of the 3").

  The trigger is caller-shaped rather than input-shaped, which is why I hold this at **high** and not
  critical: it needs an all-categorical problem (any continuous parameter makes `space` `None` and
  the guard inert), an `ExcludeConstraint`, and a history that includes a run of the excluded
  pairing. But the reporter's argument for why that history is *normal* survives scrutiny — the
  exclusion is knowledge produced by the failed run, and the failed run is evidence a chemist hands
  over — and there is no guard anywhere that would make it impossible. Note also that the offered
  recovery ("widen the space … drop a constraint") only works by discarding a real chemistry
  constraint, which is the wrong repair for a bug in the accounting.

  **Scope correction — the "durable/inline campaign loop" half of the Consequence does not hold.**
  I traced both loops. `science/bo/campaign.py:95` seeds from `initial_candidates(...)` and extends
  only with `propose_candidates(...)`; `connectors/bo/workflows.py:115-150` seeds from the
  `propose_initial` activity and extends only from `propose_next`, with `carried` being its own
  continue-as-new carry-over. Neither loop ever admits caller-supplied history, and both seeding and
  proposal honour the exclusions (the reporter's own `/tmp/audit/p4.py` result, which I did not
  re-run). So `space_exhausted` cannot be tripped this way from a campaign; the reachable surfaces
  are the two chemist-facing inline tools, `suggest_next_experiment` and `campaign_progress`. That
  narrows the blast radius without touching the severity — the inline tool is, as
  `_require_fresh_points_exist`'s own docstring says, "the path a chemist actually reaches".

  Second, smaller correction: "A genuinely unrun, feasible condition is **silently** dropped" is not
  right — the failure is loud, and the finding quotes the loud message two lines earlier. The harm is
  a confident wrong statement, not a silent omission.

  The proposed fix is the right one, and the shared-enumeration form is what makes it durable:
  `discrete_candidate_count` already walks the feasible cells, so `_require_fresh_points_exist`,
  `space_exhausted` and `CampaignProgress.n_distinct` should count history against *that* set. Worth
  adding to the fix: the regression test has to assert the guard does **not** fire, not just that the
  counts agree — a test on the two integers would pass on a version that filters history but still
  compares against the unfiltered product.
