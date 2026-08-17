# Round 1 — chemclaw3_ui, `server/` + `shared/events.ts` — design & simplification

Repo: `/workspace/chemclaw3_ui`. Files read in full: `server/index.ts`, `server/proxy.ts`,
`server/routes.ts`, `server/config.ts`, `server/runtimeConfig.ts`, `server/log.ts`,
`shared/events.ts`. Supporting reads to check claims: `tests/routes.test.ts`,
`tests/serverConfig.test.ts`, `scripts/check-openapi.mjs`, `scripts/build-server.mjs`,
`src/env.ts`, `src/components/chem/toolIcons.tsx`, `src/lib/format.ts`, `.github/workflows/ci.yml`,
`tsconfig.json`.

This is a small, careful, well-argued codebase and most of what looks like ceremony turns out to be
paid for. Seven findings, none critical, none high. Two of them (1 and 2) are worth doing; the rest
are cheap tidies I would raise in review but not block on.

---

## Every route's `target` closure computes exactly `path.slice('/api'.length)`

- **Severity**: medium
- **Location**: `server/routes.ts:79` (`Route.target`), the 22 `target:` entries at
  `routes.ts:85–219`, consumed at `routes.ts:232` and `scripts/check-openapi.mjs:152`
- **Trigger**: any proxied request. `resolveRoute('GET', '/api/sessions/<sid>/messages')` runs
  `target(m)` to rebuild `/sessions/<sid>/messages` out of capture groups — a string that is already
  sitting in `path`, four characters in.
- **Consequence**: 22 hand-written path reconstructions, each a place a typo silently rewrites the
  upstream URL, buying nothing. The capture groups in `SID`, `APPROVAL`, `NOTE`, `JOB` and
  `RESULT_REF` exist *only* to feed these closures — the whitelist itself needs no captures, since
  every pattern is `^…$`-anchored, so matching already implies the path is exactly what the target
  would rebuild. It also forces `Route` to be an object of three fields where two would do, and
  forces `check-openapi.mjs` into the awkward `match !== null && route.target(match) === path`
  double-check.
- **Evidence**: I ran every route in the table against a concrete path (the same 22 the repo's own
  `tests/routes.test.ts` table uses — its expected column is *itself* the request path minus
  `/api`), and compared `resolveRoute(...).path` with `path.slice('/api'.length)`:

  ```
  $ node --experimental-strip-types /tmp/bffaudit/target.mjs
  ROUTES length = 22 | sample cases = 22
  target() differs from path.slice(4) in 0 of 22 cases; 0 unmatched
  routes exercised: 22 of 22
  ```

  All 22 routes exercised, zero divergence.
- **Fix** (behaviour-preserving, proven over the whole table): delete `target` from the `Route`
  interface and from all 22 entries; make the capture groups non-capturing; and

  ```ts
  export const API_PREFIX = '/api';
  ...
  if (match) return { path: path.slice(API_PREFIX.length), sse: route.sse };
  ```

  `check-openapi.mjs:152` collapses to `` `/api${path}`.match(route.pattern) !== null ``, which is
  the property it was actually testing. The same constant then replaces the other two hardcoded
  copies of the prefix — `server/index.ts:68` (`path.startsWith('/api/')`) and
  `server/runtimeConfig.ts:32` (`apiBase: '/api'`) — which today can drift apart with nothing
  noticing. Note the one behavioural door this closes: with `target` gone, a pattern whose captures
  do not cover its whole path can no longer forward to a URL other than the one it matched.

---

## An event type must be written in three places and the compiler checks only one

- **Severity**: medium
- **Location**: `shared/events.ts:298–337` (`ChemclawEvent` union, `EVENT_TYPES` set) and the
  `switch` at `:444–541`; same shape at `:170–189` (`ErrorCode` union vs `ERROR_CODES` set)
- **Trigger**: add a backend event — write the interface, add it to the union, forget
  `EVENT_TYPES`. `tsc -b`, `eslint` and `vitest` all stay green; `normalizeEvent` returns `null` for
  every frame of that type and the event is dropped in silence. Forget the `switch` case instead
  and the `default: return null` at `:539` does the same.
