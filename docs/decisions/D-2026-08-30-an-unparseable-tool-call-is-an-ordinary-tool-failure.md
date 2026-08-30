# D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure — promote it, and let the tool chain refuse it

## Status

Accepted · 2026-08-30

## Supersedes

This replaces the *mechanism* of four ADRs on one subject, and keeps their findings:
`D-2026-08-27-a-refusal-is-not-a-crash` (the `RepairInvalidToolCalls` half only — its
`RecordModelCalls` half stands),
`D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure`,
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`,
`D-2026-08-29-a-discarded-call-is-not-a-lost-call` and
`D-2026-08-29-a-refusal-is-not-a-failure-and-a-bound-is-not-a-truncation`. Each is still correct about what it measured; the design all four patched is replaced.

## Context — the defect, unchanged

LangChain puts a tool call whose arguments do not parse on `AIMessage.invalid_tool_calls`.
`ToolNode` iterates `tool_calls` and nothing else, so the call was invisible to every control this
system has: no `tool_failed`, no `tool_result`, no audit row, no span, and
`chemclaw_invalid_tool_calls_total` never moved. With prose beside it the turn proceeded as though
no tool had been needed — `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed`, exactly.

**What reaches that field is malformed JSON, not truncation.** `parse_partial_json` completes any
prefix of a valid object, so a cut stream repairs itself: `'{"smiles": "CC'` arrives on `tool_calls`
with `args={"smiles": "CC"}`, and even `'{oops'` arrives as `args={}` and the tool *runs*. Only a
document that is not a prefix — garbage, a bare value, an unbalanced close — reaches it.

## The decision

**Move the call, and let the tool chain refuse it.**

`PromoteInvalidToolCalls` (a `wrap_model_call`) appends each `invalid_tool_call` to `tool_calls`,
keeping the model's own id and carrying the raw document under `_UNPARSED_ARGUMENTS`, and clears
`invalid_tool_calls`. `refuse_unparsed_arguments` (a `wrap_tool_call`) sits **innermost** of the
governance chain and raises `UnparsedArguments` before the tool body runs.

That is the whole mechanism. Everything else follows because it already worked:

| | the design this replaces | promotion |
| --- | --- | --- |
| audit row | none — argued as principled | the ordinary row, `error` outcome |
| span | none | the ordinary span |
| `call_id` on the failure | dropped; forced a `graph_stream` guard | the model's own id |
| authz / dry-run / repeat guard | never ran | all run |
| a valid call beside a broken one | discarded with the reply | runs |
| the model's correction | a hidden model call outside every graph bound | one graph iteration |
| reporting to the chemist | `_announce_unrun` + `_reportable` + 5 prose constants + a setting | `announce_tool_failures`, already there |

**~20 lines replace ~180.** The 180 were the ones that kept being wrong.

### Why the sentinel rather than `{}`

Measured against the live registry: **11 of 54** in-process tools take no required argument
(`list_watches`, `find_knowledge_gaps`, `recall_preferences`, …). A promotion carrying an empty
argument dict satisfies their schema, so the tool would *execute* on a request the model never
successfully expressed. The sentinel makes the promotion refusable before the body, and carries the
document to the one party that can act on it — the model. The count is asserted against the live
registry in `tests/test_invalid_tool_calls.py`, so a tool added next year that widens this trap
turns the suite red rather than the docstring stale.

### Why `UnparsedArguments` is not in `refusal_reason`'s table

That table names the five *gates* — decisions this system made on purpose. A document that will not
parse is a fault, so `reason` is `None` and every surface renders it as one: `Chemclaw3_ui` in the
failure red, `evals/live` in `tools_failed`, `TurnCost` in `tool_failures`. The correct
classification is reached by adding nothing.

## Why replace rather than patch again

Three rounds of adversarial review found defects in the retry machinery and **each round's fix
introduced the next round's defect**. The cause was structural rather than careless: *a retry taken
outside the graph is outside every bound the graph has*, so each bound had to be rebuilt by hand —
a "never a loop" ceiling because the loop cap could not see the extra call, a reporting ceiling
because nothing bounded the corrective `HumanMessage` (appended *below* compaction, where nothing
reduces it), an announcement rule because no `tool_failed` could be raised for a call the chain
never saw, a `graph_stream` guard because that announcement had no id, and an invariant about
streamed prose because `astream(stream_mode=["messages"])` emits per *model call* — so the
discarded attempt's tokens reached the chemist while the recorded message held only the second
attempt.

**The trade, stated so it is not rediscovered.** The old design corrected the model without
spending a graph iteration. Promotion spends one. That iteration is precisely what makes the
correction visible to the loop cap, the spend cap, the transcript and the audit trail — each of
which the old design had to fake.

## What is deleted

`RepairInvalidToolCalls`, `_announce_unrun`, `_reportable`, `_retry_request`, `_report_repair`,
`_carrying_prose`, `_because`, `valid_tool_calls`, `_prose_of`, the five prose constants, and
`agent_max_reported_lost_calls` with its `.env.example` row. The ceiling that setting bounded no
longer exists; `agent_max_parallel_tool_calls` bounds how many calls a reply may hold and
`agent/tool_result_size.py` bounds each result.

`api/graph_stream.py`'s `if signal.call_id:` guard is **reverted**. It was added for a producer
that no longer exists, and it was wrong in the direction that matters: a refusal is deliberately
`status="success"` (`_refusal_message` says why — `is_error` invites the retry the wording exists
to prevent), so `failed_calls` is the only thing suppressing it. With the guard, a refusal whose
call carried an empty id produced `tool_failed` **and** `tool_result`, and the refusal sentence
joined the grounding corpus `score_answer` reads. The two-different-calls case the guard reached
for cannot arise: a signal's id *is* its call's id.

## Four defects that outlived the redesign, each now with a failing-without-the-fix test

1. **`_bounded_reason` cut a `repr` escape in half.** It sliced the *quoted* form from the tail, so
   a cut landing between the two characters of `\n` left the letter `n` in the reason a chemist
   reads — a corruption that reads as content. Measured at a 13-character budget on a document
   ending `"\nreason here"`: `…nreason here'`, an unbalanced fragment. It now slices the text and
   quotes the slice, by binary search because `repr` expands by up to four characters per input
   character.
