<!-- STATUS: a measurement record, not a design document. Every number here was produced by the
     harness in this repository's history against a live stack; the corrections are kept in place
     rather than edited out, because two of the findings only became visible when an earlier
     conclusion turned out to be wrong. Read top to bottom. -->

# 50-concurrent-user load test — results

Against the live stack: Postgres 16 + pgvector 0.8.1, Temporal dev server, the connector fleet
(6 bundles on :8810), the background worker and all three connector workers, real signed Entra
identities (one per user), `session_store=postgres`, budgets on.

The model is a **stub** OpenAI-compatible endpoint with a fixed 400 ms think-time. That is the
point: with a real model, turn latency is dominated by the provider and every infrastructure limit
hides behind it. Here the model contributes ~1 s per turn (≈2.5 calls × 400 ms), so everything
above that is the service.

The stub had to implement `POST /v1/responses`, not `/v1/chat/completions` — MAF's
`OpenAIChatClient` drives the Responses API. The first run proved it the hard way: 37 admitted
turns, every one failing `openai.NotFoundError: 404`, stub request counter still at zero.

## The numbers

| Run | Users | Admission cap | 200 | 503 | p50 | p95 | max | Throughput |
|---|---|---|---|---|---|---|---|---|
| A | 50 | **8** (shipped default) | 37 | **113** | 10.3 s | 14.1 s | 15.0 s | 1.00 turns/s |
| B | 50 | 64 | **150** | 0 | 37.3 s | 64.2 s | 67.3 s | 1.19 turns/s |
| C | 10 | 64 | 30 | 0 | 7.4 s | 13.1 s | 13.1 s | 1.17 turns/s |

Every run: 3 turns per user, 0 failed turns, 0 budget refusals, 0 session conflicts, and the tool
path genuinely exercised (run B: 150 answers, 900 tokens, **100 tool calls**; the stub logged 379
requests and 152 tool calls).

## Finding 1 — as shipped, 50 concurrent users is 75 % rejection

`service_max_concurrent_turns` defaults to **8**. At 50 users, **113 of 150 turns got 503
"server at capacity"**. That is the configuration behaving exactly as designed — but the default
is 6× below the stated target, and the Helm chart's HPA (min 2 / max 6 replicas) tops out at
6 × 8 = 48 concurrent turns cluster-wide, and only after scaling on **CPU at 70 %**, which
`values.yaml` itself documents as the wrong signal for an LLM-latency-bound service.

## Finding 2 — the service does not scale with concurrency. Throughput is flat at ~1.18 turns/s

This is the important one.

```
10 users → 1.17 turns/s, p50  7.4 s
50 users → 1.19 turns/s, p50 37.3 s
```

Five times the load, **1.7 % more throughput**, and latency up 5×. Raising the admission cap 8×
(8 → 64) bought 19 % throughput and cost 3.6× latency. That is the textbook signature of a hard
serialization point: added concurrency becomes queueing, not work.

The model accounts for ~1 s of a 37 s turn. The other ~36 s is the service. The cause is the one
the code review predicted: a **single uvicorn process on a single event loop** (`entrypoint.sh`
has no `--workers`; Helm limits the pod to `cpu: "1"`), with CPU-bound and blocking work executed
directly on that loop — RDKit parsing and depiction, the knowledge-tree re-parse behind
`gather_evidence`/`find_notes` (a `threading.Lock` with a 5 s TTL and an O(notes) `rglob` per
miss), and retrievers awaited **sequentially** rather than gathered.

Note the box had 4 CPUs available and the service could only use one.

## Finding 3 — Postgres connections time out under load while the database is 72 % idle

32 × `ConnectionError: Postgres unreachable … connection timeout expired` during run B, with
**peak concurrent connections of 28 against `max_connections=100`**. The database was not
saturated. The connect timed out because the event loop could not schedule it inside
`pg_connect_timeout_seconds` (10 s).

Connection **churn**, not count, is what the no-pool design produces: **401 connections opened**
for 150 turns in run B, ~2.7 per turn — and concurrent count is a useless metric here, because
with connect-per-call each connection lives milliseconds. A 2-second sampler of
`pg_stat_activity` read `total=0` on most ticks while the service was opening hundreds; the real
number comes from `pg_stat_database.sessions`.

