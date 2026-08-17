# SSE / event contract — Python front door vs. the UI

Boundary audited: `chemclaw/api/events.py` + `api/graph_stream.py` + `api/routes/turns.py` +
`api/routes/streams.py` (emit side) against `chemclaw3_ui/shared/events.ts` and every reader of an
event in `chemclaw3_ui/src/` (consume side), plus the BFF at `chemclaw3_ui/server/proxy.ts`.

**The discriminator set is genuinely consistent.** All 17 members of the backend `Event` union appear
in `EVENT_TYPES` (`shared/events.ts:319`); nothing the backend emits is dropped by `normalizeEvent`
as unknown. Every finding below is a *field*, a *case* or an *ordering* mismatch inside that agreed
set. The transport layer is also sound: the BFF forwards `text/event-stream` uncompressed, un-idle-
timed, with `x-accel-buffering: no`, injects only comment frames and only at frame boundaries, and
propagates a client disconnect into the upstream request (`server/proxy.ts:96,124-136,164-172`).

Reproduction scripts and their raw output are in
`/home/user/Chemclaw3/tasks/audit-2026-08-16/repro/sse-events/`. The `.ts` files run from the
`chemclaw3_ui` repo root with `npx vite-node <file>`; `emit.py` runs under `/home/user/Chemclaw3/.venv/bin/python`
and writes the wire JSON the `.ts` probes read.

---

## `loop_cap_reached` is a mid-turn qualifier on the backend and a fatal error in the UI, so the partial answer it qualifies is thrown away

**Severity** High

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/runner.py:368-386` (yields `ErrorEvent(code="loop_cap_reached")`) followed by `:432-444` (`build_answer_event` → `yield answer`); the contract states the rule at `/home/user/Chemclaw3/src/chemclaw/api/events.py:360-369` — "`loop_cap_reached` is the only member that shares its turn with an answer … it arrives after those tokens and *before* the `AnswerEvent`".
- UI: `/workspace/chemclaw3_ui/src/api/streamTurn.ts:92-94` — `if (event.type === 'error') throw errorFromEvent(event);` — which exits the read loop and, in the `finally` at `:107-112`, cancels the response body.

**Trigger** Any turn on a harness-enabled profile that reaches `harness_max_loop_iterations`
(`enforce_loop_cap` is wired unconditionally for such profiles at
`/home/user/Chemclaw3/src/chemclaw/agent/langgraph_agent.py:454`). The backend then emits
`token…`, `error(loop_cap_reached)`, `answer`.

**Consequence** The `answer` frame is never read. `streamTurn` rejects, `sendMessage` takes the
failure branch (`/workspace/chemclaw3_ui/src/state/sendMessage.ts:220-269`) and marks the message
`status: 'error'`, so:
- `finalText` is never set — the bubble renders `streamedText` instead
  (`src/components/MessageList.tsx:73`), which is only equal to `answer.text` when no helper ran
  (see the next-but-one finding);
- every scored field of the answer is lost: `review_required`, `confidence`, `unsupported_claims`,
  `verified_by`. A capped turn that *also* tripped the verifier shows no review pill at all —
  precisely the "mark the answer partial rather than present it as the finished work" the backend
  comment at `runner.py:362-367` says this event exists for;
- the composer is released with an error banner and no retry action (`retryable=false`), so the turn
  reads as a failure rather than as a partial success.

Additionally, and racily (not reproduced): cancelling the body closes the socket, which the BFF turns
into an upstream destroy and FastAPI into a disconnect. If that cancellation lands before
`runner.py:448`, `consume_turn_approval` is skipped and a spent plan approval stays armed.

**Evidence** `repro/sse-events/repro-loopcap.ts` builds the exact three-frame byte stream and calls
the real `streamTurn`:

```
THREW: ApiError / kind= agent
message: The turn reached its 25-iteration limit and stopped with work still open, so the answer below is partial (session s1).
events delivered to the store: [ 'token' ]
```

The `answer` frame was on the wire and never reached `onEvent`.

**Fix** The backend already distinguishes the two families in prose but not in the type. Either
(a) give the cut-off family its own event (`turn_truncated`) so `error` stays terminal on both sides,
or (b) have `streamTurn` treat `loop_cap_reached` as non-terminal: record it on the message
(a "partial" flag) and keep reading until `answer` or the stream ends. (b) is a one-line change and
matches what `events.py:360-369` already asserts; (a) is the version that survives a third member
joining the family.

---

## An admission shed says "retry shortly" and the UI locks the composer as if the budget were spent

**Severity** High

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/routes/turns.py:114-122` — on admission timeout it emits `ErrorEvent(message=_AT_CAPACITY, code="budget_exhausted", retryable=True)`, commented "Retryable and honestly so: shedding says 'not now', not 'not ever'". `_AT_CAPACITY` is `"server at capacity; retry shortly"` (`api/middleware.py:30`).
- UI: `/workspace/chemclaw3_ui/src/api/errors.ts:131-138` — `case 'budget_exhausted'` spreads the event's options and then hard-overrides `retryable: false`; `/workspace/chemclaw3_ui/src/state/sendMessage.ts:251-255` — that kind sets `composerLock('budget_exhausted')` and returns without a retry action; `/workspace/chemclaw3_ui/src/components/Composer.tsx:256-260` renders "The usage budget for this service is exhausted. New turns are refused until it resets."

