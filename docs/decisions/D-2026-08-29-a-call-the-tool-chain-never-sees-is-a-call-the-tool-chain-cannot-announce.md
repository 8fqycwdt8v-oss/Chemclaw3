# D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce — an unparseable tool call is announced to the chemist, not only to the operator

## Status

Accepted. Closes finding **F6** of `tasks/live-test/full-stack-e2e-2026-08-28.md`, which was
handed over open with a named next step and one measurement that turns out to have been wrong.

## Context

The 2026-08-28 campaign corrected the storm's adversarial probe so that it finally reaches the code
path it names: `'{"text": "unterminated'` is repaired by LangChain's `parse_partial_json` and lands
on `tool_calls` as an ordinary valid call, so the probe was asserting an outcome the system is
documented never to produce. Replaced with `'{"text": }'` — JSON-shaped and unclosable — the call
really does reach `AIMessage.invalid_tool_calls`.

The check stayed red, and the handover reported two symptoms:

1. `chemclaw_invalid_tool_calls_total` "is declared and carries **no samples**".
2. No `tool_failed` reaches the stream, so the call is a silent no-op.

It named the next step as entering LangChain's streaming tool-call assembly — this system's most
defect-prone seam by its own history (STREAM-1, LOAD-1, the `stream_events` v3 revert) — and
deliberately stopped there.

## What the measurement says

**The first symptom is not real, and the streaming assembly is not where the defect is.** Driven
against the live stack (`make live-infra`, `make live-up` on the mock model, `make live-storm
--families F`), the counter moves:

```
chemclaw_invalid_tool_calls_total{tool="find_notes"} 2
```

Two per turn, which is correct: the first attempt and the repair's second attempt are separately
unparseable and separately booked. The chain from the provider up is sound at every step, and each
was measured rather than reasoned about — `ChatOpenAI` streaming the mock's chat-completions frames
aggregates to an `AIMessageChunk` carrying `invalid_tool_calls`; `create_agent`'s
`_execute_model_async` passes it through `message_chunk_to_message` and `_handle_model_output`
unchanged; `RepairInvalidToolCalls.awrap_model_call` receives it as `ModelResponse.result[0]` and
`invalid_tool_calls()` reads it.

**The second symptom is real, and its cause is one layer away from where it was being looked for.**
`tool_failed` is put on a turn's event stream by `agent/tool_authz.announce_tool_failures`, a
`@wrap_tool_call` middleware. A call whose arguments never parsed never enters the tool chain,
because `ToolNode` iterates `tool_calls` and the call is on `invalid_tool_calls`. So the one
failure class that cannot reach the announcer is the one class `RepairInvalidToolCalls` exists for.

The contrast is exact, on two mock behaviours that differ only in whether the argument document
parses. Same tool, same session shape, same turn outcome:

```
'{"query": "benzene"}'  ->  tool_call, tool_failed, error/empty_answer   (parses; the schema rejects it)
'{"text": }'            ->  error/empty_answer                          (does not parse)
```

and that lone `empty_answer` reads:

> The turn ended without producing an answer, after **0 tool call(s)**. […] A narrower or more
> specific question is the useful next step.

which is worse than silence: it tells a chemist their question was too broad about a turn in which
the model asked for exactly the right tool, twice. This is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` reached from the reader's side —
the operator had a counter and a WARNING throughout, and the person who asked the question had a
sentence that was actively misleading.

## Decision

**Announce every call this middleware knows will not run, on the turn's own side-channel.**
`agent/model_calls._report_lost_calls` — the function formerly called `_count_invalid`, renamed
because it now writes three records for three readers — emits a `ToolFailureSignal` per lost call
beside the counter and the WARNING. `api/graph_stream` turns it into the `ToolFailedEvent` the
front door, `Chemclaw3_ui` and the storm all already read.

Four properties, each with its reason:

- **The set is "what this reply will never run", not "what failed."** A first attempt is discarded
  whole, so its *parseable* calls did not run either and are announced beside the broken ones — the
  silent drop the correction already names to the model, said to the person, who unlike the model
  cannot ask for it again. A second attempt is kept, so only its still-broken calls are announced.
  That is exactly the pair `_report_lost_calls` already receives at both call sites, which is why
  the announcement lives there rather than in a third place that would re-derive it.
- **A repaired turn announces the discarded attempt and then succeeds.** `ToolFailedEvent` is
  declared as "a step that did not work, not a failure — the turn continues", and this is that
  shape. It is the same rule `_carrying_prose` already follows for the discarded attempt's *prose*:
  it reached the chemist, so the record says so.
- **The signal carries the model's own bounded tool name, not the clamped metric label.** The
  clamp exists because `/metrics` is unauthenticated and a model-invented name mints a permanent
  series there. An event stream goes to the one person who asked, already carries model-authored
  names (`f-unknown-tool` puts `tool_that_does_not_exist` on it), and clamping here would tell that
  person a tool named `unknown` had failed.
- **No `call_id` and no `reason`.** `call_id` means "match this to the `tool_call` event", and
  measured, an unparseable call announces none — an id would point at something never emitted, and
  `""` is what `ToolFailureSignal.call_id` documents as "not attributed". `RefusalReason` names the
  five *gates*; a document that will not parse is an ordinary fault, not a control working.

**The audit row and the span stay absent, deliberately.** Both record a tool *invocation* and there
was none; synthesising one would put a call that never ran into the trail that says what ran. That
is stated in the module rather than left implied, because "no audit row" was one of the four
absences the original docstring listed and three of them are now closed.

## The second defect, found on the way

`graph_stream` kept `failed_calls`, an index of already-reported failures **by call id**, whose one
job is to stop a failed call's `ToolMessage` from also being emitted as a `tool_result`. It added
`signal.call_id` unconditionally — so an unattributed failure put `""` into an index of
attributions, and the next result carrying an empty `tool_call_id` would be dropped for a failure
that was not its own. Latent before this change and routine after it, since every lost call now
writes one. Guarded, with a test that fails in both directions.

## Consequences

- The storm's family F goes **7/8 to 8/8**; F6 is closed, and the soak's one constant failing check
  per round with it.
- A chemist whose model emits a malformed tool call is told which tool did not run and what the
  arguments were, instead of being told their question was too broad.
- A repaired turn now shows a `tool_failed` that is followed by success. Any consumer reading
  `tool_failed` as "the turn is broken" will over-report; `reason` and the event's own declared
  meaning are what distinguish the two, and both predate this change.
- `chemclaw_invalid_tool_calls_total` and `tool_failed` are fed from the same function, so an
  operator's count and a chemist's stream cannot disagree about how many calls were lost.
