---
id: autonomy-plan-execute-utility
metrics: [plan_execute_utility]
output:
  higher_is_better: true
  tasks:
    - {task_id: suzuki-yield, baseline: 62.0, augmented: 78.5}
    - {task_id: amide-coupling-yield, baseline: 71.0, augmented: 74.2}
    - {task_id: solubility-screen, baseline: 55.0, augmented: 55.0}
    - {task_id: ester-hydrolysis, baseline: 80.0, augmented: 76.1}
---
Plan-vs-single-shot A/B (F9-T3): four tasks, two helped, one unchanged, one hurt — a helped share
of 0.5 with a net delta of +15.8 percentage points of yield.

`evals/ab.py::compare_tool_utility` already implemented this comparison and was simply never
registered as a metric, so it ran under no `make eval`, gated nothing, and put no number in
`baseline.json`. Registering it is most of what this row needed.

**The scored value is the helped share, not `net_delta`.** A metric's value is a single float, and
`net_delta` is unbounded and denominated in each task's own units — one task on a percent-yield
scale would swamp four on a log-solubility scale, and the drift band would mean nothing. The share
is bounded in [0, 1] and comparable across case sets; the signed deltas stay in the provenance.

Ungated on purpose. "How often does planning help" is a progress number, and a threshold on it
would gate the suite on a research question rather than on a defect.
