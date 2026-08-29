# D-2026-08-29-a-discarded-call-is-not-a-lost-call — announce after the repair, over what the turn will not run

## Status

Accepted. Supersedes the announcement rule and three claims of
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` (merged the
same day, PR #282). That ADR's finding stands: an unparseable tool call never enters the tool chain,
so `announce_tool_failures` cannot announce it, and the chemist was told nothing. Its *rule* for
what to announce was wrong, and it made three claims that are false.

Found by a four-way adversarial review of the merged commit, each reviewer running the code rather
than reading it.

## What the review measured

**The rule "announce every call a discarded reply will never run" is wrong when the repair works.**
A discarded call is not a lost call if the model re-issues it — it runs. Measured on a compiled
`create_agent` graph, a first reply of one broken `predict_pka` beside a valid `find_notes`,
repaired, both then running, the turn answering:

```
tools_called : ['predict_pka', 'find_notes']
tools_failed : ['predict_pka', 'find_notes']
answered     : True 'The pKa is 4.76.'
failed_loudly: True
```

Three readers were wrong at once about a turn in which nothing failed:

- `evals/live.py` takes every `tool_failed` whose `reason != "plan_gate"` into `tools_failed`, and
  `failed_loudly = bool(tools_failed or error_code)`. It scored a clean turn as a loud failure, and
  `evals/phoenix.py` publishes that as an eval score of 1.0. `find_notes` was never invoked at all.
- `_TurnLedger.note_event` books each into `turn_costs.tool_failures`, a column
  `core/turn_cost.py` documents as "what the turn actually did". A turn now records failures for
  calls that succeeded, disagreeing with both the audit trail and `chemclaw_tool_calls_total`,
  which correctly show one successful call each.
- `Chemclaw3_ui`'s `TracePanel.tsx` renders a `reason`-less failure as `tone='danger'` with a
  `failed` badge. The chemist read two red rows above a good answer.

**And the mitigation the ADR named does not exist.** It said "`reason` and the event's own declared
meaning are what distinguish the two". `RefusalReason` names the five *gates*; a lost call carries
`reason=None`, which `core/turn_signals.py` documents as "an ordinary failure — every failure that
was ever emitted before this field existed". On the wire a repaired-and-succeeded call and a broken
tool are identical apart from prose, which is exactly the substring-matching that
`D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream` deleted.

**Separately, `BrokenCall.error` was unbounded**, on a field whose own docstring says "every field
here is the model's own output and is bounded on the way out". It is not merely unbounded but
reliably large: `parse_tool_call` folds the entire raw argument document into the exception message
and `langchain_openai` stores it verbatim, so the document arrives twice — once truncated in
`arguments`, once whole in `error`. Measured on a 100 kB document with the budget at 200 chars:

| | chars |
| --- | ---: |
| `BrokenCall.arguments` | 201 |
| `BrokenCall.error` | 100,260 |
| `ToolFailedEvent.message` | 100,587 |
| corrective `HumanMessage` | 100,861 |
| WARNING log line | 100,376 |

The corrective message is the worst of the three sinks: `_retry_request` appends it from the
*innermost* middleware, below `context_compaction_middleware`, so the context budget has already
been computed and nothing reduces it — the failure `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`
exists to prevent. It reads empty on the streamed shape, which is why it went unnoticed; the log
line and the corrective message were already unbounded before PR #282, which added the third sink.

## Decision

**Ask the question once, after the repair, against the reply the turn continues with.**
`_announce_unrun` announces a call if the repaired reply either cannot run it or never asks for it.
Counted by name rather than differenced as a set, because under-reporting is the failure this
middleware exists to end: a model that emitted two calls to one tool and re-issued one has lost the
other.

**The operator's records stay per attempt.** `_count_invalid` keeps the counter and the WARNING
where they were, because an operator is asking a different question — how often the model emits
malformed output, and what it cost in model calls. So `chemclaw_invalid_tool_calls_total` still
reads 2 on a turn that shows the chemist one lost call, and **that is the correct outcome**, not a
discrepancy. `test_the_counter_counts_attempts_while_the_stream_counts_losses` pins the pair.

**`BrokenCall.error` is bounded** by the budget the field beside it already used.

**The `empty_answer` sentence stops contradicting the failures above it.** `_empty_answer_event`
counts announced calls off `ToolCallTrace`, which a lost call never enters, so the F6 turn read
"after 0 tool call(s) … a narrower or more specific question is the useful next step" directly
beneath two `tool_failed` events naming the tool the model asked for twice. It now names the
failures and drops the narrower-question advice when there were any.

## The three claims corrected

1. *"An operator's count and a chemist's stream cannot disagree about how many calls were lost."*
   **False, and now false in the other direction too** — they disagree by design, and each is right
   about its own question. The counter never counted `discarded` at all, and it takes the clamped
   `_metric_label` where the signal takes the model's own name.
2. *"A chemist is told which tool did not run … instead of being told their question was too
   broad."* **"In addition to", not "instead of"** — `_empty_answer_event` was untouched. Fixed
   here rather than only corrected.
3. *"`reason` and the event's own declared meaning are what distinguish the two."* **`reason`
   distinguishes nothing here.** Only the declared meaning did, and no consumer reads a docstring.
   Moot now: a repaired call is not announced at all.

Two smaller ones, both in prose: "there is no id to give" (the entries carry ids; `BrokenCall` drops
them — what is absent is a `tool_call` event to match), and "three records for three readers" over a
docstring naming two.

## Consequences

- A repaired turn is silent to the chemist and fully recorded for the operator, which is what each
  of them needs.
- A parseable call discarded with a broken reply is still announced when the model does not re-issue
  it — the silent drop `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` names. It is
  the case the "announce the second attempt only" simplification would have lost, and it has its
  own test.
- The storm's F6 check is unaffected: it needs at least one `tool_failed`, and the unrepaired case
  still announces one (one, not two — one unmet intent is one row).
- The truncation hazard `D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure` §3 left open
  is now recorded in `docs/planning/BACKLOG.md`. Closing F6 against the unclosable payload retired
  the check that used to carry it, and it was left asserted by nothing.
