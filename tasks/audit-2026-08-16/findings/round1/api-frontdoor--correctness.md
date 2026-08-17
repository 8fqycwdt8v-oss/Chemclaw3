# api front door — CORRECTNESS

Slice: `src/chemclaw/api/routes/*.py`, `src/chemclaw/api/app.py`, `src/chemclaw/api/deps.py`,
`src/chemclaw/api/middleware.py`.

All findings below were reproduced by running the real app. Scripts under `/tmp/` (`repro_title_leak.py`,
`repro_readyz.py`, `repro_readyz_timeout.py`, `repro_turn_timeout.py`); the printed output is quoted
in each Evidence block.

---

## A failed session-title write bricks the conversation with 409 for 605 seconds

- **Severity**: high
- **Location**: `src/chemclaw/api/routes/turns.py:75-87` (`post_message`) — the in-process slot is
  taken at line 75, `set_title_if_absent` is awaited at line 87, and the `try:` that owns the
  cleanup does not start until line 211.
- **Trigger**: any exception from `front.session_owners.set_title_if_absent(...)`. The commonest
  real one is `ConnectionError` — which is exactly the failure this module's own
  `_database_unavailable` handler was written for ("under load 16 of those writes raised
  `psycopg_pool.PoolTimeout`"). Sequence: `POST /sessions/{id}/messages` → `_claim_turn_slot`
  writes `active_turns[session_id] = now + 605` → the title write raises → the exception
  propagates out of the handler → the 503 handler answers. Nothing pops the entry.
- **Consequence**: the 503 body says `"server at capacity; retry shortly"`, i.e. it explicitly asks
  the client to retry — and the retry is refused with `409 "a turn is already running for this
  session"` for the full lease (`service_turn_timeout_seconds` 600 + `service_turn_admission_timeout_seconds`
  5 = **605 s**) even though no turn ever started. One transient pool hiccup costs a chemist ten
  minutes of their session. The `chemclaw_turns_in_flight` gauge also counts the phantom turn for
  that whole window. Note the durable claim is *not* leaked (it is taken after this point), so this
  is a pure in-process leak — but with a single front-door replica, or a sticky Route, that is the
  only guard the client meets.
- **Evidence**: the only cleanup site is the `finally` at `turns.py:234-244`, which is guarded by
  `if not handed_off` and belongs to a `try` entered at line 211 — after the title write. Measured
  with an owner store whose `set_title_if_absent` raises `ConnectionError` (everything else
  healthy):

  ```
  first POST  -> 503 {'detail': 'server at capacity; retry shortly'}
  active_turns after the shed: {'a5001d575fa34aa1983ea1682980b13e': 1453.250683141}
  retry POST  -> 409 {"detail":"a turn is already running for this session"}
  ```

  (`/tmp/repro_title_leak.py`; the retry ran against a *healthy* owner store, so nothing but the
  leaked slot refuses it.)
- **Fix**: move the `set_title_if_absent` call inside the existing `try:` at line 211 (it is
  already before the stream handoff, so the ordering the comment cares about — "before the stream,
  so a turn that fails mid-answer still leaves the conversation named" — is preserved). The
  `finally`'s `if not handed_off` branch then pops the slot on this path as it does on the budget
  and durable-claim refusals.

---

## `/readyz` is not bounded by its own probe budget; an unreachable Postgres makes it take 10 s

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/ops.py:76-84` (`_database_reachable`), and the docstring at
  `ops.py:62-68`.
- **Trigger**: `session_store="postgres"` and a Postgres that does not answer at the *transport*
  level (host down, blackholed route, pool exhausted) rather than at the statement level. Any
  `GET /readyz` — unauthenticated by design, and fired by the kubelet every 10 s.
- **Consequence**: the docstring claims the probe is "Bounded by `service_readiness_db_timeout_seconds`,
  its **own** short budget rather than the pool's. That distinction is the whole reason a probe is
  safe here… a probe that reports it in a second is doing its job, and one that **hangs for ten is
  the failure**." The code hands `service_readiness_db_timeout_seconds` to
  `db.connection(statement_timeout_seconds=...)`, which `core/db.py:_merged_options` turns into a
  libpq `-c statement_timeout` **server option**. A server option only applies to statements on an
  *established* connection; it bounds neither the TCP connect (`pg_connect_timeout_seconds`, default
  10) nor the pool checkout (`pg_pool_timeout_seconds`, default 10). So the route hangs for exactly
  the ten seconds its own docstring names as the failure. The chart sets no `timeoutSeconds` on the
  readiness probe (`deploy/helm/chemclaw/templates/deployment-service.yaml:51-56`,
  `_helpers.tpl:160-165` — grep for `timeoutSeconds` in `deploy/` returns nothing), so the kubelet
  default of 1 s applies and the probe is cut off before the handler answers. The second half of the
  docstring — "Well under the kubelet's own probe timeout, so the answer arrives rather than being
  cut off as a timeout whose cause the pod never logs" — is therefore false in the one case the
  probe exists for.
- **Evidence**: measured against a blackholed address (`10.255.255.1:5432`):

  ```
  readiness statement budget : 2.0 s
  libpq connect_timeout      : 10 s
  pool checkout timeout      : 10.0 s
  ConnectionError: Postgres unreachable at ... host=10.255.255.1 port=5432: connection timeout expired
  GET /readyz -> 503 after 10.15s
  ```

  (`/tmp/repro_readyz_timeout.py`.)
- **Fix**: wrap the probe in a wall-clock bound rather than relying on a statement timeout —
  `async with asyncio.timeout(settings.service_readiness_db_timeout_seconds):` around the
  `db.connection(...) / SELECT 1` block, keeping the statement timeout as the inner bound, and add
  `TimeoutError` to the already-caught tuple (it is there). Separately, set an explicit
  `timeoutSeconds` on the chart's readiness probe so the two numbers are stated in one place.

---

## The readiness probe caches are check-then-act, so concurrent callers all miss

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/ops.py:71-88` (`_database_reachable`) and `ops.py:42-52`
  (`_connector_health`) — both read the timestamp, `await` the probe, and only then write the
  timestamp.
- **Trigger**: N concurrent `GET /readyz` requests arriving while the cache window is expired.
  `/readyz` is deliberately unauthenticated (`tests/test_route_auth_coverage.py` pins it into the
  allowlist), so N is chosen by whoever can reach the pod.
- **Consequence**: the cache does nothing for concurrent callers. `_database_reachable`'s docstring
  says it is cached "for the same reason as the connector sweep: this route is unauthenticated by
  necessity and runs every ten seconds per pod, so an uncached probe is a database round trip **any
  caller can trigger at will**" — but a caller who opens 50 connections at once gets 50 round trips,
  i.e. exactly the amplification the cache was added to prevent, multiplied. Combined with the
  finding above, each of those probes occupies a pooled connection for up to 10 s when Postgres is
  unreachable, so an unauthenticated request burst turns a database outage into pool exhaustion for
  the turns that could still be served. `_connector_health` has the same shape and additionally
  fans out to every connector per miss (its docstring concedes the race but only for "two callers");
  it also writes back a `connector_health_at` captured *before* its own await, so a slow sweep that
  finishes second can overwrite a fresh snapshot with an older one and an older timestamp.
- **Evidence**: `db.connection` replaced by a 0.3 s stand-in, `session_store="postgres"`, 20
  concurrent requests:

  ```
  20 concurrent /readyz -> 20 database probes in 1.68s; statuses [200]
  one more /readyz (inside the 5s cache window) -> 0 probes
  ```

  (`/tmp/repro_readyz.py`. The serialized follow-up hits the cache, which is why this is invisible
  to a single-threaded test.)
- **Fix**: collapse concurrent misses onto one probe — keep the in-flight probe as a
  `asyncio.Future`/`Task` on `app.state` and have a caller that finds one await it instead of
  starting its own; write `probed_at` after the probe completes. One shared task per window is the
  behaviour the docstring already claims.

---

## The turn deadline delivers no error event and books no metric when the *client* is what stalls

- **Severity**: medium
- **Location**: `src/chemclaw/api/routes/turns.py:161-199` (`_turn_events`), and the claim at
  `turns.py:148-153`.
- **Trigger**: a client that opens the SSE stream, reads one frame, and then stops reading (a
  backgrounded tab, a wedged proxy, a script that forgets to drain), while the model answers
  normally. The generator is then suspended at the `yield` on line 180 inside
  `async with asyncio.timeout(...)`, with the task parked in the transport's `send`.
- **Consequence**: `asyncio.timeout` cancels the task where it is actually suspended — inside
  `send`, not inside the `async with` — so the generator never resumes and its
  `except TimeoutError:` branch never runs. The stream is torn down instead. Three things the code
  promises do not happen: (a) the documented "the client gets one error event" is not sent; (b)
  `chemclaw_turn_timeouts_total` is not incremented; (c) the `logger.warning("turn timed out …")`
  line is never written. The metric is the only signal an operator has that turns are hitting the
  wall clock, and it is blind to precisely the population the deadline was widened to cover — the
  docstring's own words are "a stalled model stream **and a slow-reading client** are both bounded".
  Only the first of the two is observable.
- **Evidence**: both cases driven at the raw-ASGI level against the real app with
  `service_turn_timeout_seconds = 1.0`:

  ```
  A: model stalls, reader fine  -> turn_timeout event delivered: True   chemclaw_turn_timeouts_total +1
      last frames: ['event: token…"thinking"…', 'event: error…{"type":"error","message":"The turn exceeded the 1']
      active_turns left behind: {}
  B: model fine, reader stalls  -> turn_timeout event delivered: False  chemclaw_turn_timeouts_total +0
      last frames: ['event: token…"tok "…', 'event: token…"tok "…']
      active_turns left behind: {'sess-repro': 1478.517637577}
  ```

  (`/tmp/repro_turn_timeout.py`. Case A is the branch the unit tests exercise; case B is the one the
  docstring claims and the code does not deliver.)

  The trailing `active_turns` entry in case B is a second consequence of the same mechanism, and is
  worth checking against a real transport before acting on: the generator's `finally` — which pops
  the in-process slot, cancels `_hold_turn_claim` and releases the durable claim — can only run when
  the generator is resumed or closed, and both require the consumer task to get past the `send` it is
  parked in. While it is parked, the heartbeat task keeps refreshing the durable `session_turns` row
  every `service_turn_claim_lease_seconds / 3`, so the *durable* 409 has no expiry at all for as long
  as the stalled client holds the socket — the in-process lease expires after 605 s, the durable one
  does not. In this harness `send` blocks forever by construction; under uvicorn a genuinely
  disconnected peer errors out of `send` instead, but a slow peer with a full TCP window does not.
- **Fix**: the honest bound for a stalled consumer is not `asyncio.timeout` around a generator body.
  Either (a) count and log the wall-clock expiry from the `finally` — compare a monotonic start
  stamp against `service_turn_timeout_seconds` there, so the metric and log fire on both paths and
  only the *event* is best-effort; or (b) enforce the deadline outside the generator, in a
  response-level wrapper like `streams._SlotBoundEventStream`, which already exists in this package
  precisely because the generator's scope is shorter than the response's.

---

## Push-back job notifications are claimed destructively in whole batches before delivery

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/streams.py:111-128` (`session_events._events`), consuming
  `chemclaw.agent.session_events.stream_new_events`.
- **Trigger**: a session with K queued `job_completed`/`job_failed` rows; the client opens
  `GET /sessions/{id}/events` and disconnects (or the pod is rolled) after the first frame.
- **Consequence**: `claim_unconsumed` marks **every** unconsumed row of those kinds `consumed_at =
  now()` in one statement and returns them; the route then yields them one at a time. Anything not
  yet flushed to the client is already consumed and will never be re-delivered by any tailer — a
  finished DFT run never wakes the chat that asked for it, and there is no replay route for
  `session_events`. `session_events.py`'s own docstring calls this "at-most-once on a crash in the
  **tiny window** between claim-commit and the event reaching the client"; the window is not tiny —
  it is one whole batch wide, and it is entered by an ordinary client disconnect, not only by a
  crash. The result itself survives in `job_records` (`GET /jobs/{id}`), which is why this is low
  rather than higher, but the chemist is never told to look.
- **Evidence**: `_CLAIM_KINDS` (`agent/session_events.py:55-60`) has no `LIMIT` and updates the whole
  matching set in one `UPDATE … RETURNING`; `stream_new_events` then does
  `for event in await do_claim(): yield event`, so the claim commits before the first yield. The
  route has no acknowledgement step of any kind — there is nothing between `yield` and the socket.
- **Fix**: claim one row at a time (`LIMIT 1` in the inner select) so at most one notification is in
  the lost window, or move the `consumed_at` write to after the yield returns (accepting
  at-least-once, which for a "wake the chat" notification is the cheaper error).

---

## A turn refused with 409 or 429 has already renamed the conversation

- **Severity**: low
- **Location**: `src/chemclaw/api/routes/turns.py:82-87` (`post_message`), and the comment on lines
  83-85.
- **Trigger**: two tabs post different first messages to a fresh session at the same time, landing
  on two front-door replicas; or a session whose budget is already exhausted posts a first message.
- **Consequence**: the inline comment states the invariant "After the turn claim, so a rejected
  double-submit does not write" — but the write at line 87 sits after the *in-process* claim and
  before both the durable claim (line 225) and the budget check (line 217). So the replica that
  loses the durable claim race has already written the title, and a turn refused `429 budget
  exhausted` has too. `set_title_if_absent` is first-write-wins, so the conversation can end up
  permanently named after the message that was rejected rather than the one that ran.
- **Evidence**: line order in `post_message`: `_claim_turn_slot` (75) → `set_title_if_absent` (87) →
  `budget.check` (217) → `claims.claim` (225). The two refusals that produce a status code both run
  strictly after the write the comment says they precede.
- **Fix**: move the title write to after the durable claim succeeds (just past line 230). It is still
  before the stream is handed off, so the other half of the comment holds.

---

## Not findings (checked and clean)

- `deps._owner_authorizes` / `_refuse_unless_owner` — the falsy-owner branch is correct for both
  `None` and `""`, and `Principal.oid` is `Field(min_length=1)`, so the `principal.oid or ""`
  fallbacks in `plan.py:120` and `proposals.py:205` are dead but harmless.
- `deps._rehydrate_session`'s post-await cache re-check is genuinely race-free: there is no `await`
  between the `get` (line 128) and the `add` (line 143).
- `state._claim_turn_slot` really is atomic — no `await` between the membership test and the write —
  and the durable `_TURN_REFRESH` is a plain `UPDATE`, not an upsert, so a late refresh racing a
  release cannot resurrect a claim.
- `streams._release_stream_slot` decrements correctly at every count including 0 and 1, and the
  per-user/per-pod cap comparisons are `>=`, i.e. off-by-one-free.
- `plan.get_plan` / `decide_plan`: `await f() or []` binds as `(await f()) or []` in both places.
