# D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure — and a *truncated* one is not unparseable at all

## Status

Accepted. Written while adding compiled-graph coverage for the repair
`D-2026-08-27-a-refusal-is-not-a-crash` shipped, and while running the check that repair was
supposed to turn green. It does not supersede that ADR: the repair stands exactly as merged. What
this records is the measured **boundary** of it, and the second defect the measurement found behind
the `BACKLOG` row's own acceptance criterion.

## Context

The row said: surface `invalid_tool_calls`, an unparseable tool call is a silent no-op — and
*"verify with `make live-storm`'s `f-malformed-json` check, which currently fails"*. The first half
was true and is fixed. The second half was the acceptance criterion, and it does not follow from
the first.

**The defect the row named is real, and it is on the non-streaming path.** Reproduced against
LangChain's own converter, with no first-party code involved:

```
$ python -c "... _convert_dict_to_message({'role':'assistant', 'content':'I will compute that.',
      'tool_calls':[{'id':'call-1','type':'function',
                     'function':{'name':'predict_pka','arguments':'{\"smiles\": \"CC'}}]})"
tool_calls        : []
invalid_tool_calls: [{'name': 'predict_pka', 'args': '{"smiles": "CC',
                      'error': '... are not valid JSON. Received JSONDecodeError
                                Unterminated string starting at: line 1 column 12 (char 11) ...'}]
```

The agent loop iterates `tool_calls`, so the call is gone before anything first-party sees it.
`agent/model_calls.RepairInvalidToolCalls` reads the other field, counts it, and asks the model
again from inside `wrap_model_call`.

**But the path a turn actually takes is the streaming one, and there the same truncation never
becomes invalid.** A streamed tool call arrives as `tool_call_chunks`, and `AIMessageChunk.__add__`
parses the accumulated document with `langchain_core.utils.json.parse_partial_json` — which
*completes* any document that is a prefix of a valid object. Measured over seven documents:

| argument document | outcome | resulting `args` |
| --- | --- | --- |
| `{"smiles": "CC` (a cut value) | silently completed → **valid** | `{'smiles': 'CC'}` |
| `{"text":` (cut before the value) | silently completed → **valid** | `{}` |
| `{` | silently completed → **valid** | `{}` |
| `"benzene"` (not an object) | `invalid_tool_calls` | — |
| `{"text": "x"}}` (unbalanced close) | `invalid_tool_calls` | — |
| `not json at all` | `invalid_tool_calls` | — |
| `["text", "x"]` | `invalid_tool_calls` | — |

Only a document that is **not a prefix** reaches the field the repair reads. A cut stream and an
exhausted token budget — the two causes the row itself named as the production ones — produce
prefixes by construction.

**Confirmed against the running lane, through the real provider client**, not in isolation.
`build_chat_model()` against the mock at `127.0.0.1:8820/v1`, asking for the storm's own
`f-malformed-json` behaviour (`'{"text": "unterminated'`):

```
--- astream ---
tool_calls        : [{'name': 'find_notes', 'args': {'text': 'unterminated'}, 'id': ...}]
invalid_tool_calls: []
finish_reason     : tool_calls
```

And `make live-storm --families F` end to end, with the full lane up (Postgres, Temporal, four
workers, the connector fleet including `chem` and `safety` from `Chemclaw3-mcp`, the front door on
the mock model) — **7/8, and the one failure is the row's own criterion**:

```
| F | a truncated argument document is reported, not swallowed | FAIL |
      HTTP 200, answered=False, error=empty_answer, tools_failed=[],
      result[0]='matches=[] total_matches=0 widened=False' |
```

`tools_failed` is empty and there is a *result*: `find_notes` **ran**, with
`text="unterminated"` — an argument value the model never finished writing. The neighbouring
`f-wrong-argument` check passes precisely because its document *is* valid JSON and fails schema
validation instead, which is what makes the contrast diagnostic rather than anecdotal.

So the storm check was never going to pass by way of `invalid_tool_calls`, and it was not failing
for the reason the row gave. It is failing because a truncated argument document is silently
completed and the tool is run on the guess.

## Decision

**1. The repair stands, unchanged, and is now proven inside a compiled graph.** The existing
coverage drives `awrap_model_call` directly with a hand-built `ModelRequest`, which is the right
shape for asserting what the middleware *decides* and cannot establish that the decision is
connected to anything — the property `tests/test_state_channels.py` exists for after three defects
in one week. `tests/test_invalid_tool_calls.py` drives `create_agent` over a real tool and a
recording model double, on **both** `invoke` and `ainvoke`, and establishes the three facts only the
graph can show: upstream's real handler tolerates the second call the repair makes; the correction
composed with `request.override(messages=…)` is in the thread the provider was actually handed
(a graph reading the thread off state instead would have composed, logged and counted a correction
nobody sent); and the repaired call reaches the tool node and comes back as a `ToolMessage`.