- **Consequence**: this is the repo's most-repeated defect — the file's own header narrates it six
  times (`capability_degraded`, `tool_failed`, `job_failed`, `evidence_source`, `handoff`, and the
  count that read "ten") and states the rule "**`EVENT_TYPES` is the gate**". A rule stated in prose
  is what you write when the type system is not carrying it. The repo then pays for that twice
  over: `scripts/check-openapi.mjs` contains a ~60-line workaround
  (`unionMembers()` at `:198–207`) that **regex-parses `shared/events.ts` as text** to recover the
  union — its comment says it must, because "the union is a type, erased at runtime, and the runtime
  set it is supposed to agree with (`EVENT_TYPES`) is module-private" — and that script is not in
  CI (`.github/workflows/ci.yml` runs typecheck/lint/format/test/contrast/build; `check:openapi` is
  absent) and exits 1 before it ever reaches the union section when the backend is unreachable
  (`check-openapi.mjs:81–87`). So the only mechanical check on the invariant that has cost six
  production misses needs a live FastAPI service and is run by hand.
- **Evidence**: the erasure is real and the fix is a compile error. I reproduced both halves in
  miniature (`/tmp/bffaudit/tsx/demo.ts`) — a three-member union with only two listed in the table
  and two `case`s in the switch:

  ```
  $ npx tsc --strict --noEmit demo.ts
  demo.ts(9,7): error TS2741: Property 'c' is missing in type '{ a: true; b: true; }' but required
                in type 'Readonly<Record<"a" | "b" | "c", true>>'.
  demo.ts(14,57): error TS2366: Function lacks ending return statement and return type does not
                include 'undefined'.
  $ # with 'c' added to both:
  CLEAN COMPILE with all three listed
  ```

- **Fix** (behaviour-preserving; `Object.hasOwn` matches `Set.has` exactly for these keys):

  ```ts
  const EVENT_TYPES: Readonly<Record<ChemclawEventType, true>> = { queued: true, /* … */ };
  const isEventType = (t: string): t is ChemclawEventType => Object.hasOwn(EVENT_TYPES, t);
  ```

  use `isEventType(type)` at `:442` (it also narrows `type` for the switch), and delete
  `default: return null` at `:539–540` — the declared `ChemclawEvent | null` return type then makes
  a missing `case` a compile error. Do the same for `ERROR_CODES` vs `ErrorCode`. Adding a member to
  the union without the other two edits stops being possible, and `unionMembers()`'s source-regex in
  `check-openapi.mjs` can be deleted along with the "this repo against itself" half of that script.

---

## `KNOWN_TOOLS` documents a consumer that does not exist; the list that has that job is unchecked

- **Severity**: low
- **Location**: `shared/events.ts:339–415` (`KNOWN_TOOLS`, 61 entries, and `KnownTool`);
  the real icon table is `src/components/chem/toolIcons.tsx:30–45` (`TOOL_ICON`)
- **Trigger**: read the docstring — "Tools the agent advertises, **used only to pick an icon/label
  in the trace panel**… which is why it had drifted to 15 of the ~56 the service now registers" —
  then grep for who reads it.
- **Consequence**: nothing picks an icon or a label from `KNOWN_TOOLS`. `TracePanel.tsx` gets its
  icon from `TOOL_ICON`, a *separate* 15-entry `Record<string, LucideIcon>` in another file that is
  keyed on bare `string` and is not connected to `KNOWN_TOOLS` at all, and its label from
  `toolLabel(tool: string)` in `src/lib/format.ts:25`, which is a pure string transform needing no
  list. So the "15 that drifted" in the comment is a different list, in a different file, still 15,
  still unchecked. The 61-entry array's only role is to be the domain of
  `Partial<Record<KnownTool, ToolMethod>>` at `src/chem/provenance.ts:322` — its runtime value has
  no importer anywhere in the repo. The cost is a 70-line list maintained against a backend tool
  fleet for a stated purpose it does not serve.
- **Evidence**:

  ```
  $ grep -rn "KnownTool\b\|KNOWN_TOOLS" --include=*.ts --include=*.tsx .
  ./shared/events.ts:343:export const KNOWN_TOOLS = [
  ./shared/events.ts:415:export type KnownTool = (typeof KNOWN_TOOLS)[number];
  ./src/chem/provenance.ts:30:import type { KnownTool } from '../../shared/events.ts';
  ./src/chem/provenance.ts:322:const TOOL_METHOD: Partial<Record<KnownTool, ToolMethod>> = {
  ```

  (`import type` — the value is never imported.) And the 15 icon keys are all already members, so
  retyping the icon map costs nothing:

  ```
  TOOL_ICON keys: 15 | not in KNOWN_TOOLS: []
  KNOWN_TOOLS size: 61
  ```

