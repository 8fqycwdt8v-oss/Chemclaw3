# Make the agentic backend lean to read

Scope: readability only. No behaviour change anywhere — every gate, event, refusal, metric and
audit row must be byte-identical in effect. The suite is the proof, not the argument.

## Why these three and not "cut the docstrings"

Measured before planning, because the obvious move is the wrong one. `agent/` holds **422
docstrings, 5,400 lines, median 9 lines, mean 12.8** — a healthy median with a long tail: **42
docstrings over 30 lines carry 1,770 of those lines**. So the prose is not uniformly bloated and a
mass cut would destroy the measurements this tree keeps deliberately. The reading cost is
concentrated in three structural places instead.

## 1 — `api/runner.py::run_turn` is one 483-line async generator

- [ ] `_TurnLedger` dataclass for the state that crosses stage boundaries (`answered`,
      `run_complete`, `answer_parts`, `started_jobs`, `tool_exchanges`, usage).
- [ ] `_turn_ambient(...)` sync `@contextmanager` stamping and resetting all five contextvars —
      makes "nothing in the teardown may await" structural rather than a comment.
- [ ] `_stream_into(...)` async generator shared by the main run and the mid-turn resume (the
      answer-part loop is currently written twice).
- [ ] `_resume_on_job_results(...)` for the mid-turn resume block.
- [ ] `_loop_cap_event(...)` / `_empty_answer_event(...)` returning `ErrorEvent | None`.
- [ ] `_book_turn_spend(...)` for the metrics + cost-ledger tail (sync, no await).
- [ ] `run_turn` left as orchestration a reader can hold in their head.

**Hazards, each of which the current code documents and must keep:** the teardown clause must
still catch `CancelledError` as well as `GeneratorExit` (D-130 — production disconnects arrive as
the former); the rollback predicate stays `run_complete`, never `answered`; `finally` must not
`await`; `consume_turn_approval` stays out of `finally`; `empty_answer` must still `return` rather
than fall through.

## 2 — Relocate the long-tail prose — **attempted on the worst case, and it yields nothing**

The plan assumed these docstrings were narrative that could move to a README behind a pointer, and
estimated 1,000–1,400 lines. **That estimate was wrong and the attempt is what showed it.**

Tried on the single biggest, `agent/checkpointer.py`'s 93-line module docstring, which looked like
the best possible case: it explicitly says its measurements are run by
`tests/test_checkpointer_schema.py`, and each of its four measured bullets does correspond to a
test whose *name* states the same finding. Real duplication, provably.

- First attempt — bullets collapsed into a prose pointer: **93 → 88 lines**, and materially worse
  to read. A dense paragraph carrying four test names is not leaner than the list it replaced.
- Second attempt — keep the bullets, append the test name to each, drop the restated numbers:
  **93 → 93 lines.** Zero.

**So the compression is not available.** These are measurement records attached to the code they
constrain, not narrative wrapped around it. Three things block the move independently: merged ADRs
are never edited, so the natural destination is unavailable; a README breaks the locality that
makes a measurement useful, since it is read when standing at the code; and a pointer to prose
elsewhere is the stale-pointer defect the previous PR existed to fix.

- [x] Measured on the worst case rather than assumed across all 26.
- [x] The checkpointer edit is **kept** — same length, but every measured claim now names the
      executable test that proves it, so the prose can no longer drift from the behaviour. That is
      a staleness fix, and it is honestly not a leanness one.
- [ ] The remaining 25 are **not** touched. There is no yield to have.

## 3 — Cut the import fan-out — **measured, and not done, because it works against the goal**

Static closure of `api.runner`: **156 modules, 529 internal edges.** Every edge was tried.

- **447 of the 529 save nothing at all.** The graph is densely reconnected, so a cut usually just
  routes around itself — `kg.note` alone is imported by **22** modules inside the closure.
- The best single cut is `durable.connector_job -> durable.memory_jobs` (−10), and it is the
  riskiest place in the tree to touch: it sits inside `workflow.unsafe.imports_passed_through()`,
  where an import change is a Temporal determinism/replay question.
- Greedily stacking the eight best cuts reaches **156 → 98 (37%)**.

**The 37% is real and is still the wrong trade.** Each of the eight is an import moved from a
module header into a function body, and the eight are `langgraph_agent`, `graph_tools`,
`research_tools`, `memory_tools`, `report_workflow`, `note_index`, `template_job`, `memory_jobs` —
every one of which a live front door imports on its *first turn* anyway. So the saving is startup
latency, not steady state, and it is bought by hiding the dependency structure inside function
bodies. That makes the tree harder to read, which is the thing this task exists to fix.

- [x] Measured exhaustively rather than argued.
- [ ] **Not implemented.** Reported to the requester with the numbers; theirs to overrule if the
      startup-time win is wanted for its own sake.

The fan-out does point at one genuine layering question — `agent/durable_tools.py` imports workflow
*implementations* in order to launch them, where D-002 says durability lives only in Temporal. That
is an architectural change needing its own ADR, not a readability edit.

## Verification

- [ ] `make lint type test` on a **full clone** with Docker up, so nothing skips for want of
      Postgres/Temporal. Report what skipped, if anything.
- [ ] PR, merge on green CI.

## Review

**One of the three was worth doing, and the other two are worth having measured.**

`run_turn` was the whole reading cost: **483 lines → 194, of which 90 are code**. Nothing was
deleted — every hazard comment moved onto the function that now owns it, which is the actual gain.
The D-130 cancellation rule now sits on the rollback that depends on it; the
`run_complete`-not-`answered` distinction is stated once, on the field; the "nothing here may
`await`" rule stopped being a comment and became the type of the thing, because `_turn_ambient` and
`_book_turn_spend` are synchronous and cannot acquire an `await`. One duplicated loop
(`_stream_into`, written twice — once for the model run, once for the resume) became one.

The other two were both **estimates I made before measuring, and both were wrong in the same
direction**: I sized them by eye from a line count and proposed them as wins. Measured, the docstring
compression yields *zero* lines once readability is held constant, and the import work yields 37%
of a number that only describes process startup — bought by hiding eight dependencies inside
function bodies, which makes the tree harder to read. Neither is a defensible trade for a task whose
whole purpose is readability.

**Lesson for `tasks/lessons.md`:** a line count is not a measure of reading cost. `agent/` is 58%
prose and that prose is mostly measurements; the file that was genuinely hard to read was the one
with a 483-line function in it. Size the work by structure, not by ratio — and measure the
candidate before pitching it, not after it is approved.
