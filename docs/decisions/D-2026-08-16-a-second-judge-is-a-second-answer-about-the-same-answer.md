# D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer — `RubricMiddleware` is declined

**Status:** accepted · **Date:** 2026-08-16 · Closes the `RubricMiddleware` row in
`docs/planning/BACKLOG.md` and phase 5's second half. Supersedes the blocker
`D-2026-08-14-the-coupling-is-the-cost-not-the-line-count` recorded for it, which turned out not to
be the reason to decline.

## Context

The gap is real and is not in dispute. `agent/verifier.py::score_answer` scores an answer's
faithfulness to the evidence the turn retrieved and sets `review_required`; `api/runner_answer.py`
puts that on the wire and a surface shows a review affordance. **Nothing routes a flagged answer
back for another pass.** The verifier's own docstring says a low-confidence answer is "marked, not
blocked".

`deepagents.RubricMiddleware` is upstream's shape for exactly that: an LLM grader with a bounded
revision loop. It is present in the pinned distribution, so adopting it needs no bump. The earlier
audit recorded one blocker — that its revisions are counted by the runaway cap, so the two bounds
must be chosen together.

## What was measured

**That blocker is not the problem.** Its hooks are `before_agent` / `after_agent`, not
`after_model`, so a revision re-enters the graph and each extra model call passes `before_model` —
where `agent/loop_cap.py::enforce_loop_cap` counts it. Being counted is *correct*: a revision is a
model call, and a cap it could skip would be a bypass. It is a sizing question, not a defect. Four
other things are.

**1. It cannot reuse the judge this repository already has.** Its constructor is
`(*, model, system_prompt=None, tools=None, max_iterations=3, on_evaluation=None)` — it builds an
LLM grader of its own, and there is no seam to hand it an existing evaluator. So the tree would hold
two judges of the same answer, reading different things: `score_answer` sees the turn's tool results,
threaded in from `api/runner.py` precisely so a citation is checked against what this turn retrieved
rather than against "note ids that exist"; the grader sees the message thread. `verifier.py` records
that having one implementation "is what stopped the two paths disagreeing about whether the same
answer was flagged" — the panel that was the second caller is gone, and this would put one back.

**2. Every non-satisfied termination leaves the answer unmodified.** Read in the installed source
rather than taken from the docstring: `_handle_grader_exception` returns `_rubric_status:
"grader_error"` and **no message mutation**, and `_finalize_evaluation` rewrites the result to
`max_iterations_reached` and likewise mutates nothing. So a grader outage means every answer ships
ungraded, with a log line and an event nothing in this tree reads. That is the shape this repository
has been burned by three times — the harness-mode flag whose consequence was that harness mode never
worked, `ModelCallLimitMiddleware`'s `exit_behavior="end"`, and D-025's compaction policy surviving
as three settings with no reader.

**3. It is inert until somebody authors a rubric.** The middleware "activates only when a caller
passes a `rubric` on invocation state"; with none, both hooks return unchanged. A capability that
ships off is what `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` deleted 1,442 lines
of agent code over. Adding a second one the day after is not a decision this tree gets to make
twice.

**4. It couples two bounds that are independent today.** `harness_max_loop_iterations` would have to
carry the rubric's revisions as well as the tool loop, so raising the revision count silently
shortens the tool loop. Correct behaviour, but a coupling with no compensating gain given 1–3.

## Decision

**Declined.** The gap stays open and stays recorded, because the honest reason not to close it is
that nothing has yet shown a flagged answer gets *better* on revision — and adopting a second
grader, whose failures are silent, to act on a signal a first grader produced, is more machinery
than the evidence supports.

What would change this is a measurement rather than an argument: take the answers `score_answer`
already flags, revise them, and score the revisions. If revision helps, the loop is worth building
**first-party** on `score_answer`, so there is one judge reading the turn's own tool results, and
its failure mode is chosen here rather than inherited.

## What trying to run the measurement found first

The measurement this ADR asks for **did not complete** — the environment's model credential hit a
hard usage limit 92 seconds into the probe run (135 of 230 probes attempted, 6 answered). No
revision was attempted, so there is still **no evidence either way** on whether revision helps.

But getting far enough to try surfaced a defect that changes the question, and it is in the judge
this ADR was defending rather than in the middleware it declined:

**With `verifier_enabled` on, the LLM judge never ran, and every non-empty answer was flagged.**
`verify_answer` bound the model with `with_structured_output(VerificationResult)` — the default
`method="function_calling"`. `convert_to_openai_tool` drops any field carrying a default out of
`required`, so `claims` (`default_factory=list`) and `verified_by` disappeared and the provider was
asked to enforce `confidence` alone, types included. Measured 8 of 8 against a live model: the
judge either omitted `confidence` or returned the whole verdict as a JSON *string* inside `claims`,
both failing `VerificationResult` validation inside `verify_answer`'s `try`, both degrading to the
citation gate — after which `score_answer`'s third rule appends *"verified by the citation gate
only; the judge did not run"* and sets `review_required`. So the signal a revision loop would have
consumed carried **no information at all** in the shipped configuration.