- **Fix**: retype `TOOL_ICON` as `Partial<Record<KnownTool, LucideIcon>>` — that makes the docstring
  true and gives the icon map a compile-time tie to the tool list, at the cost of one type
  annotation. Then correct the docstring to say what `KNOWN_TOOLS` is actually for (the domain of
  the provenance method table). Behaviour-preserving.

---

## The BFF's security headers are a parameter of the static handler, so three of its four response paths get none

- **Severity**: low
- **Location**: `server/index.ts:38–49` (`sirv({ setHeaders })`) vs the `/healthz`, `/config.js` and
  `/api/*` branches at `:57–82`
- **Trigger**: start the BFF and request anything that is not a static asset.
- **Consequence**: `content-security-policy`, `x-content-type-options: nosniff`,
  `referrer-policy` and `x-frame-options` are attached inside a sirv option, so they exist only on
  the static branch. `/config.js` — a JavaScript response this process generates itself, from
  operator-controlled strings, and the one the `renderConfigScript` escaping at
  `runtimeConfig.ts:43` exists to protect — ships with no `nosniff`. `/healthz` and every proxied
  API response ship with whatever the branch happens to set. The policy is a property of the
  server, expressed as an argument to one handler.
- **Evidence**: measured against a running BFF (`AUTH_MODE=dev BIND_HOST=127.0.0.1 PORT=8099`):

  ```
  --- /healthz ---        HTTP/1.1 200 OK; content-type: application/json          (no CSP, no nosniff)
  --- /config.js ---      HTTP/1.1 200 OK; content-type: application/javascript…
                          cache-control: no-store                                 (no CSP, no nosniff)
  --- / (static) ---      HTTP/1.1 200 OK; content-security-policy: default-src 'self'; …
                          x-content-type-options: nosniff; referrer-policy: same-origin
  ```

- **Fix**: set the four constant headers once at the top of the `http.createServer` callback,
  before the routing branches; leave only the `index.html` `cache-control` rule in `setHeaders`.
  Behaviour-preserving for static assets, additive for the other three paths. (Take care not to
  overwrite an upstream `content-type` on the proxy branch — these four are not among the headers
  `proxy.ts` copies.)

---

## `cfg.logLevel` has no reader; `log.ts` re-reads the environment behind the config module's back

- **Severity**: low
- **Location**: `server/config.ts:125` + `:166` (`logLevel`), `server/log.ts:6`
  (`process.env.LOG_LEVEL`)
- **Trigger**: assign `cfg.logLevel` — or construct a `BffConfig` with a different one — and
  observe that logging is unaffected.
- **Consequence**: `config.ts`'s opening claim, "BFF configuration, read once from the environment
  at boot", is false for `LOG_LEVEL`: it is read a second time, at a second module scope, by the one
  consumer that needs it. The `BffConfig` field is dead weight that every fixture must nonetheless
  fill — `tests/serverConfig.test.ts:34` sets `logLevel: 'error'` on a field nothing reads.
- **Evidence**:

  ```
  $ grep -rn "logLevel" --include=*.ts .
  ./server/config.ts:125:  logLevel: string;
  ./server/config.ts:166:  logLevel: str('LOG_LEVEL', 'info'),
  ./tests/serverConfig.test.ts:34:  logLevel: 'error',
  ```

- **Fix**: pick one. Either delete `logLevel` from `BffConfig` (and the fixture line), or have
  `log.ts` import `cfg` and read `cfg.logLevel` — there is no import cycle, `config.ts` does not
  import `log.ts`. I would take the second: it keeps the module's stated contract, and the level
  then appears in the one place an operator looks for configuration.

---

## `SESSION_ID_RE` is unused and its docstring names a user that has its own copy

- **Severity**: low
- **Location**: `shared/events.ts:544–546`; the duplicate lives at `server/routes.ts:16`
  (`const SID = '([0-9a-f]{32})'`)
