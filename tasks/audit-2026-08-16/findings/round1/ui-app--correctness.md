# ui-app — CORRECTNESS (round 1)

Repo: `/workspace/chemclaw3_ui`, slice `src/`.
Four findings, each reproduced by a script run under the repo's own vitest/happy-dom setup.
The temporary test files were deleted after the runs; the code shown below is verbatim from the
repo and the printed output is quoted exactly.

---

## The job push-back stream reconnects in an unthrottled tight loop when the response body ends

- **Severity**: high
- **Location**: `/workspace/chemclaw3_ui/src/hooks/useJobStreams.ts:96-165` (`openStream`), specifically the `attempt = 0` at :131 and the loop exit at :156
- **Trigger**: any `GET /sessions/{id}/events` that returns `200` and then ends its body. The backend reaches this state on its own: `stream_new_events` is documented as "A connection failure ends the stream (the client reconnects)" (`/home/user/Chemclaw3/src/chemclaw/agent/session_events.py:141`), so a Postgres outage or an exhausted pool terminates every stream at connect time. The same code path is also taken for a `200` that is *not* an event stream at all — `openStream` never checks `content-type`, unlike `streamTurn` (`src/api/streamTurn.ts:59-64`) — so a JSON or HTML `200` from an intermediary does it too.
- **Consequence**: `attempt` is reset to `0` the moment the HTTP response is `ok`, and a clean `done` from the reader falls straight out of the inner loop back to `while (!controller.signal.aborted)` with **no `backoff()` call and no `attempt` increment**. The client then re-fetches immediately, forever. Measured: **50 requests in 250 ms** for one watched session (the run hit my own cap of 50; it is unbounded without one). `MAX_JOB_STREAMS` is 3, so one tab issues roughly 600 requests/second against a service that is, by construction of this failure mode, already unhealthy. The 429 throttle cannot save it either: `consecutive429` only counts real `429`s, and this path never produces one.
- **Evidence**:

  ```ts
  //  src/hooks/useJobStreams.ts
  131      attempt = 0;
  132      const reader = res.body
  ...
  138        for (;;) {
  139          const { done, value } = await reader.read();
  140          if (done) break;          // <- clean EOF: no backoff, no attempt++
  ...
  156        }
  157      } finally {
  158        await reader.cancel().catch(() => undefined);
  159      }
  160    } catch {                        // <- only a THROWN error reaches the backoff
  161      if (controller.signal.aborted) return;
  162      attempt += 1;
  163      await backoff(attempt, controller.signal);
  164    }
  ```

  Reproduction (vitest, happy-dom, real `useJobStreams` behind a real `AuthGate`; `fetch` stubbed to answer `200 text/event-stream` with a body that closes immediately, capped at 50 so the test could terminate):

  ```
  stdout | audit: job stream reconnect ... > refetches in a tight loop when the body ends immediately
  FETCHES TO /events IN 250ms: 50

  AssertionError: expected 50 to be less than 5
  ```

  Corroborating: an unrelated test of mine that answered every non-`/messages` URL with
  `200 application/json "[]"` hung vitest until the 180 s timeout, because that response is also a
  body that ends — the missing `content-type` check makes it indistinguishable from a stream.

- **Fix**: treat a completed body as a reconnect attempt, not as a success. Move the reset of `attempt` to the point where a *frame* is actually received, and back off on the way round the loop:

  ```ts
  if (!contentTypeIsEventStream(res)) { attempt += 1; await backoff(attempt, controller.signal); continue; }
  let sawFrame = false;
  // ... inside the read loop, on the first decoded frame: sawFrame = true; attempt = 0;
  // after the finally:
  if (!sawFrame) attempt += 1;
  await backoff(attempt || 1, controller.signal);
  ```

  A stream that delivered nothing before closing must never be re-opened with zero delay.

---

## The banner's "Retry" after a failed turn is a dead control