The defect is asserted as a **behavioural diff** rather than described: the identical script through
a graph with no repair composed ends with `"I will look that up."`, no tool call and no error —
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` reproduced.

**2. The `after_model` constraint is measured rather than restated.** The rejected design — the
same repair as `@after_model(can_jump_to=["model"])` — was built and run against the same script and
the same graph:

| | provider calls | `after_model` events seen by a probe | `model_calls` | broken messages left in state |
| --- | ---: | ---: | ---: | ---: |
| `wrap_model_call` (shipped) | 3 | 2 | 2 | 0 |
| `after_model` jump (rejected) | 3 | 2 | **3** | **1** |

Three separate harms in one row, and each is one of D-2026-08-15's. The jump **spends a loop
iteration** on the malformed emission, so the runaway cap's budget pays for the model's mistake. It
**skips a hook**: `after_model` runs in reverse list order, so the probe placed earlier in the list
never saw the malformed response at all — 2 events over 3 model calls — which is exactly how
`ModelCallLimitMiddleware`'s increment came to be skippable. And the broken assistant message stays
in `messages`, which is checkpointed, so it is replayed into the model's context for the life of the
thread. The shipped design has none of them because the graph never learns the discarded attempt
existed. `tests/test_invalid_tool_calls.py` asserts the equality those numbers separate.

**3. The streaming completion is recorded as an open defect and is not fixed here.** It is a
different failure with a different remedy, and guessing at one would be the third answer to a
question this repository has twice insisted on measuring first:

- It is not reachable from a middleware. By the time any `wrap_model_call` handler returns, the
  arguments have been completed and are indistinguishable from a call the model finished writing;
  the raw accumulated document is gone. The information lives in the merge, one layer below.
- The honest signal for a *real* truncation is `response_metadata["finish_reason"] == "length"`,
  which **is** readable on the merged message — but the storm's synthetic case reports
  `finish_reason: "tool_calls"`, so the check would still not pass and the mock's behaviour is not
  a faithful stand-in for the production cause. Fixing the check and fixing the hazard are two
  changes, and conflating them is what would make the storm green while the hazard stayed.
- Deciding it properly means deciding whether a `length`-truncated tool call should be refused,
  re-asked, or run — and that is a decision about a chemist's arguments, taken with a measurement
  of how often it happens on the real endpoint, not on a mock.

**4. The boundary is a test, not a sentence.** `tests/test_invalid_tool_calls.py` pins
`parse_partial_json`'s completion of prefixes as an **absence** assertion, the shape
`tests/test_upstream_surface.py` uses: if upstream ever stops completing them, these calls begin
arriving as invalid, the repair starts firing on them, and the test turns red — which is the signal
to re-read this ADR rather than to discover the change through behaviour.

## Consequences

- `chemclaw_invalid_tool_calls_total{tool}` counts the non-streaming case and will read **zero** on
  a streaming deployment whose provider never emits non-prefix garbage. That is the correct
  reading, and this ADR is what stops it being read as "no truncation happens here".
- `make live-storm --families F` remains **7/8**. The failing check is now understood and is not
  evidence about `invalid_tool_calls`; whoever closes it should change the mock's document to one
  `parse_partial_json` cannot repair *and* decide the `finish_reason` question above, in that order.
- Both `wrap_model_call` and `awrap_model_call` are exercised through a compiled graph, closing the
  `RecordContextCompaction` trap — a middleware declaring either is composed into both chains, so a
  repair that worked on one path only would raise under `graph.invoke`.

## Alternatives considered

**Write the repair as an `after_model` jump.** Rejected on the measurement in §2, which is the
same conclusion `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` reached from
the other direction. Recorded here with numbers because that ADR's evidence is about a counter and
this is about a repair, and the next person to reach for the hook will be reaching for it here.

**Add a second ADR-worthy fix for the streaming case in the same change.** Rejected: see §3. A
control shipped to make a red check green, without a measurement of the failure it is supposed to
catch, is the shape this repository has removed four times.

**Loosen the storm check so it passes.** Rejected outright. The check's claim — "a truncated
argument document is reported, not swallowed" — is *correct*, and the system does not satisfy it.
A check weakened to match the behaviour it was written to catch is worse than no check.
