# D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one — A derivable ref is not a fetchable one, so the transcript checks before it advertises

**Status:** accepted · **Date:** 2026-08-09

## Context

`D-2026-08-09-a-preview-is-not-a-result` gave a tool result somewhere to live and a way to fetch it:
a content-addressed store (`tool_result_blobs` + `tool_result_links`, migration 042),
`ToolResultEvent.result_ref`, and `GET /sessions/{id}/tool-results/{ref}`. It reached exactly one
surface — the live SSE stream — and filed the gap as the top row of `docs/planning/BACKLOG.md`:

> `TranscriptMessage.tool_calls` (`api/schemas.py`) carries `tool`, `arguments` and a 400-character
> `result` per call and no ref, so `GET /sessions/{id}/messages` — the route a client uses to
> rehydrate a conversation — hands back the same truncated string the preview was, for turns whose
> full results are sitting in `tool_result_blobs`.

So a chemist coming back to a conversation could see *that* `screen_hazards` ran and 400 characters
of prose about what it found, while the full text sat in the store, addressable, behind a route that
was already built. The gap was on the one path a chemist takes every single time they return, and it
is the first thing the frontend building typed result cards would hit.

Two questions had to be answered before a ref could be put in the transcript, and the row above
answered only the easy half of the first one.

## Decision

**1. A stored call finds its blob by content address, not by a join.** The transcript holds the
result text; the ref is the SHA-256 of that text; that is the same string the producer hashed. The
identity holds because MAF coerces a function result to `str` once, at the content
(`Content.from_function_result` JSON-dumps a non-string), and the durable row is that content's JSON
round trip — `PostgresHistoryProvider.get_messages` rebuilds it with `Message.from_dict`. The
transcript still coerces what it reads rather than trusting that sentence, because the row it reads
was not written by the version reading it: a stored `"result": {…}` reached `content_address` as a
`dict` and raised `AttributeError` inside `.encode`, which is a 500 on the rehydration route — a
chemist's whole conversation lost to one unaddressable result card.

The alternative was pairing through `tool_result_links`, which carries session, tool, correlation id
and a timestamp. **Those four cannot separate two calls of one tool in one turn**: they share the
session, the tool and the correlation id, and the link's `created_at` is the last time those bytes
were produced by anything rather than a per-call clock. A join on them would be right most of the
time, and its failure mode is a chemist opening a hazard screen and being shown a different
molecule's flags with nothing in the payload saying so. A mispaired result is worse than an absent
one, and the *bytes* content addressing returns cannot be the wrong bytes.

**What content addressing does not dissolve is the label, and an earlier draft of this ADR claimed
it did** ("there is no pairing step available to get wrong"). The pairing did not disappear; it
moved into the write. `tool_result_links` is keyed `(session_id, content_hash)`, so two calls in one
session that returned identical text are **one row**, and `_UPSERT_LINK`'s
`DO UPDATE SET tool = EXCLUDED.tool, correlation_id = EXCLUDED.correlation_id` relabelled that row
with whichever call wrote last. A fetch then served the right body under another call's tool name
and another turn's correlation id — the id `StoredToolResult` calls "the join a GxP reviewer asks
for" — with nothing in the payload saying so. That is precisely the near-miss pairing the paragraph
above refuses, arrived at from the other side.

It is also guaranteed rather than hypothetical. `include_detailed_errors` is off
(`agent/tool_authz.py` records the reasoning), so **every** unexpected tool exception in the system
returns the byte string `"Error: Function failed."` — one blob per session, covering every failed
call it ever makes.

So the write **collapses a disagreeing label to `''` instead of overwriting it**: `tool` and
`correlation_id` keep their value while every call that produced these bytes agrees on it, and go
empty the moment one does not. Empty means "the store will not name one call", which is the honest
answer for a row that belongs to several, and it is monotone — `''` disagrees with every subsequent
write, so a collapsed row cannot silently un-collapse. `created_at` still moves on a repeat, because
it is the retention clock rather than a label.

Rekeying the link on the *call* was the other candidate and does not solve this one. The id is
reachable — `ToolCallTrace.feed` holds it while it builds the result event, so widening `ResultSink`
from `(tool, text)` to carry it is a small change. What it cannot change is the *read*: the fetch
route is `…/tool-results/{ref}` and a ref names bytes, so several call rows would match one
`(session, ref)` and the route would be back to choosing one — with more rows, a primary-key
migration, and the same ambiguity at the end of it. Naming no call is available
without either.

Two calls that returned identical text do share one ref, and that is the store's dedup working as
designed (D-011 applied to bytes): the bytes are the same bytes, and either call resolves to them.
What the dedup costs is the two labels, and the row now says so.

