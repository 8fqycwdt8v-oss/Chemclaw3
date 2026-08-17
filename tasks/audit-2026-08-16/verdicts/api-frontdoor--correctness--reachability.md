# api front door — CORRECTNESS · refutation pass (lens: reachability & consequence)

Scope: only the **critical/high** findings in
`tasks/audit-2026-08-16/findings/round1/api-frontdoor--correctness.md`. That file has exactly one —
the session-title leak. The other five are medium/low and were not examined.

`src/chemclaw/api/routes/turns.py`, `state.py` and `middleware.py` in the working tree are
byte-identical to the pristine `HEAD` copy (`diff -q` against
`…/scratchpad/pristine`), so nothing below is an artifact of another agent's mutation.

---

## A failed session-title write bricks the conversation with 409 for 605 seconds

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed; the reporter under-stated the trigger surface and
  over-hedged the reachability — see below)

### What I did

Built the real app through `create_app(owner_store=…)` with an owner store that is healthy for
`record`/`lookup` and raises on `set_title_if_absent` only, then drove it with `TestClient`
(`/tmp/repro_title_leak_v.py`, `/tmp/repro_title_leak_v2.py`).

Run 1 — `ConnectionError` (what `core/db.connection` raises for a pool checkout failure,
`db.py:202-205`):

```
turn timeout: 600.0 admission: 5.0 => lease 605.0
session: cfa368db01f645ca93414115c37b7dde
WARNING chemclaw.api.middleware: shedding POST /sessions/…/messages: Postgres unreachable at host=db port=5432: couldn't get a connection after 10.00 sec
first POST -> 503 {'detail': 'server at capacity; retry shortly'}
active_turns after the shed: {'cfa368db01f645ca93414115c37b7dde': 3879.569111202}
gauge: chemclaw_turns_in_flight 1
retry POST  -> 409 {"detail":"a turn is already running for this session"}
```

The retry ran with the store healthy again, so the 409 comes from nothing but the leaked slot.

Run 2 — the same store raising `psycopg.errors.QueryCanceled` instead (what the default
`pg_statement_timeout_seconds` produces on a slow UPDATE, and what a server restart mid-statement
produces; it is *not* a `ConnectionError`, so no handler is registered for it):

```
first POST -> 500 Internal Server Error
active_turns after the shed: {'61db6d9a5bd344f38f75cfb3df8bfa58': 3950.789922642}
gauge: chemclaw_turns_in_flight 1
retry POST  -> 409 {"detail":"a turn is already running for this session"}
```

Config read: `service_turn_timeout_seconds` = 600.0 and `service_turn_admission_timeout_seconds`
= 5.0 (`core/config/service.py:184,106`), so the lease `_claim_turn_slot` writes really is
`now + 605`, and the sweep at `state.py:201-203` only deletes entries whose deadline has *passed*.

### Why

**Mechanism**: as filed. `_claim_turn_slot` writes the lease at `turns.py:75`; the title write is
awaited at `turns.py:87`; the `try:` whose `finally` (`turns.py:234-244`) is the only in-process
cleanup site starts at `turns.py:211`. Anything raised between 75 and 211 leaves the entry behind.

**Reachability — traced from the outermost entry point, and it is worse than the finding says.**

1. *The gate is on in the shipped configuration.* The write is guarded by
   `front.session_owners is not None`, which is `_default_owner_store()` → non-None exactly when
   `session_store="postgres"`. `deploy/helm/chemclaw/values.yaml:341` sets
   `CHEMCLAW_SESSION_STORE: "postgres"`.
2. *Nothing upstream absorbs the failure first.* The one earlier DB touch in this request is
   `CurrentSession` → `_rehydrate_session` → `owners.lookup` (`deps.py:121`), and that runs **only
   on a live-cache miss**. `POST /sessions` puts the session into `live_sessions`
   (`routes/sessions.py:64`), so on the very next `POST /sessions/{id}/messages` the dependency
   short-circuits at `deps.py:106-109` and the title write is the request's *first* database touch.
   There is no pydantic model, validator, gate or startup guard between the HTTP request and this
   line — `MessageIn.message` is an ordinary string and `session_title` is pure.
3. *The trigger is an ordinary transient, not a contrived one.* `db.connection` converts
   `PoolTimeout`/`PoolClosed` into `ConnectionError`, and `middleware._database_unavailable`'s own
   docstring records this exact failure being observed 16 times under load on a sibling route
   (`create_session`) using the same pool. Run 2 shows the family is wider than the finding claims:
   any `psycopg.Error` — a statement timeout, an `AdminShutdown` during failover — leaks the slot
   too, and additionally answers HTTP **500**, whose contract is "do not retry".
4. *The finding's own hedge is discharged by the chart.* It says the 409 only bites "with a single
   front-door replica, or a sticky Route". The shipped Route is sticky **on purpose**:
   `deploy/helm/chemclaw/templates/service-route.yaml:43` sets
   `haproxy.router.openshift.io/disable_cookies: "false"` with a comment explaining that a browser
   must be pinned to one front-door pod because attachments are in-process. So the retry the 503
   body asks for lands back on the pod holding the phantom lease, deterministically. This is the
   one place I would strengthen the finding rather than weaken it.

**Consequence — checked, and it is what is claimed, no paraphrase inflation.**

- 605 s is the real number, not a worst case: the deadline is written unconditionally as
  `now + 600 + 5`, and only expiry clears it. Measured deadline minus the monotonic clock at that
  instant matched.
- The two bodies are real and mutually contradictory to a client: 503 `"server at capacity; retry
  shortly"` then 409 `"a turn is already running for this session"` — a statement that is simply
  false, since no turn ever started (`run_turn` is never reached; the generator is never created).
- `chemclaw_turns_in_flight` reads 1 for the whole window — printed above from `METRICS.render()`.
  The gauge deliberately counts only unexpired leases so it "stays honest" (`app.py` comment), and
  here that honesty is what makes it wrong.
- The durable claim is genuinely *not* leaked (`claims.claim` is at `turns.py:225`, after the write),
  so this is in-process only — the finding states this correctly and does not oversell it.
- Two things the reporter missed, both minor but real: the leaked entry is also the live-cache
  eviction **pin** (`app.py:_turn_in_flight`), so the session is un-evictable for 605 s; and no
  agent lease, permit or heartbeat is involved, so nothing else is held — this is purely a
  refusal, not a resource leak.

**What would have made me refute it**: a caller-side or middleware-level catch that popped the
entry, a non-postgres default in the chart, or a dependency that reaches Postgres before line 75 on
the common path. I looked for all three; none exists. The blast radius is bounded (self-healing,
one pod, one session per occurrence, no corruption, no security or scientific-answer impact, and
`POST /sessions` still works), which is why I would not raise this above high — but a load spike
that produces N pool timeouts refuses N chemists their own conversations for ten minutes each, on
the shipped configuration, so it does not drop below high either.

The proposed fix (move line 86-87 inside the `try:` at 211) is sound: `handed_off` is still False
there, so the existing `finally` pops the slot exactly as it does on the 429 and durable-409 paths.
