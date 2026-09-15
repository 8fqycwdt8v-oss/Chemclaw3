# D-2026-09-15-a-flagged-answer-that-goes-out-flagged-is-a-verdict-nobody-acted-on — a flagged answer is sent back for another pass, bounded by agent-initiated rounds

**Status:** accepted · **Date:** 2026-09-15 · Closes the gap
`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` conceded and left open. That
ADR's *decision* — `RubricMiddleware` is declined — stands unchanged and is not superseded: what
ships here is a first-party loop over the judge this repository already has.

## Context

`agent/verifier.py::score_answer` scores an answer against the evidence the turn actually
retrieved and sets `review_required`. `api/runner_answer.py` puts that on the wire, and a surface
shows a review affordance. D-2026-08-16 states the rest plainly:

> The gap is real and is not in dispute. … **Nothing routes a flagged answer back for another
> pass.** The verifier's own docstring says a low-confidence answer is "marked, not blocked".

That ADR declined upstream's `deepagents.RubricMiddleware` on four counts, and every one of them is
about *that implementation* rather than about the capability — chiefly that it builds an LLM grader
of its own with no seam to hand it `score_answer`, so the tree would hold two judges of the same
answer reading different things; and that `_handle_grader_exception` and `_finalize_evaluation` both
return **without mutating the message**, so a grader outage ships every answer ungraded with a log
line nothing reads.

The prompt to revisit it was the Paperclip evaluation (`tasks/paperclip-ideas-2026-09-15.md`), whose
execution policy carries one detail neither this tree nor `RubricMiddleware` has: a round counter
that counts only **agent-initiated** rounds, where a human-initiated request resets it.

## Decision

**Loop in `api/runner.py`, between `build_answer_event` and the transcript write.**

Not a middleware, for a reason that is structural rather than stylistic: the verdict is produced
*outside* the graph. `build_answer_event` runs after the stream is drained, and `score_answer` needs
`tool_trace.outputs` — the turn's own tool results, threaded in from the runner precisely so a
citation is checked against what this turn retrieved rather than against "note ids that exist".
There is no middleware position that can see a finalized answer; a repo-wide search finds no
first-party `after_model`, `before_agent` or `after_agent` hook at all, and D-2026-08-15 records why
`after_model` is unsafe for anything load-bearing.

`_revise_answer` is `_resume_on_job_results`'s shape — a second `graph_events` over the same graph
and `thread_id`, `run_complete` cleared for its duration, the same `carry`. That last one matters:
a revision is counted by `loop_cap` and `spend_cap` rather than buying a fresh allowance of either,
which is the conclusion D-2026-08-16 itself reached about revisions ("being counted is *correct*: a
revision is a model call, and a cap it could skip would be a bypass").

### Only agent-initiated rounds are counted, and the bound is a per-turn local

`rounds` is a local in `run_turn`. A chemist's own follow-up is a new turn and therefore a fresh
allowance; the model cannot buy itself one. `loop_cap` and `spend_cap` count uniformly and cannot
express that distinction — which is exactly why the bound is not a state channel.

### `answer_parts` is cleared, and that is the one place this is not the resume

`ledger.answer_text` joins the accumulated parts. A resume *continues* an answer, so appending is
right there. A revision *replaces* one, so without clearing, both the transcript and the
`AnswerEvent` would carry the flagged prose with the corrected prose stapled to its end — worse than
either alone. Probed by removing the line: two tests fail, so it is load-bearing rather than
decorative.

**What this costs, stated because it is real:** the client has already been streamed the first
attempt's tokens and cannot un-see them. `AnswerEvent.text` is authoritative and carries only the
final pass, which is what the event contract already says it is — but a surface that appends tokens
rather than replacing its buffer with the answer will show both. Suppressing the first stream would
mean either buffering the whole answer (losing streaming for every turn, to fix a rare one) or a new
SSE event, which is a coordinated change across `Chemclaw3_ui` and `Chemclaw3_mock`. Neither is
worth it for a feature that ships off.

### Exhaustion is counted and is never silent

This is the property D-2026-08-16 found `RubricMiddleware` lacking, so it is the one arm that had to
be right. When the rounds run out the answer **still goes out and still carries
`review_required`** — exactly what it carried before this loop existed. A deployment that exhausts
is no worse off than one with the loop off, and `chemclaw_answer_review_exhausted_total` plus a
WARNING make the difference visible rather than inferred.
`test_a_revision_that_does_not_help_still_answers_and_stays_flagged` is that arm.

`ChemclawAnswerRevisionsNotHelping` alerts on the **ratio**, not the count: ten exhaustions out of a
thousand revisions is a working loop, ten out of twelve is a broken one — a deployment paying twice
per flagged turn for an answer it would have shipped anyway.

### The revision names the claims

`_revision_message` quotes the unsupported claims and asks for each to be dropped or corrected. A
prompt saying "try again" measures nothing and licenses rewording instead of regrounding. The claims
are the model's own prose quoted back at it, so they arrive `frame_untrusted`-wrapped — prose that
reaches a model inside an instruction is prose that can instruct, the discipline
`_job_results_message` follows one function over.

### It ships off

`answer_review_max_rounds = 0`. It is only reachable behind `verifier_enabled` or
`answer_shape_gate_enabled`, which are themselves off, and a revision doubles a flagged turn's model
spend — a real cost a deployment should choose. `test_the_loop_is_off_by_default` asserts the
shipped configuration drives the graph exactly once.

## What this does not do

- **It does not block an answer.** The verdict still never withholds a turn's answer; it buys at
  most N more attempts at grounding it.
- **It does not reuse a second judge.** There is one judge, `score_answer`, and the loop reads its
  verdict — which is the whole reason `RubricMiddleware` was declined and this was not.
- **It does not help when the evidence is absent.** If the turn retrieved nothing that supports the
  claim, the model can only drop or hedge it; the runbook says so and names the metric to check.
