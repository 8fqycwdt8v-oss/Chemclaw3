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

## Consequences

- `agent/verifier.py` is unchanged and remains the only judge.
- The `BACKLOG.md` row is rewritten to say what was learned, rather than deleted: the gap survives,
  the proposed fix does not.
- If anyone reaches for `RubricMiddleware` again, points 1 and 2 are the two things to check first
  — whether upstream has grown an evaluator seam, and whether a failed grading still returns the
  ungraded answer.