- **Severity**: medium
- **Location**: `/workspace/chemclaw3_ui/src/App.tsx:120-123` (`onRetry`), wired at `:132`; the producer is `/workspace/chemclaw3_ui/src/state/sendMessage.ts:258-269`; the button is `/workspace/chemclaw3_ui/src/components/TopBar.tsx:167-172`
- **Trigger**: send a message; the turn fails with a retryable `ApiError` — a `503` (`capacity`), or the BFF's `502` mapped to `network`. `sendMessage` raises `setBanner({ kind: 'error', text, action: 'retry' })`. Click Retry.
- **Consequence**: `onRetry` clears the banner and bumps `rehydrateNonce`, which only re-runs `useRemoteTranscript`. That effect returns immediately here (`messageCount > 0`, and `sessionOrigin` is `'local'`), so **nothing is re-sent and nothing is re-read**. The turn is not retried. The chemist's typed message is already gone from the composer (`Composer.submit` calls `setDraft(conversationId, '')` at `Composer.tsx:165` before dispatching), so the offered recovery leaves them with a cleared box and a dismissed banner. `TopBar`'s own docstring claims this was fixed — "the two failures most likely to be transient produced a red bar with no way forward, and the message the chemist had just typed was already gone from the composer" — which the code contradicts: only the *transcript-read* failure raised in `App.tsx:84` is actually served by this handler.
- **Evidence**:

  ```ts
  // src/App.tsx
  119  // What the banner's Retry does: clear it and let the transcript read run again.
  120  const onRetry = useCallback(() => {
  121    useChatStore.getState().setBanner(null);
  122    setRehydrateNonce((n) => n + 1);
  123  }, []);
  ```

  Reproduction (`sendMessage` against a stub answering `503` on `POST /sessions/{id}/messages`, then the real `AppShell`/`TopBar` rendered and the Retry button clicked):

  ```
  BANNER AFTER 503: {"kind":"error","text":"at capacity","action":"retry"}
  POSTS TO /messages BEFORE RETRY: 1
  POSTS TO /messages AFTER CLICKING RETRY: 1

  AssertionError: expected 1 to be 2
  ```