**Trigger** `service_max_concurrent_turns` permits are all held and none frees within
`service_turn_admission_timeout_seconds` — i.e. ordinary load, not a spent budget.

**Trigger is not shared with the real budget refusal**, which the same route emits 25 lines later
with `retryable=False` (`turns.py:139-145`). The two are indistinguishable on the wire because
`ErrorCode` has no `capacity` member (`api/events.py:343-357`), even though the UI already declares a
`capacity` kind for exactly this (`src/api/errors.ts:31`) — it is reachable only from a 503 status,
which D-166 stopped producing for a shed.

**Consequence** A transient capacity shed permanently disables the composer for that conversation
(only `createConversation` / `selectConversation` / `deleteConversation` / `resetSession` clear
`composerLock`) and tells the chemist a factually wrong thing: that the usage budget is exhausted
and turns are refused until it resets. The backend's own `retryable=true` is discarded by the
override, and the "Retry" affordance is suppressed by the `budget_exhausted` early return.

**Evidence** `repro/sse-events/repro-shed.ts`, fed the shed frame verbatim:

```
backend said retryable = true
UI ApiError kind = budget_exhausted | retryable = false
sendMessage.ts:251 branch => setComposerLock('budget_exhausted'); banner with NO retry action
```

**Fix** Add `capacity` to `ErrorCode` in `api/events.py` and emit it from `turns.py:120` (the 503 path
in `middleware.py` already uses the same wording, so both shapes stay aligned); add it to
`ERROR_CODES` and the `ErrorCode` union in `shared/events.ts` and map it to the existing
`capacity` kind in `errorFromEvent`. Until the backend ships that, the UI's `retryable: false`
override is unsafe as written — it should only fire when the event itself said `retryable: false`.

---

## `TokenEvent.agent` does not exist in the UI contract, so a helper's working prose is streamed as the answer and then silently swapped out

**Severity** Medium

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/events.py:92-109` — `TokenEvent.agent`, documented as "load-bearing here in a way it is not on the other events"; emitted at `/home/user/Chemclaw3/src/chemclaw/api/graph_stream.py:152-153` as `agent or ("subagent" if namespace else "")`; consumed at `/home/user/Chemclaw3/src/chemclaw/api/runner.py:311` — only unattributed tokens join `answer_parts`, i.e. `answer.text`.
- UI: `/workspace/chemclaw3_ui/shared/events.ts:71-74` — `interface TokenEvent { type; text }`, no `agent`; `normalizeEvent` at `:456-457` drops it; `/workspace/chemclaw3_ui/src/state/chatStore.ts:580-582` appends **every** token to `streamedText`.

**Trigger** Any turn where the model calls the `task` tool. This is not hypothetical: `SubAgentMiddleware`
is in deepagents' `_REQUIRED_MIDDLEWARE` and "the `task` tool ships regardless"
(`/home/user/Chemclaw3/src/chemclaw/agent/langgraph_agent.py`, `_subagents` docstring), with a
governed helper installed at `:256`.

**Consequence** While the helper runs — the longest stretch of a delegated turn — the chemist reads
the helper's scratch prose as the answer. At the `answer` event, `finalText` replaces `streamedText`
(`chatStore.ts:585-597`, `MessageList.tsx:73`), so the visible text silently changes and shrinks.
The `agent` field the UI mirror already carries on three other events is the exact discriminator
needed, and it is the one event that omits it.

**Evidence** `repro/sse-events/repro-subagent.ts` drives the real store with the frames
`graph_stream.py` produces:

```
streamedText (what the chemist read while it ran):
   "Checking the ELN. [helper] I will grep three notes and summarise…The batch used 2.0 eq of base."