- **Trigger**: read the comment — "The BFF uses this to validate path segments, which also makes
  traversal structurally impossible" — then grep.
- **Consequence**: the BFF does not use it. It has a second, independently maintained copy of the
  same alphabet and length as a regex *fragment*, because the whitelist needs an embeddable group
  and not an anchored regex. Two clone sites for one contract, and a comment asserting a coupling
  that does not exist — which is exactly the kind of claim that makes the next reader tighten one
  copy and believe both moved.
- **Evidence**:

  ```
  $ grep -rn "SESSION_ID_RE" --include=*.ts --include=*.tsx .
  ./shared/events.ts:546:export const SESSION_ID_RE = /^[0-9a-f]{32}$/;
  ```

  Single hit: the declaration.
- **Fix**: export the *fragment* from `shared/events.ts`
  (`export const SESSION_ID_PATTERN = '[0-9a-f]{32}';`) and build both the anchored regex there and
  `routes.ts`'s `SID` from it — or, if no second consumer materialises, delete `SESSION_ID_RE` and
  leave `SID` as the single definition. Either way the docstring stops asserting a link that is not
  in the code. Behaviour-preserving.

---

## `proxy.ts` binds its upstream and agent at module import, so the trust boundary has no unit test

- **Severity**: low
- **Location**: `server/proxy.ts:22–34` (`upstream`, `transport`, `agent` at module scope from
  `cfg`)
- **Trigger**: try to write a test that points the proxy at a local `http.Server`. You cannot,
  without `vi.stubEnv` + `vi.resetModules()` + a dynamic re-import before the first reference — and
  the `Agent` is then also rebuilt per test.
- **Consequence**: `server/proxy.ts` — the file that terminates SSE, forwards `Authorization`
  verbatim, and contains what its own comment calls "the single most important line in the file"
  (the client-disconnect propagation at `:175–177`, which is what releases the backend's per-session
  turn lock) — has no unit test at all. Its only coverage is the Playwright e2e suite, which needs a
  browser and a built bundle. The heartbeat's frame-boundary logic at `:61–83`, the 502 path at
  `:179–187` and the connect-timeout at `:156–162` are all pure functions of inputs a fake upstream
  could supply.
- **Evidence**: no test file references `server/proxy`; the only match anywhere is prose in
  `e2e/fixture-service.mjs:10` and `:61`. Contrast `server/config.ts`, where the same problem was
  already solved and the reason written down: `validateConfig(c: BffConfig = cfg)` takes an explicit
  config "precisely so this is testable: `cfg` itself is built from `process.env` at module scope
  and there is only ever one of it per process" (`tests/serverConfig.test.ts:1–10`).
- **Fix**: apply the pattern that module already uses —
  `export function createProxy(c: BffConfig = cfg): (req, res, path, sse) => void`, closing over a
  locally built `upstream`/`transport`/`agent`; `index.ts` calls `const proxy = createProxy()` once
  at boot. Behaviour-preserving (one agent per process, as today), and it makes the disconnect
  propagation and the heartbeat testable against a fake upstream in-process.

---

## Considered and not reported

- **`APPROVAL`, `NOTE` and `JOB` in `routes.ts` are three character-for-character identical
  strings.** Normally a DRY finding; here the file argues explicitly that they are the same set for
  different reasons and should move independently, and the constants are one line each. I agree with
  the file.
- **The `sse: boolean` on each route is redundant with `isEventStream(upstreamRes.headers)`** —
  `proxy.ts:127` requires both, and the content-type check alone is sufficient (a route flagged
  `sse:false` that streamed would simply be handled correctly instead of buffered). Dropping the
  flag would delete a field from 22 entries and a parameter from three modules. I did not raise it
  as a finding because the flag also documents intent at the whitelist, and removing it is the one
  change here that is *not* strictly behaviour-preserving.
- **Two hop-by-hop filter loops** (`proxy.ts:88–96` request, `:122–125` response). Two callers with
  genuinely different bodies; extracting a helper would cost more than it saves.
- **`runtimeConfig()` / `renderConfigScript()` / `serveConfigJs()`** is a three-function chain with
  one caller each and no test importing the middle two. It is three tiny, well-named functions; the
  default-parameter seam is the same testability pattern used in `config.ts`. Not worth a finding.
