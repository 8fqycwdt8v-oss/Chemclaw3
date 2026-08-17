# ui-app — CORRECTNESS, reachability/consequence lens

Scope: the one finding marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/ui-app--correctness.md`. The other three are medium and
out of scope.

---

## The job push-back stream reconnects in an unthrottled tight loop when the response body ends

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. The client mechanism — reproduced, and it is worse than reported.**

Wrote `tests/audit_verify/loop.test.tsx` in `/workspace/chemclaw3_ui` (real `useJobStreams` inside
the real `AuthGate`, real `chatStore`, `fetch` stubbed per case, counter capped at 5000, deleted
after the run) and ran each case under the repo's own vitest/happy-dom:

```
$ npx vitest run tests/audit_verify/loop.test.tsx -t "A1"
A1 clean-EOF n=1: 5001 fetches in 1000ms
A2 clean-EOF n=3: 5003 fetches in 1000ms
A3 json-200  n=1: 5001 fetches in 1000ms
B1 errored-body n=1: 1 fetches in 1000ms
B2 503          n=1: 1 fetches in 1000ms
```

A1/A2/A3 hit my 5000 cap inside the 1 s window — an order of magnitude above the reporter's
"50 in 250 ms / ~600 req/s". My first, uncapped run of the whole file was killed at the 2-minute
command timeout: uncapped, the loop starves the event loop so the test's own `setTimeout` never
fires. B1/B2 confirm the two branches that *do* back off (`catch` and `!res.ok`) work: **1** fetch
per second.

So the mechanism at `useJobStreams.ts:131`/`:140` is exactly as described, and the missing
`content-type` check (A3) is real. That half I confirm.

**2. Reachability of the stated trigger — refuted by measurement.**

The finding's headline trigger is "a Postgres outage or an exhausted pool terminates every stream at
connect time" via `stream_new_events`. I built a faithful stand-in on the repo's own installed
libraries (`/tmp/vb/app.py`: FastAPI + `sse_starlette.EventSourceResponse` + uvicorn from
`/home/user/Chemclaw3/.venv`), with the body generator raising before its first yield — which is
what `claim_unconsumed` does when `db.connection()` fails. The route in
`src/chemclaw/api/routes/streams.py` is the same shape, and the front door's middleware
(`api/middleware.py:138` `_SecurityHeaders`) is pure-ASGI and explicitly non-buffering, so nothing
between the generator and the socket changes this.

Raw wire:

```
$ curl -sv --max-time 8 -H 'accept: text/event-stream' http://127.0.0.1:8912/sessions/abc/events
< HTTP/1.1 200 OK
< content-type: text/event-stream; charset=utf-8
< Transfer-Encoding: chunked
* transfer closed with outstanding read data remaining
```

There is **no terminating zero-length chunk**. Confirmed in uvicorn's source
(`protocols/http/httptools_impl.py:441-443`): on an incomplete response it logs
`"ASGI callable returned without completing response."` and calls `self.transport.close()`.

What a real `fetch` reader sees (`/tmp/vb/probe.mjs`, direct to the backend):

```
status 200 ct text/event-stream; charset=utf-8
RESULT: THREW after 125ms: TypeError terminated  other side closed
```

A **throw**, not a clean `done`. That lands in `openStream`'s `catch` at `:160` — `attempt += 1`,
`backoff(attempt)`. Measured above as B1: 1 request per second, not 5000. The stated trigger takes
the *correct* branch.

I also checked the two other server-side terminations: the generator returning normally is
unreachable (`stream_new_events` runs `while max_polls is None or …` with `max_polls=None`, and
`_events()` never breaks), and sse-starlette's shutdown path cancels the task group without sending
`more_body: False`, so it lands in the same `transport.close()`.

**3. Through the real BFF — also not a loop; a permanent hang.**

Started the repo's actual BFF (`node --experimental-strip-types server/index.ts`,
`CHEMCLAW_API_URL` pointed at the stand-in) and drove `/api/sessions/<32 hex>/events` through it:

```
$ curl -sv --max-time 12 ... http://127.0.0.1:8812/api/sessions/aaaa…/events
< HTTP/1.1 200 OK
< content-type: text/event-stream; charset=utf-8
* Operation timed out after 12002 milliseconds with 0 bytes received

$ timeout 50 node /tmp/vb/probe.mjs http://127.0.0.1:8812/api/sessions/aaaa…/events
status 200 ct text/event-stream; charset=utf-8
probe exit=124        # 50 s, zero chunks, no done, no throw
```

`upstreamRes.pipe(res)` does not end `res` when the source errors, and `upstreamReq.on('error')`
never fired (`grep -i "upstream error" bff.log` → nothing). `attachHeartbeat`'s
`upstreamRes.on('error', stop)` also clears the heartbeat timer, so not even `: hb` is written.

**4. The secondary trigger ("a JSON or HTML 200 from an intermediary") — blocked inside the repo.**

`server/index.ts:68-76` intercepts every `/api/` path *before* the sirv SPA fallback and answers an
un-whitelisted one with `404 {"detail":"not found"}` — `!res.ok`, so backoff. `apiBase` is not
deployment-configurable: `server/runtimeConfig.ts:32` hardcodes `'/api'` and `src/env.ts:78`
defaults to `'/api'`, so no misconfiguration routes the events fetch into the static handler.
`vite.config.ts` proxies `/api` to the BFF rather than to FastAPI, so dev takes the same path.
`deploy/helm/` contains no auth-proxy or nginx sidecar. Producing a 200 non-stream on this URL
therefore needs an intermediary *outside* everything in these four repos.

### Why

Grant the mechanism entirely — I reproduced it and it is worse than reported. What does not hold is
the trigger and, consequently, the severity.

The finding's own reachability argument is a docstring
(`session_events.py:141`, "A connection failure ends the stream (the client reconnects)") read as if
"ends the stream" meant "closes the body cleanly". Measured, it does not: uvicorn tears the socket
down mid-chunked-transfer, `fetch` rejects, and the existing `catch`/`backoff` handles it correctly —
1 req/s, exactly the behaviour the finding says is missing. Through the BFF the same failure produces
the *opposite* symptom: the response never ends at all and the client waits forever. Neither shape is
"600 requests/second against a service that is already unhealthy".

That leaves a genuine latent defect with no demonstrated trigger inside this system: `openStream`
resets `attempt` on `res.ok` rather than on a received frame, has no `content-type` guard (unlike its
sibling `streamTurn.ts:59-64`), and treats a completed body as success. If any intermediary ever
answers this URL with a 200 whose body ends — a captive portal, an SSO page after a redirect, a
buffering ingress — the result is an unbounded hot loop with no jitter, no ceiling and nothing shown
to the user. High consequence, unproven trigger, three-line fix; that composite is medium, not high.
The proposed fix in the finding is right and should still be applied.

### What the reporter missed (worse than filed, different bug)

The BFF hang in step 3 is the defect actually reachable from the stated trigger, and it is arguably
more serious than the one filed: after a backend DB failure the job push-back channel goes silent
**permanently** for every watched session — no reconnect, no error, no user-visible signal — until
the tab is reloaded. `proxy.ts:143` pipes without an error path (`pipe` does not forward source
errors), and `proxy.ts:179-187` only handles errors on `upstreamReq`, which does not fire here. The
fix is an `upstreamRes.on('error'|'aborted')` that calls `res.destroy()` once headers are sent — which
would then *also* route this case into `openStream`'s correct backoff branch.