finalText   (what replaces it at the answer):
   "Checking the ELN. The batch used 2.0 eq of base."
identical: false
```

`repro/sse-events/repro-contract.ts` confirms the field is dropped by the normalizer:
`token_subagent   normalized=yes  dropped=[agent]`.

**Fix** Add `agent?: string` to `TokenEvent` in `shared/events.ts`, populate it in `normalizeEvent`,
and have `chatStore.applyEvent` route attributed tokens somewhere other than `streamedText` — a
side channel in the trace, or dropped. The rule the backend applies (`runner.py:311`) is the one the
UI must mirror: unattributed tokens are the answer, attributed ones are not.

---

## `PlanEvent.plan_hash` is discarded by the normalizer, so the UI still does the second round-trip the field exists to remove

**Severity** Medium

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/events.py:31-57` — the field's whole docstring is that without it "this event cannot be acted on … the only way to answer the plan it had just rendered was a second `GET /sessions/{id}/plan` round trip — which races the very change the binding exists to catch". Emitted at `/home/user/Chemclaw3/src/chemclaw/api/graph_stream.py:322`.
- UI: `/workspace/chemclaw3_ui/shared/events.ts:45-50` — `interface PlanEvent { type; todos }`, no `plan_hash`; `normalizeEvent` at `:447-448` returns `{ type: 'plan', todos }` only. `/workspace/chemclaw3_ui/src/components/Prompts.tsx:157-176` then performs exactly the `GET /sessions/{id}/plan` the field was added to eliminate, and again at `:196-202` after a 409.

**Trigger** Any harness-mode turn that emits a plan and an `approval_request` with an empty
`approval_id`.

**Consequence** The wire field is unreachable by any UI code — the TypeScript interface prevents a
consumer from ever seeing it, and `normalizeEvent` strips it before that. The UI's own header comment
(`shared/events.ts:8`, "Verified against … `src/chemclaw/api/events.py`") is true at member level and
false at field level. Practically the card stays correct (it renders the *fetched* plan, so the hash
and the displayed todos agree), so the cost is the extra round trip and a permanently dead field —
but the mismatch also means the `plan` frames rendered in the trace (`chatStore.ts:256-257`,
`latestPlan` at `:661`) carry no identity, so nothing in the transcript can say *which* plan a
checklist was.

**Evidence** `repro/sse-events/repro-contract.ts`:
`plan                 normalized=yes  dropped=[plan_hash]`.

**Fix** Add `plan_hash: string` to the UI's `PlanEvent` and carry it through `normalizeEvent`
(`asString(o.plan_hash)`, with `''` meaning "predates the field — fetch it", exactly as
`events.py:54-57` specifies). `PlanApprovalPrompt` can then bind to the hash it was streamed and skip
the GET when it has one.

---

## `tool_result` / `tool_failed` are paired to their `tool_call` by tool name and order, ignoring the `agent` both sides carry — so a helper's result closes the root's call