**2. The address is *checked* against the store before it is advertised.** A computed address is not
a promise. It is derivable for results the store never took — it is off (`stream_max_result_bytes`
is 0), the result was over the cap, the write failed — and it stays derivable after retention has
swept the blob. A transcript that reported every computable ref would hand a client links that 404,
indistinguishable from live ones until followed.

So `GET /sessions/{id}/messages` reads the session's stored refs once
(`tool_results.fetchable_refs`, one indexed `SELECT` per transcript rather than one per call) and
`TranscriptToolCall.result_ref` carries only those. An empty ref then keeps the single meaning it
already has on the stream: **not fetchable**.

**3. Retention produces a third state, not a tombstone.** The contract is:

| `result` | `result_ref` | what it means |
| --- | --- | --- |
| `None` | `""` | the call has no result at all — it ran and nobody knows how it ended |
| set | `""` | it returned, and only these 400 characters survive |
| set | non-empty | the full text is fetchable now |

The middle row is what a swept blob leaves behind, and it is also what a never-stored result always
meant. **"Swept" and "never stored" are deliberately not separated.** They are the same instruction
to the only consumer that acts on this — there is nothing to fetch, render the text you have — and
separating them means keeping a tombstone per expired blob: a durable record of a *rendering*, on
the one table in the schema that grows at up to a row per tool call, in a store whose whole
justification is that it holds no record.

**4. The read fails the way the write does.** An unreachable store answers with an empty set through
`degraded(logger, "tool_result_store", …)` and never raises. `session_sink` swallows its write
because no rendering is worth failing a turn; the same argument is stronger here, because what a
raised error would cost is a chemist's entire conversation history rather than one tool card. The
same subsystem label is reused: from an operator's seat "the tool-result store is not answering" is
one condition whether it was noticed writing or reading.

**5. `_transcript` stays a pure projection.** The route does the one database read and passes the
set of refs in. `api/schemas.py` documents itself as touching neither `app.state`, the database nor
Temporal, which is what lets `tests/test_jobs_api.py` drive these projections without an app; a
query inside the projection would have ended that.

## Consequences

- The frontend can build typed result cards against one ref shape and one fetch route, on the live
  stream **and** on a reload. **Neither `ToolResultEvent.result_ref` nor
  `GET /sessions/{id}/tool-results/{ref}` changes** — `TranscriptToolCall.result_ref` is additive
  and is the same handle, resolved the same way.
- `GET /sessions/{id}/messages` now makes one bounded database read per reload (`SELECT
  content_hash FROM tool_result_links WHERE session_id = …`), served by the table's **primary key**
  — `(session_id, content_hash)` holds both the predicate and the projection. Measured on 20,000
  links over 200 sessions: `Index Only Scan using tool_result_links_pkey`, `Heap Fetches: 0`. Not by
  `tool_result_links_session_idx`, which an earlier draft named: that index is
  `(session_id, created_at DESC)` and carries no `content_hash`, so it answers a different question.
  The read is skipped entirely when `stream_max_result_bytes` is 0, since a store that is off has
  nothing to be asked about.
- The identity between the producer's ref and the transcript's ref is a property of MAF's content
  handling rather than of a shared function, so it is pinned by a test that drives *both* real paths
  from one MAF turn — `ToolCallTrace` fed the real `Content` objects for one derivation,
  `_transcript` reading those same contents back through `to_dict`/`from_dict` for the other — and
  asserts they agree. The result under test is a `dict` the tool never stringified, so the coercion
  the claim rests on actually happens inside the test; feeding both sides the same string literal
  (which the first version did) asserts only that `content_address` is deterministic and would stay
  green through the MAF upgrade this is meant to catch. A shared helper would have been the other
  way to get this, and it would have put `api/runner_trace`'s duck-typed result reader into
  `api/schemas.py`'s import graph to buy a guarantee the test already gives.
- `StoredToolResult.tool` and `.correlation_id` are now **empty when the store cannot name one
  call**, and a client must read an empty one as "unknown" rather than as a value. That is a
  visible change to the fetch response, and it is a narrowing of a claim rather than a loss: the
  field previously always held a name, and for a shared blob the name it held was one arbitrary
  call's. `text`, `byte_size` and `ref` are never ambiguous — those are the bytes the ref addresses,
  and every call that produced them produced exactly these.
- What this deliberately does **not** do: persist anything new. Plan snapshots, attachment
  references and the answer's `confidence`/`review_required` are still turn-time events that nothing
  writes to `session_messages`, and recovering those remains a change to what a turn *stores* rather
  than to how it is read — the line `_transcript` already draws.