- **Fix**: give the banner a retry that means the turn. Either carry the failed send on the banner (`action: 'retry'` plus the `{conversationId, text, dryRun}` it needs) and have `TopBar` call `sendMessage` again — the assistant message is already in the transcript, so it should be replaced rather than appended — or, at minimum, restore the message into the draft on failure (`setDraft(conversationId, text)` in `sendMessage`'s catch) so the chemist can resend by hand. Two distinct `retry` actions sharing one name and one handler is the root cause; splitting them (`'retry_turn'` vs `'retry_transcript'`) makes the miswiring impossible.

---

## localStorage keeps the 30 most recently *created* conversations, not the 30 most recently used

- **Severity**: medium
- **Location**: `/workspace/chemclaw3_ui/src/state/chatStore.ts:777-820` (`partialize`), line 780 `state.order.slice(0, MAX_CONVERSATIONS)`; `order` is only ever prepended-on-create (`:413`) or appended-by-server-merge (`Sidebar.tsx`, `order: [...s.order, ...ids]`) and is **never** re-ordered on use.
- **Trigger**: hold more than 30 conversations. Use one that was created early — send a message into it and select it. Reload.
- **Consequence**: `order` is creation order, so the long-lived conversation sits past index 29 and `partialize` drops it from localStorage on the very write that its own message triggered. Its transcript is gone after a reload, permanently — the local id owns the transcript (`state/types.ts:1-9`), so there is nothing on the server to recover. Worse, `activeId` is persisted **unfiltered** (`:816`), so the reloaded store points at a conversation that no longer exists, and `/c/<id>` lands on the not-found panel. That panel's own copy is the contradicted claim: *"This app also keeps the 30 most recent, so an older one may have been trimmed"* (`routes.tsx:115`) — it keeps the 30 most recently created, and the one the reader used five seconds ago can be the one that went.
- **Evidence**: reproduction — create 31 conversations, then send a message into the first-created one and select it, then read `localStorage['chemclaw3.chat.v2']`:

  ```
  order.indexOf(oldest) = 30 of 31
  oldest persisted? false
  persisted activeId === oldest? true
  persisted conversation count: 30

  AssertionError: expected undefined to be truthy
  ```

  Note `migrateV1toV2` already knows the correct predicate for the other direction (`activeId: state.activeId && conversations[state.activeId] ? … : (order[0] ?? null)`, `:80`) — `partialize` does not apply it.
- **Fix**: trim by `updatedAt`, not by position in `order`, and never persist an `activeId` that the trim just removed:

  ```ts
  const keep = new Set(
    [...state.order].sort((a, b) =>
      (state.conversations[b]?.updatedAt ?? 0) - (state.conversations[a]?.updatedAt ?? 0),
    ).slice(0, MAX_CONVERSATIONS),
  );
  const order = state.order.filter((id) => keep.has(id));
  ...
  activeId: state.activeId && conversations[state.activeId] ? state.activeId : (order[0] ?? null),
  ```

  The sidebar already sorts by `updatedAt` for display, so this only makes the persisted set agree with what the reader sees.

---

## `evidence_source` events are decoded and then silently discarded

- **Severity**: medium
- **Location**: `/workspace/chemclaw3_ui/src/state/chatStore.ts:253-307` (`traceEntryFor` has no `evidence_source` case, so it hits `default: return null`) and `:644-645` (`const entry = traceEntryFor(event); if (!entry) return;`)
- **Trigger**: any turn in which `gather_evidence` runs. The backend emits one event per source per sweep — `retrieval/fanout.py:127` `writer({"evidence_source": name, "chunks": found})`, converted in `api/graph_stream.py:218-221`.
- **Consequence**: the event survives `normalizeEvent` (it is in `EVENT_TYPES` and has a coercion branch) and is then dropped on the floor by the store. `TraceKind` (`state/types.ts:16-26`) has no member for it and nothing in `src/` reads it — `grep -rn "evidence_source" src/` returns nothing. So the exact confusion the backend added it to remove is reproduced in the UI: in the merged evidence list a source that returned **0 chunks** and a source that was **never asked** remain indistinguishable to the reader. `shared/events.ts:14-31` states this rule for itself six times over ("**`EVENT_TYPES` is the gate**… Adding an interface to the union without adding its discriminator here changes nothing at runtime") and the same class of omission has simply moved one file downstream — the discriminator was added, the consumer was not.
- **Evidence**:

  ```
  NORMALIZED: {"type":"evidence_source","source":"graph","chunks":0}
  TRACE AFTER evidence_source: []
  ```

  (real `useChatStore.applyEvent`, real `normalizeEvent`; the assistant message's `trace` is empty afterwards.)

  The only place the string appears outside `shared/events.ts` is `tests/eventContract.test.ts`, which asserts the *decode* and nothing about what happens next — which is why this is green today.
- **Fix**: add `'evidence_source'` to `TraceKind` and a case to `traceEntryFor` carrying `{ source, chunks }`, plus a `TracePanel` row ("`graph` — 0 chunks" / "`n` chunks"). A zero-chunk row is the point of the event and must render, not be filtered out. The generalisable repair is to make the drop detectable: give `traceEntryFor` an exhaustive `switch` over `ChemclawEventType` with a `never` check, so a new member of the union that no branch handles fails `tsc` instead of vanishing at runtime.

---

## What I did not find

No defect in the SSE framing or in `streamTurn`'s error mapping; `eventsource-parser` handles the split-frame case and the malformed-frame tolerance is scoped to one frame. The grounding arithmetic in `chem/provenance.ts` (`writtenTolerance`, `isGroundedFigure`, `figuresIn`) checks out against its own docstrings — the `1e±6` scale factors widen "grounded" a lot but cannot produce a false *unmatched* mark, which is the direction that would matter. `closeToolCall`'s oldest-open-row pairing is genuinely unfixable from the wire (no call id on `tool_result`) and is disclosed as such. `ensureSession`/`setSessionIdIfAbsent` is a correct compare-and-set and I could not race it into minting two sessions.
