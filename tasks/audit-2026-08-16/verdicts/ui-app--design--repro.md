# Verdicts — `ui-app--design.md` (round 1), lens: does it actually reproduce?

Scope: findings marked **critical** or **high**. The file contains exactly one — the Retry control.
The other eight are medium/low and are out of scope.

Repo state: `/workspace/chemclaw3_ui` at `1a1f6f0` ("Catch the event contract up: agent
attribution, handoff, evidence_source (#20)"), working tree clean apart from another auditor's
untracked probe (`tests/zz_audit_forge.test.tsx`, unrelated to this finding). No mutation markers in
any file on the path; nothing needed diffing against the pristine copy.

---

## The banner's Retry control does nothing after a failed turn

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (agreeing with the reporter; and the reachability is *wider*
  than the finding states — see below)

### What I did

I did not run the reporter's script (it is gone from the tree anyway). I wrote my own probe that
drives the **whole real user flow** rather than seeding a banner: render `AppShell` inside
`AuthGate`, type into the actual composer `textbox`, click the actual **Send** button, let
`sendMessage` run for real against a stubbed `fetch`, then click the actual **Retry** button that
appears in the banner and count every request issued afterwards.

`/workspace/chemclaw3_ui/tests/zz_repro_retry_verify.test.tsx` (removed after the run), executed
with `npx vitest run tests/zz_repro_retry_verify.test.tsx`. Two cases.

**Case 1 — HTTP failure on the turn (`POST /sessions/{id}/messages` → 503):**

```
BANNER: {"kind":"error","text":"The service is at capacity. Retry shortly.","action":"retry"}
DRAFT AFTER FAILURE: ""
MESSAGES: 2
sessionOrigin: local
REQUESTS AFTER RETRY CLICK: []
BANNER AFTER RETRY: null
MESSAGES AFTER RETRY: 2
DRAFT AFTER RETRY: ""
```

**Case 2 — in-stream `error` event the service marks retryable (`code: 'llm_timeout',
retryable: true`, delivered as real SSE frames through `streamTurn`):**

```
STREAM BANNER: {"kind":"error","text":"The turn could not be completed due to an internal error. (reference abc)","action":"retry"}
STREAM REQUESTS AFTER RETRY: []
STREAM BANNER AFTER: null
```

Clicking Retry issued **zero** requests of any kind in both cases. The banner vanished, the
transcript was unchanged (the errored assistant bubble still there), and the composer draft stayed
empty — `Composer.submit` (`src/components/Composer.tsx:162-167`) clears the draft before calling
`sendMessage`, so the typed text is already unrecoverable when the button is pressed.

I also re-derived the mechanism by reading, and re-checked every cited symbol and line:

- `src/state/types.ts:213` — `action?: 'reauth' | 'reset' | 'retry'` (finding says 210; three-line
  drift, the symbol is real and current).
- `src/state/sendMessage.ts:258-268` — the producer, exactly the quoted ternary chain; `retryable`
  → `'retry'`.
- `src/App.tsx:67` — verbatim
  `if (!ready || !conversationId || !sessionId || messageCount > 0 || !fromServer) return;`
- `src/App.tsx:120-123` — `onRetry = () => { setBanner(null); setRehydrateNonce(n => n + 1) }`,
  passed to `<TopBar onRetry={onRetry} />` at `App.tsx:132`.
- `src/components/TopBar.tsx:167-171` — the only consumer of `action === 'retry'`; the button's
  `onClick` is that prop and nothing else.

`grep -rn "Retry\|Try again\|resend" src` returns no other retry affordance anywhere in the UI —
`TopBar.tsx:167` is the single one. So the finding's "the one affordance offered" is literally true;
there is no per-message retry, no composer restore, nothing.

### Why

The chain is unambiguous and executes exactly as the finding describes. After a failed turn the
conversation has `messages.length === 2` (the user bubble plus the failed assistant bubble) and
`sessionOrigin === 'local'`, so **both** `messageCount > 0` and `!fromServer` hold and
`useRemoteTranscript`'s effect returns on its first line no matter how many times the nonce is
bumped. The only observable consequence of pressing Retry is that the banner is dismissed — which is
worse than doing nothing, because dismissal is the visual signal a user reads as "it's handling it".

Two things I found that the reporter did not, and both make it worse rather than better:

1. **The reachability is not limited to HTTP statuses.** The finding's trigger list is 503/500/400
   on the HTTP envelope. The far more common path is an in-stream `error` event: the backend's
   `_classify` (`/home/user/Chemclaw3/src/chemclaw/api/runner.py:103-108`) emits
   `storage_unavailable` and `llm_timeout` with `retryable=True`, and `empty_answer`
   (`runner.py:418`) is emitted with `retryable=True` as well. `errorFromEvent`
   (`src/api/errors.ts:122-143`) passes the service's `retryable` straight through, so all three
   land as `action: 'retry'`. Case 2 above drives exactly that path and the button is equally dead.
   That is the ordinary agent-failure route, not an edge case.
2. **The turn is genuinely retryable at the protocol level**, so this is not a control that was
   correctly refusing to do something unsafe. `sendMessage` already retries internally for
   `session_not_found` and `unauthorized` (`sendMessage.ts:195-215`), and the backend's 503 arms are
   explicitly documented as retryable refusals (`api/routes/turns.py:224`,
   `api/middleware.py:53,74`). The button is not conservative; it is unwired.

Severity: high is right. It is a correctness bug wearing a design label — a user who fails a turn
loses their typed message, is offered the single remedy the product has, presses it, sees the error
disappear, and waits for an answer that no request was ever made for. It is not critical: no wrong
scientific answer, no data corruption beyond the (already-lost) draft, and the failure is recoverable
by retyping.
