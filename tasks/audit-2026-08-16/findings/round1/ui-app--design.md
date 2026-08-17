# ui-app — design and simplification (round 1)

Repo: `/workspace/chemclaw3_ui`, slice `src/`. Lens: structure that costs more than it buys —
duplication, single-caller abstractions, dead code, layering, misleading names, hardcoded config.

Nine findings. One is a reproduced user-visible dead control; the rest are duplication and
dead-weight with concrete, behaviour-preserving refactors. Two hypotheses I chased and could
**not** confirm are recorded at the bottom so nobody re-spends the time.

---

## The banner's Retry control does nothing after a failed turn

- **Severity**: high
- **Location**: `src/state/types.ts:210` (`Banner.action`), `src/state/sendMessage.ts:258-269`
  (producer A), `src/App.tsx:66-98,120-123` (producer B + the handler), `src/components/TopBar.tsx:167-172`
  (the only consumer)
- **Trigger**: any conversation this browser created (`sessionOrigin: 'local'` — i.e. every
  conversation a user starts). Send a message; the service answers `503` (or `500`, or a `400` on
  `POST /sessions`). Press the **Retry** button that appears in the red banner.
- **Consequence**: the banner is dismissed and **nothing else happens**. The turn is not resent, no
  request of any kind is issued, and the message the chemist typed is already gone from the composer
  (`Composer.submit` clears the draft before calling `sendMessage`). The one affordance offered on
  the two most transient failures in the product is inert.
- **Evidence**: `Banner.action` is a single three-value vocabulary (`'reauth' | 'reset' | 'retry'`)
  written by two unrelated producers. `sendMessage` sets `action: 'retry'` meaning *retry the turn*:

  ```ts
  // sendMessage.ts:258-269
  useChatStore.getState().setBanner({ kind: 'error', text,
    action: apiError.kind === 'unauthorized' ? 'reauth'
          : apiError.kind === 'turn_in_flight' || apiError.kind === 'session_not_found' ? 'reset'
          : apiError.retryable ? 'retry' : undefined });
  ```

  `useRemoteTranscript` sets the same value meaning *re-read the transcript* (`App.tsx:78-86`). The
  consumer implements only the second meaning: `TopBar` calls the `onRetry` prop, which is
  `AppShell`'s handler — `setBanner(null); setRehydrateNonce(n + 1)` — and that nonce only feeds an
  effect that returns immediately for a conversation with messages or a local session:

  ```ts
  // App.tsx:67
  if (!ready || !conversationId || !sessionId || messageCount > 0 || !fromServer) return;
  ```

  After a failed turn both `messageCount > 0` (the user bubble and the errored assistant bubble were
  appended) and `!fromServer` hold, so the effect is a no-op.

  Reproduced with a temporary vitest file (since removed) that seeded a local conversation with two
  messages and a `{action:'retry'}` banner, rendered `AppShell` under a stubbed `fetch`, and clicked
  the button:

  ```
  REQUESTS MADE BY CLICKING RETRY: []
  BANNER AFTER RETRY: null
  MESSAGES: 2
  ```

  A companion probe confirmed the producer side really does set this action on ordinary failures:

  ```
  503  -> {"kind":"error","text":"The service is at capacity. Retry shortly.","action":"retry"}
  500  -> {"kind":"error","text":"Internal Server Error","action":"retry"}
  400  -> {"kind":"error","text":"unknown profile 'no-such-profile'","action":"retry"}
  ```

- **Fix**: make the banner carry the action rather than name it. Replace
  `action?: 'reauth' | 'reset' | 'retry'` with `action?: { label: string; run: () => void }` (or, if
  a serialisable banner is wanted, keep the tag and add `onAction`), and have each producer supply
  its own callback: `sendMessage` supplies a closure that re-runs the turn with the same text (it
  already holds `conversationId`, `text`, `dryRun`, `auth`); `useRemoteTranscript` supplies the nonce
  bump. `TopBar` then renders `banner.action.label` and calls `banner.action.run()` and stops knowing
  what a retry is. This is **not** behaviour-preserving — it makes a currently-dead control work — and
  that is the point. The minimal alternative, if resending a turn is judged too costly to do
  automatically, is to have `sendMessage` restore the draft and set no `retry` action at all, so the
  chemist keeps their text and is not offered a button that lies.

---

## Every unmapped HTTP status becomes kind `network`, and therefore "retryable"

