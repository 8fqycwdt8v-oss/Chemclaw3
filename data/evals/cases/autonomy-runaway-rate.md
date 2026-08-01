---
id: autonomy-runaway-rate
metrics: [runaway_rate]
output:
  transcripts:
    - - {type: plan, todos: ["[ ] Await QM job qm-1"]}
      - {type: plan, todos: ["[x] Await QM job qm-1"]}
      - {type: answer, text: "Done: -154.75 Hartree.", unsupported_claims: [], review_required: false}
    - - {type: tool_call, tool: predict_solubility, arguments: '{"smiles": "CCO"}'}
      - {type: tool_result, tool: predict_solubility, preview: "-0.125 log10(mol/L)"}
      - {type: answer, text: "About -0.13 log S.", unsupported_claims: [], review_required: false}
    - - {type: plan, todos: ["[x] Screen the reagents for hazards"]}
      - {type: answer, text: "No flagged reagents.", unsupported_claims: [], review_required: false}
---
Runaway rate (F9-T3): three turns, none of which ran away — two closed every step they planned and
one never needed a plan. So the rate is 0/3, at the `eval_runaway_max` gate of 0.0.

**A rate needs more than one turn**, which is why this case carries `transcripts` rather than the
single `transcript` the plan-quality cases use. Scoring one turn would yield a coin flip wearing the
word "rate".

**Why the second clause of the metric is the interesting one.** The iteration cap emits no event:
`AgentLoopMiddleware` stops at `harness_max_loop_iterations` and returns normally, so a capped turn
looks exactly like a finished one except for its residue — an answer sent while todos are still
open. `tests/test_harness_execution.py` asserts that residue from inside the process
(`assert not items[0].is_complete`); this metric makes the same observation from the transcript,
which is all an eval case has. A metric that waited for a `runaway` event would report 0.0 forever
and read as proof the cap never fires.
