# D-2026-10-03-a-draft-is-read-off-the-arguments-the-model-is-still-writing — where `exhibit_draft` comes from

**Status:** accepted · **Date:** 2026-10-03 · Wave 2 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, implementing the frozen
`exhibit_draft` event (turn stream only, never persisted, never on `/events`).

## Context

A report the agent writes into `create_exhibit` is one tool call whose arguments take as long to
generate as the report does — a minute of output for a long document. `api/graph_stream.py` reads
tool calls from the `updates` mode, where a call arrives whole once the model node completes, so the
chemist saw nothing and then the finished artefact. The contract added a preview event carrying the
whole document so far, throttled, growth-only and capped. The text exists during generation in
exactly one place this system can see: the `messages` mode's `AIMessageChunk.tool_call_chunks`,
the fragmented argument stream `graph_stream` deliberately does not read calls from, because
reassembling it cost the previous engine two live-run defects (D-138, and the provider that
announced ten `tool_call` events for one call).

## Decision

**Read the fragments, for the preview only** (`api/exhibit_drafts.DraftStream`). Each root-agent
chunk's fragments are accumulated per call (keyed by message and `index`, because only a call's
first fragment carries its id and name), the arguments so far are parsed with LangChain's own
`parse_partial_json`, and a frame is emitted when the partial `spec` is a document — `kind`
`"document"`, or not yet written — whose `markdown` grew. Every `tool_call`, `tool_result` and
`exhibit` event still comes from the completed node, so a reassembly mistake here costs a wrong
preview that the `exhibit` event then replaces, which is the failure a preview is allowed to have
and the one D-138's defects were not.

- **Throttle by the last parse, not the last frame**, once a frame has gone: frames of one call are
  at least `exhibit_draft_min_interval_ms` apart, and re-parsing a growing 200 kB document is paid
  at the frame rate rather than the chunk rate. Before the first frame every fragment is parsed —
  the arguments are still a title and a kind — so the first frame is not held back an interval.
- **One `done` frame when the model node completes**, carrying what the throttle held back, and only
  if the text grew. It is once per call, so it bounds itself; without it the last quarter-second of
  a document would appear only with the `exhibit` event.
- **Stopped for good** when a revision passes `edits` (replacements, not a document), the spec is
  another kind, or the text passes `exhibit_max_spec_bytes` — the tool would refuse it.
- Root agent only (`len(namespace) <= depth`), and never a model call made inside a tool body —
  the same two rules the token branch applies.

## Options considered

- **A callback on the model** (`on_llm_new_token`) publishing through the stream writer. It sees
  the same fragments one layer lower, needs the turn's writer inside a callback LangGraph does not
  scope to a node, and would reach the stream as a `custom` payload the translator then has to order
  against the very `messages` chunks it came from. Declined: same input, more moving parts.
- **Have the model stream the document as answer text and then create the artefact from it** — a
  prompt rule rather than a stream reader. It doubles the document's output tokens on every report
  and puts the draft in the transcript, which the contract forbids. Declined.
- **No preview** — wait for the `exhibit` event. The status quo, and what this exists to end.

## What keeps it true

- `tests/test_exhibit_drafts.py` — through the real compiled graph and `graph_events` with a model
  that streams a call's arguments in fragments: frames before `tool_call` → `tool_result` →
  `exhibit`, each the whole text so far and growing, the throttle holding a call to its first frame
  and one closing frame, a table drafting nothing; and on `DraftStream`, a revision by `spec` named
  by its id, by `edits` silent, the cap, and markdown written before its `kind`.
- `tests/test_event_contract.py` and `tests/test_dev_page_events.py` — the event in the published
  union and on the dev page.