That is D-2026-08-16's own point 2 — a silent, non-mutating failure whose only evidence is a log
line — present in the first-party judge. Declining `RubricMiddleware` for having that shape while
shipping it here would have been the wrong lesson to draw, so it is recorded here rather than left
in a measurement note.

Fixed by binding with `method="json_schema"` (13 of 13 with no other change). `tests/test_verifier.py`
pins both halves: that the loose rendering exists, and that the caller does not use it — either
alone passes while the feature is broken.

**A second finding, at n=6 and worth carrying:** of the six answers that did complete, three were
flagged for three *different* reasons, and two of those at high judge confidence (0.833 and 0.923)
— a shape-gate hit on an ungrounded `wavelength: 270 nm`, and `promised but not called:
screen_hazards`. **The second cannot be fixed by revising prose at all**: the remedy is to call the
tool. A one-shot text reviser could only clear it by deleting the sentence that promised the tool —
exactly the degenerate fix the agreed criterion was designed to catch, visible before a single
revision was run. Whoever resumes this should treat the three flag reasons as three questions, not
one.

## The measurement, run — and it settles the decline

Run on `claude-haiku-4-5` throughout (agent, judge, reviser), 51 probes stratified across eight
probe files, full live stack, model routing asserted rather than assumed. **39 of 51 answers
flagged; all 39 revised.**

| | n | of flagged |
|---|---:|---:|
| cleared the flag | 10 | 25.6% |
| cleared **and kept their substance** | 2 | 5.1% |
| **cleared with no edit at all** (null control, per roll) | **2.0** | **5.1%** |

**The null control is what decides it, and it was not in the brief.** Re-scoring the same 39
answers *unchanged*, three more times, clears 2.0 per roll. Revision's non-degenerate clear rate is
identical. **The measured benefit over doing nothing is zero** — at $0.0149 and a 3.4 s median per
flagged turn, added to the answer hot path after the user has already waited.

Eight of the ten clears are deletions, and what they delete matters: a `screen_hazards` call
offered on a diazo compound, replaced with "consult your standard hazard-assessment process"; a
five-step protocol the user explicitly asked for; a mechanistic explanation replaced with "the
record does not contain details on why this works". One answer cleared by *fabricating* citations —
`[[evidence-0]]`, the labels of the harness's own evidence envelopes.

**`promised but not called` is the cleanest result and the least model-dependent: 8 of 8 were
"fixed" by deleting the promise.** None called the tool, which a text reviser structurally cannot.
Any loop scored on flag clearance learns exactly that move. If that class is worth fixing, the
intervention is re-entering the agent loop with the tool call forced — a different and much larger
feature than a rewrite pass.

Two failure modes a first-party loop would inherit, both measured: a reviser **must** be shown the
evidence, so `turn_evidence`'s synthetic `tool-output-N` ids stop being uncitable — the reviser
cited them, and went on citing renamed labels after being told not to, so any loop must exclude
them structurally rather than by prompt. And under an ungrounded-number flag the reviser's instinct
was to *change* the number (254 nm → 280 nm), not remove it.

**What the measurement does not support**, stated because the sample is small: n=39 flagged, and
2 vs 2 cannot exclude a small positive effect. Haiku is weaker than a deployment model and cuts
both ways — it likely inflates the flag rate *and* revises worse. The null control is measured on
the same judge, so the comparison that carries the conclusion is internally controlled; what it
cannot say is what a stronger judge and reviser would do. That is the one experiment that could
overturn this, and it needs a bigger model on both legs.

## A prior question the measurement raised about the judge itself

`verified_by == "judge"` on 51 of 51, so the `json_schema` fix holds. Two things about the signal
are worth more than the revision question:

- **The judge enumerated claims on only 6 of 51 turns.** 26 of the 39 flagged answers carry an
  *empty* `unsupported` list, so a reviser is told "(none listed)" and given no target.
- **At the threshold, the verdict is not reproducible.** The null control measures this directly:
  5.1% of flagged answers clear on a re-roll with no edit. It is a *margin* effect rather than
  general noise — two independent probes here, one trivial and one a realistic multi-claim fully
  grounded answer, both scored 1.00 six times out of six. So the judge is stable where the answer
  is unambiguous and unstable exactly where the threshold lives.

Both are recorded in `BACKLOG.md`. They matter more than the revision loop, because they are about
whether `review_required` means anything — and that question is prior to what one would do with it.

## Consequences

- `agent/verifier.py` remains the only judge, and now actually runs: the `method="json_schema"`
  binding is the one code change this ADR carries, and it is a prerequisite for the measurement
  rather than a part of the declined design.
- The `BACKLOG.md` row is rewritten to say what was learned, rather than deleted: the gap survives,
  the proposed fix does not.
- If anyone reaches for `RubricMiddleware` again, points 1 and 2 are the two things to check first
  — whether upstream has grown an evaluator seam, and whether a failed grading still returns the
  ungraded answer.
