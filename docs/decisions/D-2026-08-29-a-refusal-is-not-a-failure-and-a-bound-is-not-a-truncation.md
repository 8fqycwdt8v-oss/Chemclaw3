# D-2026-08-29-a-refusal-is-not-a-failure-and-a-bound-is-not-a-truncation — the third pass over the unparseable-tool-call change

## Status

Accepted. The third pass over the same change:
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` (#282)
found the defect, `D-2026-08-29-a-discarded-call-is-not-a-lost-call` (#284) corrected its
announcement rule, and this corrects #284's own fixes plus three claims it made. Four fresh
reviewers over the merged commits, each running the code rather than reading it.

The announcement rule and the `Counter` arithmetic #284 shipped came back **sound** — 26 shapes
enumerated, including all sixteen combinations of N-broken against M-reissued. Everything below sat
beside it.

## What was wrong

**A gate refusal was reported to the chemist as a failure.** `_empty_answer_event` computed
`lost = ledger.tool_failures + ledger.tool_refusals` and rendered the sum as "N tool call(s)
failed". Measured on a turn with three plan-gate refusals and nothing broken:

```
… 3 tool call(s) failed and are reported above, which is the reason to start from. (session sess-1).
```

`_TurnLedger.tool_refusals`' own comment one screen above says a refusal is "the control working,
which must not be read as a failure";
`D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream` exists to end
exactly this and is cited nowhere in the change that reintroduced it; and `Chemclaw3_ui`'s trace
header, three lines above the sentence, reads `0 failures / 3 held` from the same events. The
WARNING beside it logged `tool_failures` alone, so one turn said `0 failed` in the log and
`3 tool call(s) failed` to the person.

**The remedy replaced the advice rather than adding to it.** Any failure at all deleted the
narrower-question line — on the du-03 shape that function's own docstring is about (29 calls, no
answer), one incidental failure removed the only useful next step.

**The parse error was bounded from the wrong end, and unescaped.** `_bounded_text` truncates the
head, and LangChain folds the entire argument document into the exception message
(`Function {name} arguments:\n\n{document}\n\nare not valid JSON. Received JSONDecodeError
{reason}`) — so the surviving text was a second copy of `BrokenCall.arguments`, printed beside it
in the same sentence, and the reason at the tail was cut. Measured against the 200-char budget: the
reason survived a 102-char document and was gone from 122 upward. Not repr'ing it was inherited
from `_bounded_text`, whose reason for not quoting is about the *name* (`_metric_label` compares it
against the bound tools); extended to this field it means an embedded newline forges a log line,
since `log_json` defaults to **false**:

```
WARNING chemclaw.agent.model_calls model.invalid_tool_calls: … find_notes
2026-08-29 ERROR chemclaw.audit: actor=admin action=approve_plan result=granted: '{"x": }'
```

**Nothing capped how many unrunnable calls one reply could hold.**
`agent_max_parallel_tool_calls` bounds calls that *run*; `len(AIMessage.invalid_tool_calls)` had no
bound. Measured with every field at its own ceiling: 8 malformed calls cost a 7.2 kB correction,
**1000 cost 841 kB** and 2000 stream events — and `_retry_request` appends that `HumanMessage` from
the *innermost* middleware, below `context_compaction_middleware`, where the budget is already
computed and nothing reduces it. That is `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`
reached through the one message it could not see, in the function #284 edited for the per-field
dimension and left unbounded in the per-message one.

**A raising repair lost the announcement.** `_count_invalid` runs before the retry and
`_announce_unrun` after it, so a 429 or a context-length refusal on the second model call booked
the operator's record and told the chemist nothing — the asymmetry the whole change exists to
remove, through the one path the ordering did not cover.

## Decision

- **Failures and refusals are counted apart and lead to different next steps.** A fault is
  something to read; a refusal is something to approve, and the sentence says so. The counts are
  always stated; only the advice branches, so the du-03 shape keeps its remedy.
- **`_bounded_reason` bounds the parse error from the tail and escapes it.** The head is pure
  duplication of a field printed beside it, so the tail alone carries information —
  `agent/tool_result_size.py` states the general rule ("head and tail, never head alone… a
  head-truncated result reads as complete and silently drops the outcome") and here the head is
  worth nothing at all. `repr` because this string reaches an unescaped log line and a chemist's
  event, and `bounded_repr` already escapes `arguments` for the same reason.
- **`agent_max_reported_lost_calls` (20) bounds every sink** — the correction, the WARNING and the
  events — with the remainder **counted, never dropped**. A bound that says nothing is the
  truncation this module exists to end, one level up.
- **The retry is wrapped**, so a raising repair announces the first reply's losses on the way out.
  `Exception`, not `BaseException`: a cancelled turn is not a lost call.

## The claims corrected

1. *"published to Phoenix as a 1.0"* — **false**. `evals/phoenix.py` guards that yield on
   `outcome.error_code or outcome.transport_error`, and the measured turn answered cleanly, so no
   `failed_loudly` annotation is published at all. The harm was real and reached `evals/live.py`
   and `cli/live_probes.py`; it did not reach Phoenix. The ledger row is corrected in place, since
   the ledger is an index rather than the record.
2. *"on a field whose own docstring says every field is bounded"* — **misattributed**. That claim
   is in `invalid_tool_calls`' docstring, not `BrokenCall.error`'s.
3. *"The `empty_answer` sentence stops contradicting the failures above it"* — **overstated**. The
   narrower-question advice went; "after 0 tool call(s)" stayed. It is now one clause of a counts
   sentence that names what ran, what failed and what was refused.
4. The `RepairInvalidToolCalls` class docstring still described #282's reverted rule in the present
   tense, naming `_report_lost_calls` — a function #284 deleted, in the file #284 edited. Same dead
   name in `tests/test_langgraph_stream.py`, with "no id to give" beside it.

## What the tests could not see, and now can

Every one of these was a claim with no check behind it. `_empty_answer_event` had **no test at
all** — `grep "reason to start from" tests/` returned only the source line. The `Counter`
multiplicity rule `_announce_unrun`'s docstring argues for in a paragraph survived degradation to a
set difference. The parse error's path to the chemist survived deleting it from the sentence. The
sync hook's announcement, a repaired reply breaking a *different* tool, and the whole
signal→`ToolFailedEvent` hop — the sentence both earlier ADRs are titled after — were asserted
nowhere.

Each fix here ships with a test proven load-bearing by reverting the hunk it names, and the raising
repair is parametrized over both hooks because mutating one alone left the suite green.
