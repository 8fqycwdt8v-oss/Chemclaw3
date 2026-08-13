# Full-stack e2e — Chemclaw3 + Chemclaw3-mcp + Chemclaw3_mock + Chemclaw3_ui

First run of `infra/live/e2e-full-stack/`, the harness closing the gap `tasks/todo.md` used to
name: no script tied this backend, the Chemclaw3-mcp tool fleet, the Chemclaw3_mock HPC/ELN/vendor
mock, and Chemclaw3_ui together for an end-to-end pass. Driven from a real headless browser
(Playwright) against a real Anthropic model, a real MCP connector fleet, and the mocked-but-real
HPC/ELN/vendor-tool surfaces — not fixtures on any side of the wire.

Front door `http://127.0.0.1:8000` · `props` `:8850` · `rxnpredict` `:8857` (fake_a/fake_c doubles)
· `mock-hpc-eln` `:8090` · `mock-vendor` `:8091` · UI `:5173`/BFF `:8787` (and a second BFF on
`:4331` for the Playwright suite's own webServer) · Temporal `localhost:7233` · Postgres
`localhost:5432/chemclaw`.

## Headline finding: the chat UI never recovers from a real turn

**Severity: high.** Against this harness's real backend, every layer up through the wire protocol
works — the model reasons, calls tools, and the SSE stream carries a well-formed `answer` event
that closes the connection cleanly — but the browser never shows it. The composer stays disabled
and the message bubble stays on "Thinking… `<elapsed>`" forever, even though the Zustand store
underneath is provably holding the correct, settled state (`composerLock: false`, `streaming:
null`, the message's `status: 'done'` and `finalText` populated). Reproduces on the very first
turn of a very first session, with no external connector involved (a plain "what is the pKa of
acetic acid" question hits it) — this is not specific to the four-repo harness or to a
Chemclaw3-mcp/Chemclaw3_mock tool call, and it does not reproduce against `e2e/fixture-service.mjs`
in the existing `playwright.config.ts` suite.

**What was ruled out**, each with direct evidence:

| Layer | Check | Result |
| --- | --- | --- |
| Backend turn | `curl` directly to `:8000/sessions/{id}/messages` | Completes correctly: `tool_call` → `tool_result` → `token`×N → `answer`, connection closes cleanly, `curl` exit 0 |
| BFF proxy | Same request through the BFF (`:4331` or `:8787`) via `curl` | Identical — 200, correct SSE framing, clean close |
| `streamTurn.ts` (fetch + `EventSourceParserStream`) | Instrumented with tracing | Resolves correctly: detects the `answer` event, breaks the read loop, returns it |
| `sendMessage.ts` orchestration | Instrumented with tracing | `finishTurn`, then `setComposerLock(false)` — confirmed via `useChatStore.getState()` immediately after — then the `finally` block confirms `streaming` cleared to `null` |
| Upstream `http.Agent` keep-alive | Toggled `keepAlive: true` → `false`, rebuilt, retested | No change — ruled out |
| Page visibility / `requestAnimationFrame` throttling | Forced `page.bringToFront()` every second throughout | No change — ruled out |

What was **not** fully isolated: which specific subscription/render path drops the update. The
store update is real (confirmed via direct `getState()` reads at the exact moment); the DOM never
receives it, for both the composer's `disabled` attribute and the message bubble's own
`status`-driven content. The most likely remaining class of cause is a stale-reference issue in how
a message-list/composer component reads its slice of the Zustand store (a `React.memo`/`useMemo`
boundary, or a `persist`-middleware interaction) rather than anything about the network or the
event contract — worth a focused session with React DevTools rather than more black-box tracing.

**Practical effect**: a chemist using this UI against a real deployment cannot have a second turn
in any conversation, and the first turn's own answer never becomes visible either — the product is
unusable end-to-end today, despite `playwright.config.ts`'s fixture-backed suite passing in CI.
That gap is exactly why this harness exists.

No fix was attempted: the evidence narrows the search space but does not name a line, and guessing
at a fix to core chat state-management without being sure would risk landing something worse. Filed
here as the harness's first real catch, for a follow-up session with fresh context and React
DevTools.

## Coverage

The Playwright spec (`e2e/full-stack.spec.ts`, `feat/full-stack-e2e-spec` branch — patch attached,
push access to `Chemclaw3_ui` was not available this session) is a single serial session with 8
scenarios, each pinned to one subsystem:

| # | scenario | subsystem | result |
| --- | --- | --- | --- |
| 1 | shell paints against the real front door | Chemclaw3 front door | **PASS** |
| 2 | solvent question reaches `props` | Chemclaw3-mcp | **FAIL** — blocked by the headline finding |
| 3 | reaction question reaches `rxnpredict` | Chemclaw3-mcp | did not run (blocked by #2) |
| 4 | sourcing question reaches `mock-vendor` | Chemclaw3_mock | did not run |
| 5 | evidence from seeded ELN/ORD data | Chemclaw3_mock | did not run |
| 6 | durable HPC/QM job through the plan gate | Chemclaw3_mock + Temporal | did not run |
| 7 | note proposal reaches the PR-gate review queue | Chemclaw3 | did not run |
| 8 | `/readyz` reports every connector healthy | Chemclaw3 | did not run |

**1/8 ran and passed; 1/8 ran and failed on the headline finding; 6/8 did not run** (the suite is
deliberately serial and single-session — see the spec's own docstring for why racing real-model
turns isn't worth it). Scenario 8's check (`/readyz` names every connector) was independently
confirmed true by hand — see below — the suite just never reached it.

### What was independently confirmed true, outside the blocked UI suite

Every one of these was checked directly against the running stack (`curl`, the Temporal CLI, or a
one-off script), specifically *because* the UI suite couldn't reach them:

- **Full four-repo stack comes up clean.** `infra/live/e2e-full-stack/up.sh up` brings up Postgres/
  Temporal, `props`, `rxnpredict` (with `fake_a`/`fake_c` doubles — no GPU, no checkpoint download),
  `mock-hpc-eln`, `mock-vendor`, this repo's own connectors/workers/front door, and the UI's BFF+SPA,
  each readiness-polled. `/readyz` lists `props=healthy, rxnpredict=healthy, mock-vendor=unprobed`
  (no REST health route on the vendor tool — expected, see the manifest) alongside every one of this
  repo's own bundles, and `chemclaw_connectors_unhealthy` reads `0`.
- **`props` (Chemclaw3-mcp) is really reached, not recalled from memory.** A direct turn asking for
  2-MeTHF's flash point and Hansen parameters produces `tool_call`/`tool_result` events and an
  answer citing `process-solvents v0.1.0` — the vendored dataset's own name.
- **`mock-vendor` (Chemclaw3_mock) manifest is correctly wired**, once its `health_url` bug (see
  Bugs found) was fixed — reachable, `unprobed` (correctly, since it exposes no REST health route),
  never `unreachable`.
- **A killed connector degrades a turn honestly rather than crashing the service** (chaos round,
  below).
- **A durable Temporal job's HPC poll survives a transient failure via retry**, and reports a real,
  legible failure — not a silent hang — when the underlying mock state is actually gone (chaos
  round, below).

## Chaos round

Driven directly against the API/Temporal layer rather than through the UI, since the headline
finding blocks any UI-driven turn from completing visibly. Both scenarios used
`infra/live/e2e-full-stack/up.sh restart <name>` — the same primitive a UI-driven chaos pass would
have used.

### A · kill and restart `props` mid-session

| check | result |
| --- | --- |
| `/readyz` reflects the kill within one probe | **PASS** — `props: healthy → unreachable`, `chemclaw_connectors_unhealthy: 0 → 1` |
| A turn asking a `props`-only question does not crash the service | **PASS** — the front door stayed up throughout |
| The turn degrades honestly rather than hallucinating an answer | **PASS** — the stream carried a `capability_degraded` event and the model's own answer said outright it has no tool for the question, rather than inventing a flash point |
| Restarting `props` restores health | **PASS** — `/readyz` re-probes live (no restart of the front door needed) and reports `healthy` again within ~2s |
| A `props` question after recovery actually reaches the tool again | **PASS** — `tool_call`/`tool_result` events return, answer cites the correct vendored value (−11 °C) |

**5/5 checks passed.**

### B · kill and restart the HPC mock mid-poll on a running durable QM job

A `QMJobWorkflow` was launched directly against Temporal (bypassing the plan-gate/LLM path, which
the headline finding blocks) with `MOCK_HPC_POLLS_UNTIL_DONE=6` for a wider kill window, then
`mock-hpc-eln` was killed with `kill -9` mid-poll.

| check | result |
| --- | --- |
| The Temporal activity retries transient poll failures rather than failing the workflow immediately | **PASS** — the worker logged `transient poll error (1..N consecutive)` and kept polling, no crash |
| The activity's own retry policy re-attempts after enough consecutive failures | **PASS** — observed `attempt: 2` after the transient-error streak, per Temporal's activity retry policy |
| The job completes once the dependency recovers | **Not reached** — `Chemclaw3_mock`'s HPC store is in-process/in-memory (by its own design: "no real compute... no database"), so restarting it forgets the launched job entirely. The poll then legitimately, permanently 404s ("unknown workflow id") rather than recovering — a different, harder failure than the transient blip the check was aimed at |

**2/3 checks passed**, and the one that didn't is a mock-fidelity gap rather than a Chemclaw3 defect
— worth noting for whoever next reaches for this specific scenario (a mid-poll HPC *restart*, as
opposed to a network blip) that the mock cannot currently model it faithfully. The stuck workflow
was terminated cleanly via `temporal workflow terminate` rather than left running.

## Bugs found and fixed

Four real, verified bugs surfaced by actually running the harness rather than reasoning about it —
each fixed, each with its own test, each isolated to its own repo/commit:

1. **`infra/live/e2e-full-stack/up.sh`'s `log()` wrote to stdout** (Chemclaw3, this repo). Corrupted
   `mock_venv_bin()`'s captured return value — the ANSI-coded log line concatenated into the python
   interpreter path — producing an unparseable `exec` target and killing `mock-vendor` at startup.
   Fixed: `log()` now writes to stderr, matching `die()`.
2. **`manifests/mock-vendor/connector.yaml` declared a `health_url` the server doesn't serve**
   (Chemclaw3, this repo). `app/mcp_tools/vendor_server.py` is a bare `FastMCP(...).run(...)`, not
   `mcp_server_kit.connector_app` (what gives `props`/`rxnpredict` their `/healthz`) — the declared
   URL 404'd and, under `CHEMCLAW_CONNECTORS_REQUIRED=true`, hard-failed the front door's startup on
   a connector that was actually fine. Fixed: dropped `health_url` — `HttpEndpoint`'s own docstring
   names exactly this case as why the field is optional.
3. **`up.sh` never ran database migrations.** `infra/live/bootstrap.sh`'s own last log line says
   "Next: `make db-migrate && make live-up`" — a step this harness's first run missed by hand, and
   it failed exactly the way a missing table fails: every `/sessions` call 500'd with
   `psycopg.errors.UndefinedTable: relation "session_owners" does not exist`. Fixed: `up.sh` now
   runs both migration commands itself, unconditionally (both are idempotent).
4. **`Chemclaw3-mcp`'s `rxnpredict` README documented a feature that didn't work**
   (`Chemclaw3-mcp`, patch attached — push access not available this session).
   `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a` was documented to give "a working tool
   surface with no model weights," but `discover_predictors()` never imported
   `engine/base_doubles.py` in production — only `tests/conftest.py`'s fixture ever registered a
   fake predictor. Setting the env var against a real `uvicorn` process did nothing. Fixed: added
   `register_requested()`, called from `tools.py` right after discovery, which reads the raw
   settings strings and registers exactly the named fakes (never via `"*"`, preserving the module's
   "never registered by accident" guarantee). 7 new tests, 78/78 passing.
5. **`Chemclaw3_mock`'s `mcp` dependency had no upper bound** (`Chemclaw3_mock`, patch attached).
   `mcp>=1.2` let a fresh `pip install -e .` pick up `mcp==2.0.0`, which removed
   `mcp.server.fastmcp` (the module `vendor_server.py` imports `FastMCP` from) with no shim —
   `mock-vendor` failed at import with no code change on this repo's side at all.
   `Chemclaw3-mcp`'s own `mcp-server-kit` already pins `mcp>=1.9,<2` for the same reason. Fixed:
   pinned `mcp>=1.2,<2` here too. 28/28 tests passing after the fix.

## What shipped where

- **Chemclaw3** (this repo, branch `claude/chemclaw3-e2e-test-nxwilu`): the harness
  (`infra/live/e2e-full-stack/`), the `make live-e2e-full-stack*` targets, this report, and the
  `CLAUDE.md` note on where this environment's live/e2e Anthropic credential lives.
- **Chemclaw3_ui**: `e2e/full-stack.spec.ts` + `playwright.full-stack.config.ts` +
  `test:e2e:full-stack` script, as a patch — push access wasn't available this session.
- **Chemclaw3-mcp**: the `rxnpredict` fix + test, as a patch — same reason.
- **Chemclaw3_mock**: the `mcp` version pin, as a patch — same reason.

## Next

- A focused session on the headline finding, with React DevTools attached to a live repro (the
  fastest repro is `up.sh up` plus one `curl -N -X POST .../messages`-shaped turn through a headed
  browser) — the evidence above should save most of the re-diagnosis.
- Once the UI is fixed, re-run `npm run test:e2e:full-stack` — scenarios 2-8 should then actually
  exercise `rxnpredict`, `mock-vendor`, the ELN/ORD evidence path, the durable-job plan-gate flow,
  and the PR-gate queue, none of which got a real pass this time.
- If `Chemclaw3_mock`'s HPC mock ever needs to model a mid-run restart faithfully (not just a
  network blip), its in-memory job store would need to persist across process restarts — currently
  out of scope for what the mock promises ("no database").