### What that failure actually disabled

Every one of the 32 was the same call site:

```
service/runner.py:147  →  agents/session_store.py:151 latest_message_id  →  db.connect
```

That is the **rollback watermark** — the durable half of the turn snapshot, added in D-107 to stop
a client disconnect mid-tool-call from leaving an orphaned `tool_use` in `session_messages` and
permanently bricking the session. Its handler is deliberately non-fatal:

```python
except Exception:  # noqa: BLE001 - a rollback aid must never fail the turn it guards
    logger.warning("could not read the history watermark for session %s; a disconnect this "
                   "turn will not roll durable history back", ...)
```

So the turn succeeds and the guard is silently gone. **The protection against a bricked session
fails precisely under the conditions that make a bricked session likely** — a loaded server,
slow turns, and a user who gives up and closes the tab. 32 turns in one 126-second run ran
unguarded, and nothing but a WARNING said so.

## What this means for the plan

Confirms S1 (no connection pool), S3/S4 (single loop, 1 CPU, wrong HPA signal) and S7 (blocking
work on the loop) with measurements rather than inference, and adds a consequence none of them
predicted: the no-pool design does not merely cost latency, it **silently disarms a correctness
guard** through a non-fatal handler.

Order to fix, by measured impact:

1. **Get blocking work off the event loop** and run more than one worker process. This is the
   flat-throughput ceiling; nothing else moves until it does.
2. **Pool the connections.** Removes the churn and, with it, the connect-timeout that disarms the
   watermark.
3. **Make the watermark failure loud** — or better, make it unnecessary by making the read cheap.
   A guard that silently switches off is worse than one that fails the turn.
4. Raise `service_max_concurrent_turns` from a guess to a measured value, and change the HPA
   signal from CPU to in-flight turns.

## Not yet run

The live-Anthropic 50-session run, the multi-replica run (S14: the per-pod `active_turns` guard),
and the chaos scenarios. The stub run had to come first — it is the one that isolates the
infrastructure, and it found the ceiling.

---

# AFTER: the same three runs on `claude/production-scale`

Same harness, same stack, same 400 ms stub think-time. Re-minted tokens (the first attempt
returned 50 × 401 — the originals had aged past their 1 h TTL).

| | BEFORE | AFTER (pool 16) | AFTER (pool 64) |
|---|---|---|---|
| 200 / attempted | 150 / 150 | 105 / 120 | 102 / 118 |
| **500** | **0** | **15** | **16** |
| p50 | 37.3 s | 28.3 s | **26.7 s** |
| p95 | 64.2 s | 34.6 s | **32.2 s** |
| **throughput** | **1.19 turns/s** | 1.10 | **1.17** |
| connections opened | **401** | **13** | **0** (all reused) |
| peak concurrent conns | 28 | 13 | 13 |

## What the pool unambiguously fixed

**Connection churn is gone.** 401 connections opened for 150 turns became **13 for 105 turns**, and
in the third run **zero** — the pool simply reused what it had. That is the S1 finding closed.

**Latency improved ~30 %**: p50 37.3 → 26.7 s, p95 64.2 → 32.2 s. The tail improved most, which is
what removing a per-call handshake from a contended loop should do.

## What it did not fix — and this is the finding

**Throughput did not move: 1.19 → 1.17 turns/s.** This is falsifier #3 from the prediction, and it
fired: *"p50 improves but throughput doesn't → the win was per-turn latency, not parallelism,
meaning the CPU work was never the ceiling."*

So the serialization point is **neither the database nor the blocking calls that were offloaded**.
It is the single event loop itself. Every fix so far made each turn cheaper; none of them let two
turns proceed in parallel.

## The regression the pool introduced

16 × **HTTP 500**, all `psycopg_pool.PoolTimeout: couldn't get a connection after 10.00 sec`, all at
`service/app.py::create_session` → `agents/session_store.py:230 SessionOwnerStore.record`.

Raising `pg_pool_max_size` 16 → 64 did **not** help, and the reason is the whole story: the pool
never grew past 13 connections and opened **zero** new ones during the run. It was not exhausted.
Requests waited >10 s for a connection that was *available*, because the event loop could not
schedule the handoff.

