# D-2026-08-01-a-scripted-transcript-gates-the-harness-not-the-judgment — A scripted transcript gates the harness, not the judgment

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** F9-T3 · **Extends:** D-009 (the
eval/metric layer) · **Does not close:** AG-13 (agent-behaviour evaluation on a live endpoint)

## Context

`implementation-tickets.md` specifies three autonomy measures — plan quality, a plan-vs-single-shot
A/B, and a runaway/abort rate — and notes all three are computable on a scripted transcript. None
was registered as a metric, so a prompt edit, a skill change or a middleware reorder could regress
behaviour with `make eval` green and nothing entering `baseline.json` for the drift check to watch.

**The row's framing ("zero evaluation of agent behaviour") is overstated, and the correction
matters.** `tests/test_harness_execution.py` drives real MAF machinery and already pins the loop
cap. What was missing is not *all* checking of behaviour but its absence from the **eval layer** —
the versioned case-set, the gate, and the drift baseline. Saying "zero" would have justified
building something larger than the gap.

Two of the three measures turned out to already exist in pieces. `evals/ab.py::compare_tool_utility`
*is* the plan-vs-single-shot A/B, fully implemented and simply never decorated with `@metric`, so it
ran under no command and gated nothing. And `precision_recall_f1` already defines "did it name the
right things" for retrieval.

## Decision

**Three metrics in `evals/autonomy.py`: `plan_quality`, `runaway_rate`, `plan_execute_utility`** —
registered through the existing `@metric` seam, scored over cases in the committed case-set, and
included in `baseline.json`.

**These gate the harness, not the model, and the ADR says so because the names do not.** A case here
carries a *scripted* transcript: the model's replies are pinned, so nothing about the model's
judgment is under test. What is tested is the machinery around it — that a plan is emitted, that its
steps survive into the event stream intact, that work is closed before an answer goes out, that the
A/B arithmetic holds and honours its direction. **AG-13 stays open**, and this is not a partial
payment against it: judging judgment needs the live endpoint AG-13 is deferred on. `retrieval_recall`
once carried a name that promised more than it scored, and that correction was expensive to
discover and cheap to write down.

**The transcript is parsed by the closed `Event` union, not read as loose dicts.** This is the
decision that makes the cases trustworthy rather than decorative. A case naming a `type:` the front
door cannot emit would otherwise contain no `PlanEvent`, score as "the signal was absent", and
report a healthy number from a case measuring nothing. Parsed, it fails at load.

**`runaway_rate` reads the cap's residue, because the cap emits no event.** `AgentLoopMiddleware`
stops at `harness_max_loop_iterations` and returns normally; `run_turn` then yields an ordinary
`AnswerEvent`. A capped turn is externally identical to a finished one except for what it leaves
behind — an answer sent while todos are still open. So a turn counts as a runaway when it ends in an
`ErrorEvent` coded `turn_timeout`/`budget_exhausted`, **or** when it answered with unchecked steps in
its final plan. `tests/test_harness_execution.py` asserts exactly that residue from inside the
process (`assert not items[0].is_complete`); this is the same observation made from the transcript,
which is all an eval case has.

**A rate needs more than one turn**, so `runaway_rate` reads `output.transcripts` (plural). Scoring
one turn would produce an indicator in {0, 1} wearing the word "rate", and the aggregate in
`baseline.json` would be the only place the name was true.

**`plan_quality` is deliberately blind to order.** It reuses `precision_recall_f1`, which is
set-based, and for a plan that is the right call rather than an inherited limitation: two orderings
of the same steps are usually both correct — pull the ELN history before or after running the
calculation — and penalising one would gate on a preference. What is genuinely wrong is naming a
step that should not be there or dropping one that should, which precision and recall already say.
There is a test asserting the order-blindness so a later reader does not "fix" it.

**The checkbox is stripped.** `PlanEvent.todos` carries display strings (`"[x] title"`) because the
surfaces must not have to infer completion state. Comparing those against a reference of bare titles
intersects to nothing, so a perfect plan would score 0.0 and the gate would fire on every healthy
turn. The alternative — writing checkboxes into the references — is worse: the prefix flips as work
completes, so the reference would have to predict how far the turn got.

**`plan_execute_utility` scores the helped *share*, not `net_delta`.** A metric's value is one
float, and `net_delta` is unbounded and denominated in each task's own units — one task on a
percent-yield scale would swamp four on a log-solubility scale and the drift band would mean
nothing. The share is bounded in [0, 1] and comparable across case sets; the signed deltas stay in
the provenance. It is ungated (`passed=None`): "how often does planning help" is a progress number,
and a threshold would gate the suite on a research question rather than on a defect.

**A turn that never planned is a `MetricError`, not a score of zero.** Absent evidence and bad
evidence must not share a number: 0.0 says "it planned badly", and a harness that stopped emitting
plans altogether would send a reviewer hunting for a quality regression in the prompt.

## Why not the alternatives

**Drive `run_turn` inside the metric**, the way `evals/retrieval.py` runs a live retriever. A metric
is documented as a pure function of an `EvalCase`, and the live path needed a memo, a corpus
signature and a `_require_scoreable_retrieval()` guard to stay honest. `run_turn` additionally
mutates context vars, writes audit and metrics, and books budget — none of which belongs in a
scoring pass. So the cases are static, and a **test** drives the real runner and asserts the events
it produces are what the cases contain. That test is what makes the static cases trustworthy; without
it they would be a hand-written fiction that reports healthy numbers forever.

**Add a `runaway` event to the front door.** It would be the honest signal, and it is a three-part
change — a new `Event` member, a runner branch, a UI branch — for a metrics row. The residue is
observable today and the metric documents why it looks for it.

**Widen `_EXHAUSTION_CODES` to every error code.** A storage outage is not the agent looping.
Conflating them makes the rate a measure of infrastructure health and destroys the one thing it is
for.

**Gate `plan_quality` at 1.0.** An extra defensible step is not a regression; a missing required one
is. The floor is `eval_plan_quality_min` (0.8), and a demonstration case pins it firing so the gate
is not silently unable to fail — the failure mode `expect_pass` exists to expose.

## Consequences

- Three autonomy numbers now enter `baseline.json`, so `check_eval_drift` watches them and
  `make eval-strict` fails on a regression. The baseline is re-stamped `autonomy-2026-08-01`.
- `tests/test_evals.py`'s chemistry scope is now a **positive** list. It filtered as "everything not
  `retrieval-*`", which made a test about chemistry a catch-all every new case family had to
  remember to exclude itself from or break its exact-equality assertion.
- **AG-13 is untouched**, and `DEFERRED.md` keeps its row: these metrics cannot evaluate the model's
  choices, only the harness that carries them.
- **Not closed: the cap is still silent.** `runaway_rate` infers it. A deployment that wants to
  alert on runaways in production still has nothing to alert on, which is a front-door change rather
  than an eval one.
- **Not closed: the A/B has no real task set.** `plan_execute_utility` scores the numbers a case
  gives it, and the shipped case is illustrative. Producing genuine baseline-vs-augmented pairs
  means running the same tasks twice against a live model — AG-13 again.
