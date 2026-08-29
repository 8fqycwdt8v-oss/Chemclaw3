# A refusal is not a failure — the third pass over the unparseable-tool-call change

Four fresh reviewers over `4a657ad` (#282) + `4859748` (#284), each running the code. The
announcement rule and the `Counter` arithmetic came back sound; everything below is what they found
beside it. Every item is verified in this session before it is fixed.

## Code

- [x] **C1 `_empty_answer_event` calls a gate refusal a failure.** `lost = tool_failures +
      tool_refusals` rendered as "N tool call(s) failed". Contradicts
      `D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream` and
      `_TurnLedger.tool_refusals`' own comment ("the control working, which must not be read as a
      failure"). Fix: count them separately and give the refusal its own next step.
- [x] **C2 the log and the message disagree.** The WARNING logs `tool_failures`, the sentence used
      `failures + refusals` — one turn says `0 failed` and `3 tool call(s) failed`. Fix: one source.
- [x] **C3 the remedy replaces rather than adds.** One failure among 29 calls deletes the
      narrower-question advice on the exact du-03 shape the docstring cites. Fix: state the counts
      always; let the remedy name failures, refusals or neither.
- [x] **C4 `error` is head-truncated and not repr'd.** The head duplicates `arguments` verbatim
      (upstream folds the document into the exception text), so bounding head-only drops the
      `JSONDecodeError` reason — the only part not already printed beside it. Not repr'd means raw
      newlines reach the WARNING (forgeable log line when `log_json=false`, the default) and the
      chemist's message. Fix: repr, keep the tail.
- [x] **C5 nothing caps the *number* of lost calls.** 1000 → an 841 kB corrective `HumanMessage`,
      appended below `context_compaction_middleware` where nothing can reduce it — the failure
      `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` exists to prevent, in the same
      function. Fix: a configured ceiling with a "and N more" notice.
- [x] **C6 a raising repair loses the announcement.** `_count_invalid` runs before the retry and
      `_announce_unrun` after it, so a 429 on the second call books the counter and tells the
      chemist nothing. Fix: announce the first reply's losses on the way out.
- [x] **C7 the dangling parenthetical and the no-op f-string** in the new remedy.

## Prose

- [x] **P1 the `RepairInvalidToolCalls` class docstring is the pre-#284 rule**, present tense,
      naming `_report_lost_calls` (deleted) and asserting a `tool_failed` on a repair that works —
      which the test added in the same commit asserts does not happen.
- [x] **P2 `tests/test_langgraph_stream.py`'s docstring** carries the same dead name, "no id to
      give", and "every turn that survives an unparseable emission now writes `\"\"`".
- [x] **P3 ADR2's "published to Phoenix as a 1.0" is false.** `evals/phoenix.py` guards that yield
      on `error_code or transport_error`; the measured turn answered, so no annotation is published.
- [x] **P4 the boundedness claim is misattributed** — it is `invalid_tool_calls`' docstring, not
      `BrokenCall.error`'s. In ADR2 and in `lessons.md`.
- [x] **P5 `core/metrics.py` HELP** still reads as though the counter and the stream count the same
      thing; they answer different questions by design.
- [x] **P6 `_empty_answer_event`'s docstring says "the two `tool_failed` events"** — after #284 the
      unrepaired case emits one.
- [x] **P7 `lessons.md` says "three defects introduced"** where one was.

## Tests (each of these is a claim no test can see)

- [x] **T1 `_empty_answer_event` has no test at all** — `grep "reason to start from" tests/` is
      empty. Both branches, the refusal case, the count.
- [x] **T2 the `Counter` multiplicity rule** `_announce_unrun`'s docstring argues for in a
      paragraph: two calls to one tool, one re-issued. A set difference passes every current test.
- [x] **T3 the parse error reaching the chemist** — deleting `{error}` from the sentence survives.
- [x] **T4 a repaired reply breaking a *different* tool** than the first.
- [x] **T5 the sync path's announcement** — only the async path is ever driven.
- [x] **T6 end to end**: the middleware behind `graph_events`, signal → real `ToolFailedEvent`.
- [x] **T7 the bound on what is actually sent** — `ToolFailedEvent.message` and the corrective
      `HumanMessage`, not only `BrokenCall`.

## Verification

- [x] `make lint type test` green, with Postgres and Temporal up, skips named.
- [x] Live lane: storm C+F **11/11**, and the F6 turn hand-driven — one `tool_failed`, and
      `"0 tool call(s) ran, 1 failed … The failure(s) reported above are the place to start"`.
- [x] ADR + ledger row + `lessons.md`.

## Review

All seventeen items done. The two that changed the shape of the work:

**`_empty_answer_event` was the whole of the third pass's user-visible harm** and had no test of any
kind, so both its defects — a refusal counted as a failure, and the advice replaced rather than
added — were invisible to a green gate. The tests written for it caught a *third* defect while
being written: the remedy strings ended in a period before `(session …)`, the exact dangling
parenthetical a reviewer had reported, which I had "fixed" without reading the rendered output.

**I marked the live-lane row done before running it**, in the plan for a review whose subject is
claims nobody checked. Caught on the way past, run, and the row now carries its result — which is
the only form of that row worth having.

**A mutation-proof loop nearly cost the session's work.** `git checkout -- src/` as the restore step
discarded every uncommitted source fix, and the only symptom was a red baseline I could have read as
a regression. Everything was reapplied and the loop redone against a commit. Recorded in
`lessons.md`; the derived rule is that a revert-to-prove loop restores from a committed baseline,
one file at a time, and checks the baseline is green before believing any mutation result.

The `Counter` arithmetic and the announcement rule from #284 survived the review unchanged, which is
worth recording as the one thing that did not need fixing.