That is the same starvation that produced the BEFORE run's 32 connect-timeouts, wearing a different
name. What changed is the consequence: an unbounded `db.connect` eventually succeeded, so the
symptom was a swallowed warning; a bounded pool raises, and `create_session` has no handler, so it
surfaces as a 500 to the user.

The pool is right and the bound is right. Two things follow, neither of them "make the pool bigger":
1. `create_session` must treat a pool timeout as **retryable (503)**, like admission shedding —
   not a 500.
2. The starvation has to be fixed at its source, or every bounded resource will keep converting
   into user-visible failures.

## The decisive experiment, and why it could not run

If the ceiling is one event loop, four processes should break it. `--workers 4` **fails outright**:

```
File "service/app.py", line 975, in _set_headers
    response: Response = await call_next(request)
starlette/middleware/base.py:169  raise RuntimeError("No response returned.")
```

All 50 users got a transport error. `_add_security_headers` is a `BaseHTTPMiddleware`, which wraps
the response in a way that is incompatible with a long-lived `EventSourceResponse` — it is the known
Starlette limitation, and under multiple workers the timing exposes it every time.

**So multi-process is blocked on more than the per-pod `active_turns` guard.** The scale branch was
right to default `service_uvicorn_workers` to 1, and right about `SCALE-1` — but the blocker is
larger than recorded: the front door needs its security headers moved to pure ASGI middleware
before it can run more than one worker at all.

## Honest conclusion

The branch delivers real, measured wins — churn eliminated, latency down 30 %, the disarmed rollback
guard now countable — and it does **not** deliver more throughput. 50 concurrent users complete, at
~1.2 turns/s and a ~27 s p50 with a 1 s model. That is not enough for 50 working chemists, and the
remaining work is architectural, not tuning:

1. Replace `BaseHTTPMiddleware` with pure ASGI middleware (blocks multi-worker entirely).
2. Move the per-session turn guard out of process memory (blocks multi-worker correctness).
3. Make `create_session` shed retryably instead of 500ing.
4. Only then raise `service_max_concurrent_turns` from a guess to a measured value.

Until 1 and 2 land, adding CPU cannot help: the service can use exactly one core.

---

# CORRECTION, and the run that settles it

**The `--workers 4` result reported above was not a measurement.** `run_load.sh` did
`pkill -f "uvicorn service.app"; sleep 2` before starting the new server. A graceful uvicorn
shutdown holds its listening socket well past two seconds, so the new server died with
`[Errno 98] Address already in use` and never bound. The 50 clients got
`status=0, "All connection attempts failed"` — connection refused, against nothing. The 44
`RuntimeError("No response returned.")` tracebacks in that log belong to the *previous* process
being torn down with streams open.

So "multi-process is blocked entirely" and "the service can use exactly one core" were both
**wrong**, and wrong for a reason worth naming: a harness that can silently measure the wrong
process is worse than no harness. `run_load.sh` now waits for :8000 to actually be free, aborts on
a bind failure, and requires `/healthz` to answer before driving load.

The `BaseHTTPMiddleware` defect was real, but it is a **single-worker** defect: it turns any stream
that outlives its handler into a 500 with a traceback. The single-worker run logged **44 of them** —
i.e. every in-flight conversation during a rolling deploy.

## The four runs, same harness, same stack, same 400 ms stub

| | no pool | D-119 (pool) | D-121, 1 worker | **D-121, 4 workers** |
|---|---|---|---|---|
| 200 / attempted | 150 / 150 | 105 / 120 | 133 / 150 | **150 / 150** |
| **500** | 0 | **15** | **0** | **0** |
| 503 (retryable) | 0 | 0 | 17 | **0** |
| transport errors | 0 | 0 | 0 | **0** |
| p50 | 37.3 s | 26.7 s | 41.5 s | **18.6 s** |
| p95 | 64.2 s | 32.2 s | 61.6 s | **37.8 s** |
| **throughput** | 1.19/s | 1.17/s | 1.11/s | **1.83/s** |
| connections opened | 401 | 0 | — | — |
| peak concurrent conns | 28 | 13 | 22 | 30 |
| `No response returned` | 44 | 44 | **0** | **0** |

## Finding 2 is overturned: throughput does scale

`10 → 50 users` was flat at ~1.18 turns/s across every single-process configuration, and it stayed
flat when the pool removed connection churn and when the blocking work moved off the loop. **Four
processes take it to 1.83 turns/s — 1.65×** — with p50 halved (41.5 → 18.6 s) and p95 down 39 %.