**Severity** Medium

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/runner_trace.py:198,219` — `issued(key, tool, arguments)` / `returned(key, text)`, both keyed on the call id — but the wire events (`api/events.py:77-89`, `:274-336`, `:260-271`) carry no id. `agent` *is* carried on all three (`events.py:89,271,336`) and is set from the namespace at `/home/user/Chemclaw3/src/chemclaw/api/graph_stream.py:188-197`.
- UI: `/workspace/chemclaw3_ui/src/state/chatStore.ts:212-228` — `closeToolCall` matches "the oldest still-open row for this tool" and never looks at `agent`, although the row stores it (`:262`).

**Trigger** Two in-flight calls to the same tool whose results arrive out of issue order. With the
`task` helper live, a root call and a helper call to the same tool are genuinely concurrent (separate
graphs, separate tool nodes, interleaved on one stream).

**Consequence** The preview, the `result_ref` and the untruncated `numbers` are attached to the wrong
call. `numbers` is what `chem/provenance.ts` grades the answer's figures against, and `result_ref` is
what the "see the full result" affordance fetches — so a chemist can open a helper's evidence sweep
believing it is the answer to the root's question.

**Evidence** `repro/sse-events/repro-pairing.ts` — two `gather_evidence` calls, distinguishable by
`agent` on every frame, helper answering first:

```
call agent=""         args={"q":"ROOT question"}   -> result="HELPER RESULT" ref=h
call agent="subagent" args={"q":"HELPER question"} -> result="ROOT RESULT"  ref=r
```

**Fix** Two independent halves. (1) UI, immediately: make `closeToolCall` match on
`(tool, agent)` — the discriminator is already on both events and already stored on the row.
(2) Backend, properly: put the call id on `ToolCallEvent`, `ToolResultEvent` and `ToolFailedEvent`.
The producer holds it (`runner_trace.issued/returned` are keyed on it) and it is the only thing that
makes the pairing exact rather than heuristic; `chatStore.ts:206-210` already documents that it is
guessing because nothing on the wire says otherwise.

---

## Job push-back is at-most-once; the UI is written for at-least-once and has no re-sync, so a disconnect between the claim and the frame loses the completion permanently

**Severity** Medium

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/agent/session_events.py:94-118` — `claim_unconsumed` runs one `UPDATE … FOR UPDATE SKIP LOCKED … RETURNING` and **commits** before returning the rows; `:154-156` — "at most once across tailers (a claimed row is never re-delivered)". `/home/user/Chemclaw3/src/chemclaw/api/routes/streams.py:111-128` then serializes each claimed row into an SSE frame — with no `id:` field, so there is no Last-Event-ID to resume from.
- UI: `/workspace/chemclaw3_ui/src/state/chatStore.ts:697-717` — `pushJobFinished`, commented "Re-delivery is expected: the stream reconnects with backoff and **delivery is at-least-once**". `/workspace/chemclaw3_ui/src/hooks/useJobStreams.ts:96-165` — the reconnect loop reopens the stream and never asks for anything it may have missed.

**Trigger** The client's socket drops (tab close/reopen, laptop sleep, BFF restart, pod restart)
between the claim's `COMMIT` and the browser reading the frame. Also: two tabs on one account watching
the same session — the SQL claim hands the row to whichever tab polls first, and the other tab never
sees it.

**Consequence** The completion is gone: `consumed_at` is set, the row is never re-claimed, no
`job_completed`/`job_failed` reaches the UI. `settleJob` (`chatStore.ts:242-250`) is never called, so
the `job_started` trace row keeps its "runs asynchronously" badge for the life of the conversation,
and no `JobFeedItem` is ever created. Nothing reconciles: `JobsPanel` only calls `api.listJobs` from a
manual search (`src/components/JobsPanel.tsx:168-172`), never against the in-flight rows of a
transcript. The UI's stated delivery model is the inverse of the backend's, so its only defence
(the dedupe at `chatStore.ts:699-703`) guards against a hazard that cannot occur while the hazard that
can occur is unguarded.

Note also that `useJobStreams.ts:15` says the claim "is destructive and scoped to `job_completed` in
SQL" — it has been scoped to both kinds since `streams.py:111-112`; the comment is stale but harmless.

**Evidence** Read directly off the two files: `claim_unconsumed` commits the `consumed_at` write
(`session_events.py:110-115`) before `stream_new_events` yields (`:166-167`) and before
`streams.py:128` writes the frame; the frame carries `{"event", "data"}` only, no `id`; and the UI's
reconnect path (`useJobStreams.ts:99-106`) issues a plain `GET` with no resume header.

**Fix** Either make the delivery match the UI's belief — claim on *ack* rather than on read (a
visibility timeout, or `id:` on the frame plus `Last-Event-ID` re-delivery from the last acked id) —
or give the UI a reconciliation pass: on stream (re)connect, `GET /jobs` for the job ids of any
unsettled `job_started` trace rows and settle them from the registry. The second is cheap and is the
one that also fixes the two-tab case.

---

## `evidence_source` is normalized and then silently discarded — no trace kind, no renderer