- **Severity**: medium
- **Location**: `src/api/errors.ts:100-106` (`errorFromStatus` default arm), `src/api/errors.ts:69`
  (`retryable` default)
- **Trigger**: any status the switch does not name. Concretely: `POST /sessions` with a profile the
  service does not know answers `400` (documented at `src/api/client.ts:236-238`).
- **Consequence**: the error is constructed as `new ApiError('network', detail, 400)`, and because
  `retryable` defaults to `kind === 'capacity' || kind === 'network'`, `retryable` is `true`. A
  permanent client-side rejection is filed under "the service is unreachable" and is offered a Retry.
  A `500` lands in the same bucket. Any call site wanting to tell a transport failure from a server
  error has to string-match on `err.status === undefined`, which is exactly the discrimination the
  typed-error module exists to remove.
- **Evidence**: the probe above; `400` came back with `action: "retry"` and the message
  `unknown profile 'no-such-profile'`. The module docstring says the statuses "each mean something
  specific and each want a different response from the UI, so they are mapped once here rather than
  being re-interpreted at every call site" — the default arm is where that claim stops holding.
- **Fix**: add a `server` kind (5xx, not retryable by default beyond `capacity`) and a `rejected`
  kind (4xx the switch does not name, never retryable), and reserve `network` for the `catch` around
  `fetch` — the one place where there genuinely is no status. Behaviour-preserving for every status
  the switch already names; it changes only the arm that is currently wrong.

---

## The same render-phase fetch is hand-written in four sheets

- **Severity**: medium
- **Location**: `src/components/ResultSheet.tsx:450-465`, `src/components/JobsPanel.tsx:47-68`
  (`JobSheet`), `src/components/ReviewQueue.tsx:68-85` (`ProposalSheet`),
  `src/components/NoteSheet.tsx:86-110`
- **Trigger**: opening any of the four sheets.
- **Consequence**: four copies of a `useState` triple (`loadedFor`, result, error) plus a
  `if (open && loadedFor !== key) { setLoadedFor(key); …; api.X(…).then(…).catch(…) }` block written
  in the component body. Three of the four also hand-roll their own union
  (`{status:'idle'|'loading'|'ready'|'failed'}` in `ResultSheet` and `NoteSheet`, a bare
  `detail | error` pair in `ProposalSheet`, a `status | notice` pair in `JobSheet`), so the same
  loading/failed rendering is expressed four different ways. Starting a request from the render body
  is also a side effect in render, which React does not promise to run once — it happens to be safe
  today (measured below) but nothing in the code says why.
- **Evidence**: the four call sites are textually near-identical apart from the api function and the
  key type (`string` ref, `string` jobId, `number` proposal id, `string` noteId). I measured whether
  the render-phase call double-fires under `StrictMode` (which `src/main.tsx:20` enables): it does
  not — React applies the render-phase `setLoadedFor` before re-invoking, so both the StrictMode and
  the plain render issue exactly **one** `GET`:

  ```
  TOOL-RESULT FETCHES (StrictMode): 1
  TOOL-RESULT FETCHES (plain): 1
  ```

  So this is duplication, not a live bug — but it is duplication with four chances to drift.