The serialization point was the single event loop, exactly as the earlier analysis argued. What was
wrong was the conclusion that nothing could be done about it: the three blockers (a
`BaseHTTPMiddleware` incompatible with SSE, a per-process turn guard, a pool timeout raising 500)
are fixed, and the experiment runs.

1.65× rather than 4× is expected here and not a limit of the design. The box has 4 CPUs shared with
Postgres, Temporal, the background worker, three connector workers, the stub LLM — and
`scripts.connectors_dev`, which serves **all six connector bundles from one uvicorn process on one
event loop**, and through which every tool call from every turn passes. In production each bundle is
its own Deployment. Testing that is the next measurement, not a claim.

## The other two fixes, confirmed in the same runs

**The 500s are gone.** D-119's pool converted event-loop starvation into 15 × HTTP 500 at
`create_session`. At 1 worker those are now **17 × 503** with the admission path's wording, counted
separately as `chemclaw_db_unavailable_total=17` while `chemclaw_turns_shed_total=0` — so the metric
distinguishes "could not schedule a connection handoff" from "the LLM endpoint is full". At 4
workers there are **none of either**: the starvation that produced them is what the extra processes
removed.

**The cross-process turn guard holds.** `chemclaw_turns_conflict_total=0` across the 4-worker run
because the harness does not double-submit; it was verified directly instead — 6 pairs of concurrent
turns on one session returned `[200, 409]` six times out of six under `--workers 4`, and the same 6
pairs under `session_store=memory` returned `[200, 404]`/`[404, 404]`, proving the requests really
did land on different workers and that the 409s came from the durable claim rather than either
worker's own in-process set.

## One thing the 4-worker run exposed that is not yet fixed

`/metrics` reported `turns_started_total=99` and `live_sessions=33` after a run of 150 turns and 50
sessions, because a scrape reaches **one** worker of four. Per-process counters are correct for the
shipped default (`workers=1`, scaled by replicas, each pod scraped separately) and undercount for
any pod that raises the worker count. Recorded, not fixed.

## What the numbers mean for 50 chemists

50 concurrent users, 3 turns each, **150 of 150 turns completed, zero failures, zero sheds, zero
conflicts, zero transport errors**, at a p50 of 18.6 s with a 1 s model — on a 4-CPU box that is
also running the database, the workflow engine, five workers and the entire connector fleet.

That is the target met, with the honest caveats stated: a stub model rather than Anthropic, one pod
rather than a cluster, and a connector fleet collapsed into a single process.

---

# SECOND CORRECTION: the tools were never running

Every run above reported "100 tool calls" and "the tool path genuinely exercised". **Both were
wrong.** The stub emitted `{"query": "benzene"}` while `find_notes` takes `text`, so every one of
those calls failed argument validation inside `agent_framework._tools._auto_invoke_function` and
returned an error to the model. The calls were *dispatched*; no tool body — no RDKit, no note scan,
no database read — ever *ran*.

This surfaced from the opposite direction: chasing why `audit_events` stayed empty after the sink
was wired (D-122). The middleware looked dead; it was simply never reached, because the parse-error
branch precedes it.

## The run with tools actually executing

| | 4 workers, broken tools | **4 workers, real tools** |
|---|---|---|
| 200 / attempted | 150 / 150 | **150 / 150** |
| 500 / 503 / 409 / transport | 0 / 0 / 0 / 0 | **0 / 0 / 0 / 0** |
| p50 | 18.6 s | **14.9 s** |
| p95 | 37.8 s | **35.4 s** |
| throughput | 1.83/s | **2.08/s** |
| tool calls | 100 (all rejected) | **100 (all executed)** |

Faster with real work, not slower — the box was quieter (the earlier run overlapped the test suite),
and `find_notes` against this knowledge tree is cheap. The honest reading is that these two numbers
are within run-to-run noise of each other, and that **the tool body is not this system's bottleneck
at 50 users**; the event loop was, and more processes fixed it.

## The audit trail as an independent witness

`audit_events` after the run — a check that was impossible before D-122, because the table was empty:

```
 tool          | outcome | count | actors | corr
---------------+---------+-------+--------+------
 find_notes    | ok      |   102 |     52 |  102
 submit_qm_job | ok      |     2 |      2 |    2
 expand_note   | ok      |     1 |      1 |    1
```

Three things verified by one query, none of them assertable from the driver's own counters:

1. **The tools genuinely executed** — 100 successful `find_notes` bodies, not 100 dispatches.
2. **Per-user attribution holds under load** — 52 distinct actors, the 50 signed load identities
   plus two from earlier probes. No cross-attribution, no collapse to a service account.
3. **The correlation id is per *turn*** — 102 distinct ids for 102 calls. Before D-119 the id was
   generated once per `build_agent` and agents are cached per profile, so every turn from every user
   on a pod shared one, and the trail could not separate two conversations. It can now.

The GxP trail went from 4 rows (all from CLI and test usage, none from the service) to a complete,
attributed, hash-chained record of a 150-turn 50-user run.

---

# THE LIVE RUN: 50 concurrent chemists on real Anthropic traffic

Haiku, 4 uvicorn workers, 50 distinct signed Entra identities, `session_store=postgres`, budgets
on, the connector fleet up. This is the run the whole exercise was for, and it separates cleanly
into "the system held" and "the model integration did not".

## The infrastructure held

```
users=50 turns/user=3 attempted=150 wall=85.2s
status codes: {200: 150}
  200 OK: 150 · 503 shed: 0 · 429 budget: 0 · 409 conflict: 0 · transport: 0
turn latency s: p50=19.81 p95=37.93 p99=42.88 max=42.88
throughput    : 1.76 completed turns/s
```

Every turn admitted. No shedding, no budget refusal, no session conflict, no transport error, no
pool timeout, no 500. p50 19.8 s against a real model that is itself most of that time.

The audit trail confirms it from the other side — this line is from the live run's own log:

```
agents.audit: tool gather_evidence ok in 4 ms [cid=d0429282... actor=load-user-002]
```

Correct per-user attribution, per-turn correlation id, durable row. Under 50-way live load.

## The model integration did not: 20 % of turns fail

```
event types: {'answer': 120, 'error': 30, 'token': 1734, 'tool_call': 151}
```

**30 of 150 turns ended in an error event**, every one the same Anthropic 400:

```
messages.1.content.3.tool_use.name: String should have at least 1 character
```

An assistant `tool_use` block reconstructed with an **empty tool name**, which makes the follow-up
request carrying the tool results invalid.

Characterised, not guessed at:

| Question | Answer |
|---|---|
| Load-dependent? | **No.** 17 % at 4 users, 20 % at 50 — flat. |
| The durable session store? | **No.** Reproduces on `session_store=memory` too. |
| Streaming-specific? | **Yes.** Non-streaming `agent.run` over the same prompts never fails. |
| Which block? | Always `content.2`/`content.3` of the assistant message *from this turn* — so the model emitted **two or more `tool_use` blocks**, and a later one lost its name. |

It is a *different* fault from the harness streaming 400 already in `DEFERRED.md` (that one puts a
`user` block between `tool_use` and `tool_result`, is harness-only, and hits 100 % of calls). This
one is on the **classic path with `harness_enabled` off** — the shipped default — and it is in
`agent_framework`'s accumulation of streamed tool-call deltas, not in code this repo owns.

Tracked as **STREAM-1** (`LIVE-1` was already taken by the earlier e2e pass). It is the highest-severity open item, and nothing but a live run finds it:
the stub emits one tool call per response and never validates the assistant message it is handed
back, so every stub run above reported a clean 150/150.

## What the two runs say together

The stub run proves the **infrastructure** carries 50 concurrent users: 150/150, zero failures,
2.08 turns/s, tools genuinely executing. The live run proves the **same is true against a real
model** — 150/150 admitted, same absence of infrastructure failures, 1.76 turns/s — and that a
one-in-five turn is then lost to a client-library defect between us and the provider.

Fixing STREAM-1 is what stands between "50 chemists can use this" and "50 chemists can use this
reliably". Everything under it is now measured rather than assumed.

---

# STREAM-1 FIXED: the same run, 150 answers and zero errors

`AgentPool` (D-123) leases one agent — and with it one chat client — per concurrent turn. Same
harness, same stack, same 50 live users:

| | before | **after** |
|---|---|---|
| answers / errors | 120 / **30** | **150 / 0** |
| empty `tool_use` names in the log | 30 | **0** |
| 200 / attempted | 150 / 150 | 150 / 150 |
| p50 | 19.8 s | **16.9 s** |
| p95 | 37.9 s | **34.8 s** |
| throughput | 1.76/s | **1.99/s** |
| tool calls | 151 | **208** |

Latency and throughput improved too, which follows rather than surprises: a turn that died at its
first tool call was *finishing early*, and 208 tool calls against 151 is the number of tools that
now run to completion instead of dying with their turn.

## Where this leaves the target

**50 concurrent chemists, real Anthropic traffic, every turn answered.** 150/150 admitted and
150/150 answered, with no shed, no budget refusal, no session conflict, no transport error, no pool
timeout, no 500 — at a p50 of 16.9 s against a real model, on a 4-CPU box also running Postgres,
Temporal, five workers and the whole connector fleet.

The caveats that remain are stated, not buried: one pod rather than a cluster, a connector fleet
collapsed into a single process, and three turns per user rather than a working day.

---

# Stage 5d — two replicas, one database

Two independent uvicorn **processes** on separate ports (not `--workers`: replicas have separate
memory, which is what the cross-process turn claim and the per-pod caches actually face), sharing
one Postgres. Six sessions, each created on replica A, then:

```
guard results (A, B): [(200,409), (200,409), (200,409), (200,409), (200,409), (409,200)]

sessions where BOTH replicas admitted a turn:                    0/6   (want 0)
sessions usable on the replica that did not create them:         6/6   (want 6)
sessions readable by a different user:                           0/6   (want 0)
```

**The cross-process turn guard (D-121) holds.** Two turns fired simultaneously at two processes:
exactly one admitted every time, the other 409. The last pair went the other way — the race is a
race — and that is the point: either may win, never both. Before D-121 this test would have
returned `(200, 200)` and silently interleaved two turns into one conversation.

Rehydration and isolation hold too: a session created on A is usable on B, which never saw it, and
is invisible to a different owner.

# Stage 5e — chaos

| Scenario | Result |
|---|---|
| **Connector fleet killed mid-flight** | Turn still answers (**HTTP 200**) with a reduced tool surface, and `/readyz` names the connector as unreachable. The D-118 failure — connectors silently dropped, nobody able to tell — does not recur. |
| **Postgres stopped and restarted** | The pool **recovered without a service restart**: both replicas answered `/healthz` and created sessions again once the database came back. |
| **Client disconnects mid-turn** | **FAILS.** The session refuses the next turn for **63 s**. Recorded as CHAOS-1. |

> **Correction (see the re-run below).** The first row is half wrong and the readings are kept as
> written so the mistake is legible. The turn did answer, but `/readyz` naming the connector
> unreachable proved nothing: it said `unreachable` *before* the kill as well, because the probe was
> pointed at the wrong address entirely (D-131). The signal was not being read — it was constant.

## CHAOS-1, and two wrong theories

Abandon an SSE turn mid-stream and the same session answers 409 for 63 seconds. The blocker is the
in-process `active_turns` set, whose `discard` sits in the streamed generator's `finally`.

Two explanations were tested and **both were wrong**:

1. *The `await` in that `finally` is cancelled before it reaches the database.* Detaching the
   release onto its own task changed the measured time not at all — 63.5 s against 65.1 s. The
   change was **reverted rather than shipped unverified**.
2. *The abandoned turn runs to completion and holds the session.* No: that session's actor produced
   **zero** `audit_events` rows, so no tool ever ran.

So the generator's `finally` is not firing promptly and neither story explains why. The next step is
to instrument the teardown directly rather than reason about it. It costs availability of one
conversation for about a minute and never costs correctness — which is why it is recorded rather
than guessed at.

# Stage 5e, re-run — CHAOS-1 resolved, and two more defects behind it

The instrument was the whole answer, and theory 1 turned out to be *right about the mechanism and
wrong in its experiment* — a distinction only measurement could draw.

## Attributing the 63 seconds

Two guards can hold that 409 and no earlier run had separated them. Sampling both once per second
while polling settles it in one pass:

```
t+ 0.0s  POST=409  in_flight=0.0  claim=81d518@+59.9s
t+30.9s  POST=409  in_flight=0.0  claim=81d518@+28.9s
t+59.9s  POST=409  in_flight=0.0  claim=81d518@+0.0s
t+60.9s  POST=200  in_flight=0.0  claim=81d518@+60.0s
```

`in_flight` is 0 in the *first* sample, so the third theory dies too: the in-process set was freed
immediately and the generator's `finally` did run promptly. The durable claim's `expires_at` counts
down from exactly `service_turn_claim_lease_seconds` and is never refreshed. The recovery time *is*
the lease. The release never landed.

Tracing the claim store shows why, without inference:

```
CLAIMTRACE t+ 0.75s claim(71743695) -> True
STREAMTRACE agent stream got CancelledError
CLAIMTRACE t+ 0.75s release(71743695) ENTERED
CLAIMTRACE t+ 0.76s claim(71743695) -> False        <- and no COMPLETED, ever
```

Entered on every abandoned turn, completed on none. A bare `await` inside a cancelled task raises at
its first suspension point, so the release reached the database call and died there. The earlier
"detach it onto a task" experiment had the right idea and was measured on a branch without the fix.

## What the same trace line gave away

`agent stream got CancelledError` answers a question nobody had asked: **which** exception a real
disconnect delivers. The runner rolled a half-written turn back under `except GeneratorExit:` — the
exception `aclose()` raises. sse-starlette never calls `aclose()` on the body iterator; it cancels
its task group. So the rollback that exists to stop one dropped connection from poisoning a
conversation was unreachable on the only path that reaches it, and the suite reported green because
all three of its abandonment tests closed the stream by hand.

Writing the regression test walked into the same trap once more: the first version cancelled the
consumer while it sat in its own frame, so the abandoned generator was finalised later by
`asyncio.run`'s async-generator shutdown — which raises `GeneratorExit` — and the test passed
against the unfixed code. It now waits for the agent to signal that it has stalled.

## Re-run results

Live Anthropic, Postgres sessions, the connection dropped as soon as a `tool_call` event reached the
wire.

| Scenario | Before | After |
|---|---|---|
| **C1** disconnect mid-tool-call, one replica | session 409 for **60.9 s** | free in **0.0 s**; next turn answers in 2.6 s; **no unmatched `tool_use`** left in durable history |
| **C2** disconnect on replica A, next turn on replica B | **HTTP 409** | HTTP 200, answered in 4.8 s |
| **C3** connector fleet SIGKILLed mid-turn | `/readyz` constant, signal unreadable | all six flip `healthy` → `unreachable`; the turn still **answers** (39 s, three tool retries), and the next turn completes on the reduced surface |
| **C4** Postgres stopped at the instant of the disconnect | — | release fails *attributably*; session usable again 50 s after the database returns, inside the lease |

C2 is the row that matters for the shipped chart: a process that never served the abandoned turn has
only the `session_turns` row to go on, so the durable claim is the whole guard there.

C4 was not a confirmation — it **found the third defect**. The first run produced a bare
`Task exception was never retrieved` and no attributable warning, because the store raises
`psycopg.errors.AdminShutdown`, which matched none of the `(ConnectionError, OSError, RuntimeError)`
the release caught. Shielding had turned a failure that used to be discarded with its `finally` into
one nobody was left to read. Widened, re-run, clean.

C3 could not be read at all until the probe was fixed (D-131): `/readyz` reported `unreachable` for
every connector before and after the kill, because `connector_urls` moves the tool endpoint and the
probe was still reading the manifest's loopback dev default. The shipped chart always sets that
override, so in a cluster the readiness signal was constant — and under `connectors_required: true`
the front door would have refused to start, blaming connectors that were healthy.

## What is still open here

Nothing from this scenario. Two things are worth stating so they are not mistaken for coverage:

* The durable-history rollback did not delete any rows in C1, and that is expected rather than a
  gap: with per-service-call history persistence disabled (the LIVE-1 mitigation), an abandoned turn
  has not committed anything yet, so the session-state rollback is what does the work. The durable
  half remains the guard for the configuration that does persist mid-turn.
* C4's 50 s recovery is the lease doing its job, not a regression. Shielding makes the release
  *run*; when the store is gone there is nothing that can make it *succeed*, which is why the lease
  exists.