2. **The tool *name* reached the WARNING unescaped** where the parse error beside it was escaped —
   same `%s`, same line, `log_json=false` by default — so a newline in a model-authored name forged
   an `actor=admin … result=granted` audit line. Escaped at the sink, not at the source, so
   `_metric_label`'s comparison against the bound tools is untouched.
3. **`_empty_answer_event` said "N tool call(s) ran"** where `called_tools` counts calls
   *announced*. A dry run that held three calls read "3 tool call(s) ran, 3 refused by a gate" —
   six intents where there were three, and three bodies said to have executed that a gate stopped.
   It says `attempted` now, with the failures and refusals as subsets. Its docstring's claim that
   the remedy "follows from what dominates" described a comparison the code does not make; the code
   is failure-precedence, deliberately, and now says so.
4. **`evals/live.py` split only `plan_gate` off `tools_failed`.** `RefusalReason` names five gates;
   the other four — `dry_run`, `undeclared_write`, `repeat`, `authz` — were scored as tools falling
   over, and each set `failed_loudly`, the harness's headline finding. Any reason now means the
   control worked. This is `D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream`
   one layer further out, for the third time.

## Three claims corrected

1. *"`Chemclaw3_ui`'s trace header … read `0 failures / 3 held`"* — **a string nobody could see.**
   `TracePanel.troubleLabel` renders `` `${held} refusal${held === 1 ? '' : 's'}` `` and omits the
   failure clause entirely when `problems === 0`, so the header reads `3 refusals`. It was quoted
   from a file that had not been read.
2. *"the 841 kB corrective `HumanMessage`"* — **not reproducible.** Measured at the commit it
   describes: 623 kB. It is removed with the machinery it described rather than corrected, because
   the ceiling it justified is gone.
3. *"`_TurnLedger.tool_refusals`' own comment"* — **misattributed.** `_TurnLedger` carries no
   comment on that field; the sentence quoted ("the control working, which must not be read as a
   failure") is on `TurnCost.tool_refusals`.

The ledger row for `D-2026-08-29-a-discarded-call-is-not-a-lost-call` also carries a correction that
a merge resolver silently dropped — it keyed rows by id and let `main`'s copy win every collision,
so a row edited on the branch was replaced by the version it was editing. That correction is
restored: the `failed_loudly` annotation is guarded on `error_code or transport_error` in
`evals/phoenix.py`, and the measured turn answered cleanly, so it never reached Phoenix; the harm
was real and reached `evals/live.py` and `cli/live_probes.py`.

**The rule that leaves:** a resolver that reconciles two append-only registers by key must treat a
*changed* row as a conflict, not as a duplicate. Ours reported a clean merge over a discarded edit.

## Measured on the live stack

`make live-storm --families CF` against the mock model: **11/11 checks pass**, including the one
that had been permanently red — `an unparseable argument document is reported, not swallowed`, now
`tools_failed=['find_notes']` where it read `tools_failed=[]`. One turn of the same behaviour driven
by hand, with the records the storm cannot see:

```
tool_call    find_notes {"__unparsed_arguments__": "'{\"text\": }'"}
tool_failed  find_notes "UnparsedArguments: The arguments for this call were not valid JSON, …"
error        "…: 1 tool call(s) attempted, 1 failed. … The failure(s) reported above are the
              place to start"
AUDIT        tool=find_notes outcome=error
turn_costs   outcome=empty_answer tool_calls=1 tool_failures=1 tool_refusals=0
/metrics     chemclaw_invalid_tool_calls_total{tool="find_notes"} 2
```

No `tool_result`: the refusal pairs with the failure by the model's own id and is suppressed. The
same behaviour before this change produced `error/empty_answer` and *"after 0 tool call(s) … a
narrower or more specific question is the useful next step"*, with no `tool_call` and no
`tool_failed` at all. `tasks/live-test/storm-cf-2026-08-30.md` is the record.

**One consequence, stated rather than discovered.** The `tool_call` event carries the sentinel key,
so a surface renders the arguments as `{"__unparsed_arguments__": "'{\"text\": }'"}`. That is the
honest record — it is what the model sent — and the `tool_failed` beneath it is the readable half.
Hiding it would make the event read as a call with no arguments, which is a different and worse
untruth.

## The rule to carry

**When three consecutive fixes each introduce the next defect, the defect is the design.** The
signal is not the count of bugs — it is that every fix had to *rebuild* something the surrounding
system already provides. A mechanism that needs its own ceiling, its own reporting, its own
identity and its own bound is a mechanism standing outside the machinery that would have given it
all four.
