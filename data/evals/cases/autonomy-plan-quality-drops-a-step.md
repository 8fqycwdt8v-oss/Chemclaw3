---
id: autonomy-plan-quality-drops-a-step
# A demonstration case: this plan is missing a required step, so its failure is the expected
# result rather than a regression — the same idiom `pharma-solvent-heavy` uses for the PMI gate.
expect_pass: false
metrics: [plan_quality]
output:
  transcript:
    - {type: plan, todos: ["[x] Compute the DFT energy"]}
    - {type: answer, text: "The energy is -154.75 Hartree.", unsupported_claims: [], review_required: false}
reference:
  expected_plan_steps:
    - Pull the ELN history for this transformation
    - Compute the DFT energy
    - Propose the next experiment
---
The gate firing (F9-T3). One of three required steps was planned: precision 1.0, recall 0.333,
F1 0.5 — below the `eval_plan_quality_min` floor of 0.8.

This case exists so the floor is demonstrably load-bearing. Without it, `plan_quality` could be
silently unable to fail and the suite would look green for the wrong reason — which is the failure
`expect_pass` was added to make visible.

Note what it does *not* demonstrate: a plan that names the right steps in a different order still
scores 1.0, because the metric is deliberately set-based. Two orderings of the same work are
usually both correct, and gating on one would gate on a preference.
