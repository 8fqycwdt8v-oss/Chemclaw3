---
id: autonomy-runaway-rate
metrics: [runaway_rate]
output:
  transcripts:
    - - {type: plan, todos: ["[ ] Compute the DFT energy"]}
      - {type: job_started, job_id: qm-1, kind: qm}
      - {type: plan, todos: ["[ ] Compute the DFT energy", "[ ] Await QM job qm-1"]}
      - {type: answer, text: "I've started the DFT run, job qm-1.", unsupported_claims: [], review_required: false}
    - - {type: tool_call, tool: predict_solubility, arguments: '{"smiles": "CCO"}'}
      - {type: tool_result, tool: predict_solubility, preview: "-0.125 log10(mol/L)"}
      - {type: answer, text: "About -0.13 log S.", unsupported_claims: [], review_required: false}
    - - {type: plan, todos: ["[x] Screen the reagents for hazards"]}
      - {type: answer, text: "No flagged reagents.", unsupported_claims: [], review_required: false}
---
Runaway rate (F9-T3): three turns, none of which ran away — one deferred its work to a durable job,
one closed the step it planned, one never needed a plan. So the rate is 0/3, at the
`eval_runaway_max` gate of 0.0.

**A rate needs more than one turn**, which is why this case carries `transcripts` rather than the
single `transcript` the plan-quality cases use. Scoring one turn would yield a coin flip wearing the
word "rate".

**The first turn is the one that pins the fix.** It does exactly the right thing — launches a
durable job and says so — and it answers with unchecked todos behind it, because a turn that
correctly defers work leaves exactly that residue. An earlier `runaway_rate` inferred the loop cap
from precisely that residue ("answered with steps still open"), so it scored this turn as a runaway
and failed the 0.0 gate on correct behaviour. The transcript cannot separate the two by looking
harder: `PlanEvent.todos` carries only the rendered display strings.

**So the metric reads the cap instead of guessing at it.** The loop no longer stops silently —
`chemclaw.agent.loop_cap.enforce_loop_cap` counts each model call against
`harness_max_loop_iterations` in a declared state field, and the runner emits an `ErrorEvent` coded
`loop_cap_reached`, the third member of the exhaustion family beside `turn_timeout` and
`budget_exhausted`. A capped turn now states its own outcome, which is something a transcript can
carry; `tests/test_langgraph_stream.py` drives a real compiled graph into the cap and asserts both
records, so the thing this gate depends on cannot go quiet again unnoticed.