**Severity** Low

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/events.py:396-419`; emitted at `/home/user/Chemclaw3/src/chemclaw/api/graph_stream.py:218-221`.
- UI: declared and normalized (`shared/events.ts:274-284`, `:491-496`) — and then `traceEntryFor` (`/workspace/chemclaw3_ui/src/state/chatStore.ts:253-307`) has no `case 'evidence_source'`, so it falls to `default: return null` and `applyEvent` returns at `:644-645`. `TraceKind` (`/workspace/chemclaw3_ui/src/state/types.ts:16-26`) has no member for it and `TracePanel.tsx` no branch. A repo-wide grep finds exactly zero readers in `src/`.

**Trigger** Every `gather_evidence` sweep.

**Consequence** The per-branch retrieval arithmetic — the whole point of the event, added because a
source contributing zero chunks was invisible in the merged list — reaches the browser and is thrown
away. This is the same shape as the three misses `shared/events.ts:11-28` records at member level:
adding the discriminator to `EVENT_TYPES` is necessary and not sufficient, and this one got the
discriminator and nothing else.

**Evidence** `repro/sse-events/repro-contract.ts` shows the frame normalizing cleanly
(`evidence_source      normalized=yes  dropped=[]`); the store then drops the normalized object at
`chatStore.ts:644-645` because `traceEntryFor` returned `null`.

**Fix** Add `'evidence_source'` to `TraceKind`, a `case` to `traceEntryFor`, and a branch in
`TracePanel`. If it is deliberately not rendered, say so where the drop happens — a member in
`EVENT_TYPES` with no consumer is indistinguishable from the bug that list exists to prevent.

---

## `AnswerEvent.challenged` and `AnswerEvent.review_hold_id` are absent from the UI mirror

**Severity** Low

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/events.py:246-257` (`challenged`, `review_hold_id`), set from `score_answer` at `/home/user/Chemclaw3/src/chemclaw/api/runner_answer.py:50-58`.
- UI: `/workspace/chemclaw3_ui/shared/events.ts:135-160` declares five of the seven answer fields; `normalizeEvent` at `:519-528` drops the other two.

**Trigger** Any `answer` event — the fields are always on the wire.

**Consequence** None today: both are pinned at their defaults since the challenge panel was removed,
which `events.py:253-257` states. It matters because that comment also states the fields are kept
declared *because* removing a member is a coordinated change across `Chemclaw3_ui` — i.e. the backend
is holding a field for a mirror that never mirrored it. If a durable review hold is re-introduced,
`review_hold_id` will arrive at a UI that cannot see it.

**Evidence** `repro/sse-events/repro-contract.ts`:
`answer_full          normalized=yes  dropped=[challenged,review_hold_id]`.

**Fix** Mirror both (`challenged: boolean`, `review_hold_id: string | null`) even while they are
constant, or drop them from the backend model. Half a coordinated change is worse than either whole.

---

## The job-push-back reconnect loop retries a 404 (dead session handle) forever, silently

**Severity** Low

**Location**
- Backend: `/home/user/Chemclaw3/src/chemclaw/api/routes/streams.py:159-161` — the route depends on `resolve_session`, which 404s an unknown, evicted or non-owned session (`/home/user/Chemclaw3/src/chemclaw/api/deps.py:146-159`).
- UI: `/workspace/chemclaw3_ui/src/hooks/useJobStreams.ts:125-129` — every non-429, non-OK status falls into `attempt += 1; await backoff(attempt)` and loops.

**Trigger** A conversation whose `sessionId` was evicted from the backend's live-session LRU while a
job it launched is still running.

**Consequence** The stream reconnects roughly every 30 s forever against a handle that can never come
back, with nothing surfaced. The chat path already knows how to handle this — `sendMessage.ts:199-207`
mints a new session on `session_not_found` — but that recovery never reaches `useJobStreams`, which
keeps polling the dead id, and the job's completion (recorded against the *old* session id) is
unreachable either way.

**Evidence** Read off the two files: a 404 is not the 429 branch and not `res.ok`, so it is the
generic retry; `backoff` caps at 30 s (`useJobStreams.ts:169-183`) and the loop's only exit is
`controller.signal.aborted`.

**Fix** Treat 404 as terminal for that session's stream — stop the loop and let the store's
session-recovery path decide — rather than as a transient transport failure.
