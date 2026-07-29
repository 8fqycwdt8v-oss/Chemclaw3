# D-121 — The front door as a multi-process service: pure-ASGI headers, a durable turn claim, a pool timeout that sheds

**Context.** D-119 pooled Postgres and got the blocking work off the event loop. A 50-user load run
against that branch showed what those fixes did and did not buy: connection churn gone (401 opened
connections for 150 turns became zero — the pool reused what it had), p50 down 30 % and p95 down
50 %, and **throughput unchanged at ~1.18 turns/s from 10 users to 50**. Five times the load, 1.7 %
more work. The serialization point was neither the database nor the offloaded CPU: it was the single
event loop, and the box had four idle CPUs beside it.

The decisive experiment — `--workers 4` — was recorded as failing outright for all 50 users. It did
not fail; **it never ran.** The 4-worker server's own log opens with `ERROR: [Errno 98] Address
already in use`: the previous single-worker service still held the port. The 50 clients' errors are
`status=0, All connection attempts failed` — nothing was listening — and the 44
`RuntimeError("No response returned.")` tracebacks in that log belong to the *previous* process,
being torn down with streams still open. So the recorded conclusion "multi-process does not work at
all" was not measured. What the log does prove is worse in one way and better in another: the
`BaseHTTPMiddleware` defect is real and fires on **one** worker, on every stream that outlives its
server; and nothing at all is known against multi-process from that run.

Three real blockers stood between the branch and a multi-process front door, and they are what this
ADR records.

**1. `BaseHTTPMiddleware` cannot carry an SSE stream.** `_add_security_headers` was one, which runs
the downstream app as a second task and pipes its ASGI messages through a memory object stream. A
request that ends without ever sending a response — a pod draining mid-stream, a client that gave up
waiting for an admission permit, any cancelled handler — reaches `call_next` as a closed stream and
is re-raised as `RuntimeError("No response returned.")`. That is an HTTP 500 with a traceback where
the honest outcome is a closed connection.

Not hypothetical, and not a multi-worker problem at all: the **single-worker** process logged 44 of
them, every one on the SSE turn route, as it was shut down with streams open. That is what a rolling
deploy does to every in-flight conversation.

*Decision:* pure ASGI middleware that wraps only `send` and stamps the headers onto the
`http.response.start` message. The body is never re-tasked and never buffered, so an SSE stream is
byte-for-byte what the route produced.

**2. The per-session turn guard was per-process.** `active_turns` is a Python set in one process's
memory and the shipped chart runs the front door at `minReplicas: 2` — so a double-submit landing on
the other replica **has always been admitted twice**, and the two turns interleaved their messages
into one conversation thread. That is the exact corruption the 409 exists to prevent, and it was
live before any of this work; raising the worker count would have added the same hazard inside a pod.

*Decision:* a turn also takes a **leased row** in `session_turns`, under the same
`session_store="postgres"` gate as session ownership — that switch is precisely the condition under
which two processes share a conversation's durable history and can corrupt it.

A lease, not a lock. An advisory lock and `SELECT … FOR UPDATE` are both connection- or
transaction-scoped, so holding one for a turn means pinning a pooled connection for minutes,
re-creating the starvation this work exists to remove. Claim, refresh and release are one short
statement each: borrow a connection, give it straight back. The claim is a single
`INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <= now()`, so the check and the take cannot be
interleaved, and a process SIGKILLed mid-turn stops blocking its session after one lease rather than
until a restart.

The in-process set is kept and checked first, so the single-worker guarantee is byte-for-byte what
it was — no I/O, no race window, no lease involved. The lease adds the cross-process half, with the
property every lease has: exclusion holds while the holder is scheduled often enough to refresh,
which the front door does three times per lease. A failed refresh is **counted, not swallowed** —
D-107 already taught this branch that a guard which quietly switches itself off is worse than one
that fails loudly.

**3. A pool timeout surfaced as a 500.** The run's 16 HTTP 500s were all `psycopg_pool.PoolTimeout`
at `create_session` → `SessionOwnerStore.record`, and the pool was **never exhausted**: 13 of a
permitted 64 connections, zero opened during the run. Callers waited >10 s for a connection that was
*available* and could not be handed over, because the loop could not schedule the handoff. Raising
`pg_pool_max_size` 16 → 64 changed nothing, which is the whole story — this is the same starvation
that used to appear as a connect timeout, made user-visible because a bounded pool raises where an
unbounded connect eventually succeeded.

*Decision:* one `ConnectionError` handler on the app, not a try/except per route — `chemclaw.db`
already funnels "no database" and "no free connection in time" into that one exception precisely
because no caller can act on the difference, and every route touching durable session state can hit
it. It answers **503** with the admission path's own wording, so a client's back-off behaviour is
identical and a browser learns nothing about the infrastructure. Counted as
`chemclaw_db_unavailable_total`, separate from the admission shed, so "the loop could not schedule a
handoff" is never read as "the LLM endpoint is full".

**Consequences.**

- `CHEMCLAW_SERVICE_UVICORN_WORKERS` still defaults to **1**, but no longer because of the turn
  guard. What remains per-process is *capability*, not correctness of durable history: attachments,
  harness todos and the live `AgentSession`. No ingress can pin a request below the pod, so replicas
  plus Route affinity stay the supported way to use more CPU, and the Route now states that affinity
  explicitly instead of relying on the haproxy router's default. A chart test holds it.
- `infra/sql/018_session_turns.sql` is the new migration.
- The 44 spurious 500s per run disappear, independently of worker count.
- **Verified live, not inferred.** The real front door on `--workers 4` against the live stack
  (Postgres, Temporal, the connector fleet, the stub LLM) served 8 concurrent streaming turns to
  completion with the security headers on every stream, and 6 out of 6 pairs of concurrent turns on
  *one* session answered `[200, 409]`. The same 6 pairs run with `session_store=memory` — where no
  shared claim exists — answered `[200, 404]` or `[404, 404]` every time, which is how we know the
  two requests really did land on different workers and that the 409 came from the durable claim
  rather than from either worker's own `active_turns`.
- **Not claimed here:** that throughput improves much. A smoke check (24 concurrent turns, not the
  load harness) measured 0.92 turns/s on one worker against 1.33 on four — real, and far short of
  4×. On this box it cannot be more: four CPUs are shared with Postgres, Temporal, the background
  worker and, above all, `scripts.connectors_dev`, which serves all six connector bundles from **one
  uvicorn process on one event loop**. In production each bundle is its own Deployment; in the load
  harness it is a single loop that every tool call from every turn passes through, so a
  `--workers 4` run there may simply relocate the ceiling rather than raise it.
- Renumbered from D-120 during the merge: `claude/datasource-seam` had already published D-120
  (per CLAUDE.md, the branch merging second renumbers).
