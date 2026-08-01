---
id: autonomy-plan-quality
metrics: [plan_quality]
output:
  transcript:
    - {type: token, text: "Pulling the ELN history first. "}
    - {type: plan, todos: ["[ ] Pull the ELN history for this transformation", "[ ] Compute the DFT energy", "[ ] Propose the next experiment"]}
    - {type: tool_call, tool: search_notes, arguments: '{"query": "Suzuki coupling yield"}'}
    - {type: tool_result, tool: search_notes, preview: "3 notes"}
    - {type: plan, todos: ["[x] Pull the ELN history for this transformation", "[x] Compute the DFT energy", "[x] Propose the next experiment"]}
    - {type: answer, text: "Try 1.2 mol% Pd at 90 °C.", unsupported_claims: [], review_required: false}
reference:
  expected_plan_steps:
    - Pull the ELN history for this transformation
    - Compute the DFT energy
    - Propose the next experiment
---
Plan quality (F9-T3): the turn ended having named exactly the three steps this request needs, so
precision and recall are both 1.0 and F1 is 1.0.

**The transcript is the shape `run_turn` really emits**, not an invented one — checkbox-prefixed
`todos` strings, a `PlanEvent` only when the plan changes (so the last one is the final state), and
an `AnswerEvent` last. It is validated against the closed `Event` union when the metric loads it,
so a `type:` the front door cannot produce fails the case instead of scoring as a missing signal.

Scripted deliberately: the model's replies are pinned, so what is gated here is the harness
carrying a plan into the event stream intact. Whether the *model* would choose these three steps is
the AG-13 question, which needs a live endpoint and is not this case's claim.
