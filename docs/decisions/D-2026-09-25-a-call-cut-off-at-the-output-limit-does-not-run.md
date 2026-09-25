# D-2026-09-25-a-call-cut-off-at-the-output-limit-does-not-run — refusing a tool call the provider truncated

**Status:** accepted · **Date:** 2026-09-25 · Answers the question
`D-2026-08-27-an-unparseable-tool-call-is-a-visible-failure` §3 left open, and closes the
`BACKLOG.md` row *"A streamed tool call cut mid-document is completed by upstream and the tool runs
on the guess"*.

## Context

LangChain merges a streamed tool call's argument fragments with `parse_partial_json`, which closes
an unterminated string and an unclosed brace. So a reply the provider stopped at its output limit
while writing `'{"smiles": "CC'` arrives as a **valid** call reading `{"smiles": "CC"}`, and the
tool ran on a truncated molecule. Nothing on the call says it was cut;
`tests/test_invalid_tool_calls.py::test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid`
pins that upstream behaviour. The response does say so: `finish_reason` is `length` (`max_tokens`
in the Anthropic-native spelling a gateway may pass through).

## Decision

**A reply that stopped at the output limit does not run its tool calls.**
`agent/model_calls._demote_cut_off_calls`, inside `PromoteInvalidToolCalls`, moves every tool call
of such a reply onto `invalid_tool_calls`, and the existing promotion then makes each one an
ordinary refused call: no tool body, an `error` audit row, a `tool_failed` on the chemist's stream
carrying the model's own call id, and a `ToolMessage` telling the model *that the limit cut it* —
not that its JSON was invalid, which would send it to fix a document it wrote correctly as far as it
got. The model re-issues inside its own loop, under every bound the loop already has.

**Every call in the reply, not only the last.** A stream is cut once, at its end, so strictly only
the final call is truncated — but the merged message does not record which call was being written,
and running the earlier ones while refusing the last acts on half of a plan the model was still
stating.

Driven end to end: the mock gateway's new `f-cut-off` behaviour streams `'{"text": "suzuki coup'`
and ends on `finish_reason: "length"`; through the real front door the turn reports `find_notes` as
failed, returns no tool result, and the front door's log carries the cut-off refusal.
`make live-storm`'s family F now asserts it.

## Alternatives

- **Re-ask the model from inside the middleware.** Declined: a hidden retry is outside every bound
  the graph has — the history `D-2026-08-30-an-unparseable-tool-call-is-an-ordinary-tool-failure`
  records — and the same output budget would cut the same reply again. Refusing inside the loop
  costs one ordinary iteration the loop cap counts.
- **Run the completion and say so.** Declined: the completion is upstream's guess at a molecule, a
  path or a number, and a tool that ran on it has already done what the chemist did not ask for.
  **Revisit when:** a provider this system talks to reports `length` on replies whose tool calls are
  complete — visible as `chemclaw_invalid_tool_calls_total` rising with no matching truncation in
  the refused documents.

## Consequences

- **Matched by containment.** Streamed chunks merge `response_metadata` by concatenating strings,
  so a gateway that repeats `finish_reason` on a trailing usage chunk leaves `"lengthlength"`; an
  equality test let that call run. Found by review before merge, and the parametrised test's
  repeated case is red on equality.
- **The refusal shows upstream's completion, not the raw fragment** — the merged message no longer
  carries what the model actually wrote — and says so rather than calling it "what was received".
- **Past `agent_max_promoted_invalid_calls`** (20) calls in one cut reply, the rest are counted and
  named in the operator's WARNING but not promoted, so the model is not told they did not run. The
  existing bound, now reached by calls that were well-formed; a cut reply with twenty calls is
  already far outside how this system is used.

- A turn whose reply is cut off at the limit costs one more model call, which re-issues the calls.
- A provider that never reports `finish_reason` is not covered: the call runs on the completion,
  as before.

## What keeps it true

- `tests/test_invalid_tool_calls.py::test_a_call_cut_off_at_the_output_limit_does_not_run_on_upstreams_guess`
- `tests/test_invalid_tool_calls.py::test_a_reply_that_finished_normally_still_runs_its_calls`
- `tests/test_invalid_tool_calls.py::test_a_streamed_truncation_is_completed_by_upstream_and_never_becomes_invalid`
