# Verdicts — `ui-app--design.md`, reachability/consequence lens

In-scope: findings marked **critical** or **high**. That is exactly one of the nine.
The other eight are medium/low and were not examined.

---

## The banner's Retry control does nothing after a failed turn

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  I did not take the reporter's removed test on trust; I drove the whole user path myself
  (`/workspace/chemclaw3_ui`, HEAD `1a1f6f0`, clean tree, no stray edits) — render `AppShell`
  under `AuthGate` + `MemoryRouter`, **type into the real composer**, **click the real Send
  button**, let the stubbed service answer `503` on `POST /api/sessions/<sid>/messages`, then
  **click the real Retry button** in the banner and count every request the click produced.

  `npx vitest run tests/zzRetryProbe.test.tsx` (temporary, since deleted — `git status` clean):

  ```
  BANNER AFTER FAILED TURN: {"kind":"error","text":"The service is at capacity. Retry shortly.","action":"retry"}
  DRAFT AFTER FAILED TURN: ""
  MESSAGES AFTER FAILED TURN: 2
  SESSION ORIGIN: local
  COMPOSER LOCK: false
  REQUESTS MADE BY CLICKING RETRY: []
  BANNER AFTER RETRY: null
  MESSAGES AFTER RETRY: 2
  ALL CALLS: ["GET /api/healthz","GET /api/sessions","GET /api/profiles",
              "GET /api/sessions/aaaa…/events","POST /api/sessions/aaaa…/messages"]
  ```

  Zero requests from the click. Banner cleared. Draft already empty. Every claim in the finding
  reproduces through the actual DOM, not through a store poke.

  Second probe, on the producer side — `npx vitest run tests/zzRetryProducers.test.ts` (also
  deleted), driving `sendMessage` against three *different* real failure shapes:

  ```
  NETWORK DROP        -> {"kind":"error","text":"Could not reach the Chemclaw service.","action":"retry"}
  500                 -> {"kind":"error","text":"Internal Server Error","action":"retry"}
  IN-STREAM retryable -> {"kind":"error","text":"storage is unavailable","action":"retry"}
  ```

  Backend side, to check the trigger is not hypothetical:
  `grep -rn "503" src/chemclaw/api/routes/*.py src/chemclaw/api/middleware.py` →
  `middleware.py:70` sheds a failed Postgres pool checkout as `503`, `middleware.py:103` sheds an
  unreachable durable subsystem as `503`, `sessions.py:176` sheds profile-load failure as `503`.
  `turns.py:53` confirms admission waits end as an in-stream `error` event, which is the third
  producer above.

  Consumer uniqueness: `grep -rn "onRetry" src/` → three hits, all in `TopBar.tsx`/`App.tsx`;
  `grep -rn "AppShell" src/` → one definition, one importer (`routes.tsx`). There is no second
  shell that passes a different handler. `git log -S "setRehydrateNonce" -- src/App.tsx` → one
  commit; the handler has never done anything else.

  Recovery affordances: `grep -rni "resend|retry" src/components/MessageList.tsx
  src/components/Composer.tsx src/state/chatStore.ts` → **no hits**. There is no per-message
  resend anywhere; the banner button is the only thing the product offers.

- **Why**

  **Reachability — nothing upstream stands in the way, and the trigger is wider than reported.**
  The gate on the button is `banner.action === 'retry' && onRetry`; `AppShell` always supplies
  `onRetry`, so the button always renders when the action is set. The action is set by
  `sendMessage.ts:258-269` whenever the failure is neither `unauthorized` nor `turn_in_flight`
  nor `session_not_found` and `ApiError.retryable` is true. `retryable` defaults to
  `kind === 'capacity' || kind === 'network'` (`errors.ts:69`), and `errorFromStatus`'s default
  arm makes *every* unmapped status `network` — so 503, 500, any other unmapped status, a bare
  `fetch` rejection (`streamTurn.ts:54`) and any in-stream `error` event with `retryable: true`
  all land on it. The reporter listed 503/500/400; the **network drop** is the one that matters
  most and they missed it, because a dropped connection is both the commonest transient failure
  in a chat UI and the case where a user most confidently expects a Retry button to resend.
  On the backend these are not exotic: a Postgres pool checkout failure and an unreachable
  durable subsystem are both deliberately turned into a retryable 503 by
  `chemclaw/api/middleware.py`.

  **The no-op is universal for this producer, not conditional.** The finding attributes it to
  `messageCount > 0 || !fromServer` at `App.tsx:67`. Both hold, and the first holds *by
  construction*: `sendMessage` calls `appendUserMessage` + `startAssistantMessage` before the
  `try`, so by the time any banner exists the conversation has ≥2 messages. There is no
  `sendMessage` failure path that can reach the effect's body — including on a **server-origin**
  conversation restored from another device, where `fromServer` is true but `messageCount > 0`
  still short-circuits. So this is not "inert for local conversations"; it is inert for every
  turn failure, full stop. The reporter's `sessionOrigin: 'local'` framing understates it.

  **Consequence — as stated, and marginally worse.** Clicking Retry issues nothing and also
  `setBanner(null)`, so the click *removes the explanation of the failure from the screen* while
  producing no stream indicator, no spinner and no new message. The user's own reading of that is
  "it accepted my retry"; the honest reading is "the error text is gone and so is the turn". The
  same button on the transcript-rehydrate path (`App.tsx:78-86`, exercised by
  `tests/transcriptRehydrate.test.tsx:197`) *does* work, which is what has kept this alive: the
  control is not always dead, only dead on the path that matters.

  **Where I would soften the wording, without changing the verdict.** Nothing is permanently
  lost. `DRAFT AFTER FAILED TURN: ""` confirms the composer is cleared, but the typed text is
  still on screen in the user bubble (`MESSAGES: 2`), and `COMPOSER LOCK: false` means the user
  can retype or copy-paste and send successfully. So this is a dead recovery control, not data
  loss and not a stuck application. That is the only reason I considered medium.

  I keep **high** because the three things severity should turn on all point that way: the
  trigger is producible by any real deployment without special conditions (capacity shedding is
  designed behaviour here), the affordance is the *only* one the product offers on its most
  common failure, and the failure mode is silent — a user cannot distinguish "retry sent" from
  "nothing happened", and the click destroys the only evidence they had. The finding's own fix
  (banner carries a callback rather than a tag) is the right shape; the fallback it names —
  restore the draft and offer no button — is also acceptable and strictly better than today.
