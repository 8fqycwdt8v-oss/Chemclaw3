# D-2026-09-27-a-cut-result-is-kept-for-the-chemist-not-the-model — who a cut tool result is kept for

**Status:** accepted · **Date:** 2026-09-27 · **Owner decision**, recorded as one: the owner chose
the consumer, and this ADR records that choice and the build it implies. Closes the `BACKLOG.md` row
*"A cut tool result is unrecoverable, and the store that would hold it is downstream of the cut"*
(issue #471).

## Context

`D-2026-09-14-the-lossy-step-is-the-cut-and-upstream-already-offloads` established that the context
*clear* loses nothing and the *cut* in `agent/tool_result_size.py` is the one lossy step: a result
over the model's share of `agent_max_tool_result_chars` is cut head-and-tail and the middle is
replaced by a system notice. The row measured the consequence: the tool-result store
(`api/tool_results.py`, `tool_result_blobs` + `tool_result_links`) is fed by
`ToolCallTrace.returned`, which `api/graph_stream.py` calls on the `ToolMessage` the graph *emits* —
post-middleware — so the store held the cut text and the removed middle reached no store at all.

The row left one decision open, because the two possible consumers want different builds:

- **the chemist** ("show me the full result") needs the ref on the stream event to be the full
  text's, which is a plumbing question about the stream;
- **the model** (re-read what was cut) needs the notice to name a ref *and* a tool to fetch it — a
  new model-facing surface that re-inflates exactly the context the cut reclaimed.

## Decision

**The owner chose the chemist.** When a result is cut for the model, its full text is stored and
`ToolResultEvent.result_ref` names it, so the UI opens it through the existing
`GET /sessions/{id}/tool-results/{ref}`. **No model-facing re-read tool is built.**

1. **The cut hands the full text to a sink before the message leaves the middleware.**
   `bound_tool_results` (and `frame_connector_results`, for a result only its escape-expanded
   re-bound cuts) records the pre-cut text and `tool_result_size.kept_in_full` awaits the turn's
   `FullResultSink`, then stamps the returned ref on `response_metadata[FULL_RESULT_REF_KEY]`. The
   key's *presence* says "the model was shown a cut"; its *value* is the ref, or `""` when nothing
   stored the full text. `response_metadata` is the carrier `ORIGINAL_CHARS_KEY` already uses: it
   survives `model_copy`, the checkpoint and the `session_messages` JSON round trip, and it is not
   what the model reads. The thread carries a 64-character pointer, never the text.
2. **The sink is ambient, installed by the one driver that has a session.** The middleware is built
   once per profile and cached for the process, so a sink captured at build time would file one
   turn's results under another's session; and `tests/test_layering.py` forbids `agent -> api`.
   `api/runner._turn_ambient` sets `full_result_sink(session_id, correlation_id)` beside the other
   per-request ambients. The CLI and a template step install none: a cut there is still marked, and
   stores nothing, because nothing serves their results to a surface.
3. **The stream names the full text; grounding stays on the cut.** `graph_stream` passes
   `was_cut`/`full_result_ref` into `ToolCallTrace.returned`, which uses the full ref as
   `result_ref` (skipping a second write) and sets the new `ToolResultEvent.result_cut`. `preview`,
   `note_ids`, `numbers`, `values` and `ToolCallTrace.outputs` stay on the model's text, because the
   answer verifier's question is what was *in front of the model*. A reload
   (`schemas._transcript`) reads the same stamp, so `TranscriptToolCall.result_ref` names the same
   bytes and `TranscriptToolCall.result_cut` carries the same flag.

### Where the full text lives: the existing store, not a new one

Reused `tool_result_blobs`/`tool_result_links` — a full text *is* a stored tool result, and every
property the row's owner asked for is already that store's:

- **per-actor access** — the route resolves through `resolve_session` (owner or 404), and the read
  joins the link row for that session, so a ref from another session is a miss;
- **retention** — the store's own window and sweep (`durable/retention.py`), unchanged;
- **erasure** — `agent/leaver.py` and `session_store`'s session delete already take a person's blobs
  (sparing one another person still links); no new table, so no new erasure-list entry;
- **forks** — `agent/session_fork.py` copies the links, so a fork's transcript opens the same text.

Rejected: *the artifact store* (keyed by a calculation, not a turn — `api/tool_results.py`'s module
docstring already argues this split); *the audit trail* (`audit_events.detail` is bounded by
`agent_audit_max_arg_chars` and is a reviewer's record, not a chemist's fetch path); *stamping the
text itself on the message* (it would put the full text into every checkpoint and every
`session_messages` row — doubling what the cut exists to keep small in the thread).

### Size: one cap, raised, refusing rather than trimming

`stream_max_result_bytes` bounds every stored result, full texts included — one knob, as its own
comment argues. Its default moves from 128 KiB to **1 MiB**, because the old value was sized
against what the model reads and a *cut* result is by construction over the model's ceiling: the
motivating `read_document` at its own `document_read_max_chars` would have been refused. The new
floor is derived — four UTF-8 bytes a character over the largest first-party per-tool ceiling — and
`tests/test_full_tool_results.py::test_the_store_cap_admits_the_largest_first_party_result_it_exists_for`
pins it against those settings. A full text over the cap (only a connector can produce one, having
no ceiling of its own) is **refused, not trimmed**: a trimmed "full" result reads as whole, which is
the failure `stored_within_cap` exists to refuse. The stream then stores the model's cut, as before,
and that text carries the cut's system notice in-band, so it cannot read as complete.

## Consequences

- The UI contract gains one field on each of two shapes: `ToolResultEvent.result_cut` and
  `TranscriptToolCall.result_cut` (`bool`, default `false`). `result_ref`'s meaning widens from
  "the text the model read" to "the fullest stored text of this result"; a surface that already
  fetches it now opens the full result with no change.
- A cut result costs one extra write on the tool path (the full text), and no write on the stream
  path (the cut is no longer stored when the full text was).
- A deployment that set `CHEMCLAW_STREAM_MAX_RESULT_BYTES` explicitly keeps its value, and a full
  text over it falls back to the cut.

**Revisit when:** a turn needs the model itself to read what was cut — measured as answers that are
wrong *because* the removed middle was needed, not as the notice firing — at which point the
offload-and-pointer design in `D-2026-09-14-the-lossy-step-is-the-cut-and-upstream-already-offloads`
is the starting point, and the ref this stamps is already the pointer it would need.
