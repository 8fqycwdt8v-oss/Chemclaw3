---
id: autonomy-runaway-cap-fires
# A demonstration case: one of these turns was cut off by a guard, so the rate is above the 0.0
# limit and its failure is the expected result rather than a regression — the same idiom
# `pharma-solvent-heavy` and `autonomy-plan-quality-drops-a-step` use.
expect_pass: false
metrics: [runaway_rate]
output:
  transcripts:
    - - {type: plan, todos: ["[ ] Screen the ligand set"]}
      - {type: tool_call, tool: find_notes, arguments: '{"text": "ligand screen"}'}
      - {type: tool_result, tool: find_notes, preview: "2 notes"}
      - {type: answer, text: "Two prior screens; XPhos won both.", unsupported_claims: [], review_required: false}
    - - {type: plan, todos: ["[ ] Compute the conformer ensemble"]}
      - {type: token, text: "Let me try that again. "}
      - {type: error, message: "reached its 25-iteration limit", code: loop_cap_reached}
      - {type: answer, text: "I could not finish this.", unsupported_claims: [], review_required: true}
---
**The demonstration `runaway_rate` did not have.** `autonomy-runaway-rate` is the passing case:
two turns, neither cut off, rate 0.0, gate satisfied. Nothing in `data/evals/cases/` made this
metric report a failure, so from the day it was written to the day this file was added the gate had
never been observed to fire — and a version of `runaway_rate` that stopped reading
`_EXHAUSTION_CODES` and returned 0.0 unconditionally would have passed `make eval-strict` and left
`baseline.json` untouched. That is the hole `EvalReport.gates_no_demonstration_can_fire` closes, and
this case is what closes it for this metric.

The second transcript is the shape the runner really emits when
`chemclaw.agent.loop_cap.enforce_loop_cap` stops a turn: an `ErrorEvent` carrying
`loop_cap_reached`, followed by whatever partial answer the turn had. That pairing is the point —
the capped turn *does* answer, which is exactly why the metric reads the explicit code instead of
inferring a cap from residue (see `runaway_rate`'s own docstring for the defect that caused).

One of two turns was cut off, so the rate is 0.5 against an `eval_runaway_max` of 0.0. The value is
a property of the transcripts rather than of the threshold: raise the limit and this case stops
failing, which `EvalReport.inert_demonstrations` then reports as lost coverage rather than as a
green build.