- **Fix**: one hook, `useSheetResource<K, T>(key: K | null, load: (k: K) => Promise<T>)`, returning
  `{status: 'idle'|'loading'|'ready'|'failed', data?, message?}` and doing the fetch in a
  `useEffect` keyed on `key` with a `cancelled` flag (the pattern `Composer`, `Holds`, `Proposals`
  and `JobsPanel`'s list already use, so the codebase has both idioms side by side). Four callers is
  well past the Rule of Three. Behaviour-preserving, with one deliberate improvement: an in-flight
  response can no longer land after the sheet has been re-targeted.

---

## Six identical "swallow a 404 into an empty list" blocks, keyed on a kind that names the wrong thing

- **Severity**: medium
- **Location**: `src/api/client.ts` — `listProfiles:250-257`, `listSessions:261-268`,
  `getMessages:272-279`, `listApprovals:365-372`, `listProposals:400-405`, `listJobs:447-452`
- **Trigger**: any 404 from one of the six list routes.
- **Consequence**: the same five lines appear six times:

  ```ts
  try { return await request<T[]>(path, getToken); }
  catch (err) { if (err instanceof ApiError && err.kind === 'session_not_found') return []; throw err; }
  ```

  and the kind they test names something that is untrue for five of the six. `/profiles`,
  `/approvals`, `/proposals` and `/jobs` are not session-scoped at all; a 404 there means "this
  service has no such route", which is a different fact from "that session handle is dead". A reader
  of `listJobs` has to know that `session_not_found` is being used as a synonym for `route_absent`
  before the code makes sense, and any future 404 that genuinely does mean "the thing you asked for
  is gone" is now indistinguishable from it.
- **Evidence**: the six call sites above; `errorFromStatus:78-79` is the single producer of that
  kind, mapping every 404 to it.
- **Fix**: extract `async function optional<T>(p: Promise<T>, fallback: T): Promise<T>` (or
  `degradeOn404`) in `client.ts` and call it six times: `return optional(request<Job[]>(…), [])`.
  Separately, split the kind: keep `session_not_found` for the session routes and add `not_found` for
  the rest, so the name states the fact. Behaviour-preserving.

---

## `TopBar` re-implements `api.health`, which is dead, on a justification the code contradicts

- **Severity**: low
- **Location**: `src/components/TopBar.tsx:203-211` (`api_health`), `src/api/client.ts:223-230`
  (`api.health`)
- **Trigger**: none needed — this is static.
- **Consequence**: two implementations of "GET /healthz and return res.ok", one of which
  (`api.health`) has zero callers anywhere in the repo. The local copy carries a comment claiming a
  reason that does not exist:

  ```ts
  /** Kept local so the health poll cannot accidentally acquire a token on every tick. */
  async function api_health(): Promise<boolean> {
  ```

  `api.health` already passes `async () => null` as its token getter (`client.ts:225`), so it cannot
  acquire a token either. The comment is a claim about a hazard the code it replaces does not have.
  The name is also the only `snake_case` function in `src/`.
- **Evidence**: `grep -rn "api\.health" src tests e2e server shared scripts` → no hits.
- **Fix**: delete `api_health` from `TopBar`, call `api.health()`, and delete the comment. Or delete
  `api.health` and rename the local one `pollHealth`. Either way one of the two goes. Behaviour-preserving.

---

## `lib/cn.ts` is a migration shim that outlived its own deletion condition

- **Severity**: low
- **Location**: `src/lib/cn.ts` (whole file); importers `src/components/TracePanel.tsx:36`,
  `src/components/Molecule.tsx:45`, `src/components/JobFeed.tsx:28`,
  `src/components/CitationChip.tsx:20`
- **Trigger**: static.
- **Consequence**: two module paths export the same `cn`. Seventeen files import `@/lib/utils`, four
  import `../lib/cn.ts`. A reader grepping for `cn`'s definition finds two answers, and the shim's own
  docstring states the exit condition it has not met: *"delete it once they have all moved to
  `@/lib/utils`."*
- **Evidence**: `grep -rn "lib/cn" src` → four component imports plus the shim's own docstring.
- **Fix**: rewrite the four imports to `import { cn } from '@/lib/utils';` and delete
  `src/lib/cn.ts`. Behaviour-preserving (`lib/cn.ts` is a bare re-export).

---

## Dead exports, including one the file's own rule forbids

- **Severity**: low
- **Location**: `src/components/ui/misc.tsx:27` (`Skeleton`), `src/env.ts:111` (`isDevAuth`),
  `src/api/client.ts:223` (`api.health`, also covered above)
- **Trigger**: static.
- **Consequence**: `misc.tsx`'s docstring states the rule it is breaking three lines later:

  > Kept deliberately short of a component library: a primitive earns its place here by having a
  > caller. Popover, Separator and a full Dialog were written during the rebuild, went unused, and
  > were deleted rather than left as furniture.

  `Skeleton` has no caller and is furniture. `isDevAuth` is an exported predicate with no caller; the
  `auth.mode === 'dev'` check it wraps is written inline at `TopBar.tsx:107,141` and
  `AuthContext.tsx:102`, so it is also a single-purpose helper with zero adopters.
- **Evidence**: a whole-repo unused-export scan over `src tests e2e server shared scripts` (script at
  `/tmp/deadexports.mjs`) plus direct greps:
  `grep -rn "Skeleton\|isDevAuth\|api\.health" src tests e2e server shared scripts` returns only the
  definitions and `misc.tsx`'s own docstring mention.
- **Fix**: delete all three (and the `Skeleton` mention in `misc.tsx`'s docstring). Behaviour-preserving.

---

## `citations.ts`: the `report` alternative in the job pattern is unreachable, and `kindOf` resets a `lastIndex` it never uses

- **Severity**: low
- **Location**: `src/lib/citations.ts:45-75` (`NOTE_PREFIXES`, `PATTERNS`, `kindOf`)
- **Trigger**: an answer containing `report-abcd1234`.
- **Consequence**: `PATTERNS` declares a `job` regex `\b(?:qm|calc|bo|report)-[A-Za-z0-9]{4,64}\b`,
  but `report` is also in `NOTE_PREFIXES`, and `kindOf` tests the `note` pattern first — whose tail
  `[A-Za-z0-9][A-Za-z0-9_.-]*` is strictly looser. So no token can ever resolve to `kind: 'job'` via
  the `report` alternative; the chip always gets the note tone
  (`CitationChip.PALETTE`), which is the exact distinction that component's docstring says a reader
  needs ("a note resolves in the graph, a job may have no note at all"). Separately, `kindOf`'s
  `re.lastIndex = 0` (line 71) mutates a shared module-level regex and then ignores it — the next
  line constructs a fresh `new RegExp(...)` and tests that instead.
- **Evidence**: driven through the real plugin in a temporary vitest file (since removed):

  ```
  report-abcd1234 -> [ '#cite/note/report-abcd1234' ]
  qm-abcd1234     -> [ '#cite/job/qm-abcd1234' ]
  calc-abcd1234   -> [ '#cite/job/calc-abcd1234' ]
  bo-abcd1234     -> [ '#cite/job/bo-abcd1234' ]
  bo-candidate-7  -> [ '#cite/note/bo-candidate-7' ]
  ```

- **Fix**: drop `report` from the `job` pattern's alternation (it is genuinely ambiguous with the
  note prefix and the note reading is the one that wins today, so removing it makes the code state
  what it does), and delete the `re.lastIndex = 0` line. Behaviour-preserving.

---

## `MAX_JOB_STREAMS` hardcodes a backend limit that is an ENV setting, in a UI that already has a runtime-config seam

- **Severity**: low
- **Location**: `src/hooks/useJobStreams.ts:48`; the seam it should use is
  `src/env.ts:14-43` (`RuntimeConfig`)
- **Trigger**: a deployment that sets `CHEMCLAW_SERVICE_MAX_EVENT_STREAMS_PER_USER` below 3.
- **Consequence**: the client budget is a compile-time `3`, justified in its docstring against the
  backend's *default* of 5. That default is a `Field(default=5, gt=0)` setting
  (`/home/user/Chemclaw3/src/chemclaw/core/config/service.py:223`), read at
  `api/routes/streams.py:85`. On a deployment that lowers it to 1 or 2, every page load opens three
  streams, two 429 in a row, `setJobStreamsThrottled(true)` latches for the life of the page, the
  sidebar shows "Watching fewer conversations for finished jobs" permanently, and the feature runs at
  one stream forever — with no way to correct it short of a client rebuild.
- **Evidence**: `useJobStreams.ts:59-74` reads `MAX_JOB_STREAMS` directly; `openStream:117-122` is the
  latch; `Sidebar.tsx:334-340` is the permanent warning. `env.ts:21-29` documents `warmSessions` as
  runtime-switchable for exactly this reason — *"Runtime-switchable because it changes a backend
  resource pattern… If that turns out to matter, this can be turned off without a client rebuild"* —
  and this constant changes the same backend resource pattern harder.
- **Fix**: add `maxJobStreams: number` to `RuntimeConfig` (default 3, resolved the same way
  `warmSessions` is) and read `config.maxJobStreams` in the hook. Behaviour-preserving on a
  deployment that sets nothing.

---

## Checked and NOT findings

Recorded so the next reviewer does not repeat the work.

- **Render-phase fetch double-firing under `StrictMode`.** Hypothesised; measured false. React applies
  the render-phase `setLoadedFor` before the second StrictMode invocation, so exactly one request is
  issued either way (numbers in the third finding above). The duplication stands; the bug does not.
- **`combined` in `citations.ts` leaking `lastIndex` between calls.** The module-level `/…/g` regex is
  reset on both sides of the `test()` (lines 90-92) and `String.prototype.matchAll` iterates a clone,
  so no cross-call bleed is reachable. Fragile, but not a defect — the only genuinely dead line there
  is `kindOf`'s, reported above.
