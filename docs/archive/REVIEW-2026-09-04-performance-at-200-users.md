# REVIEW 2026-09-04 — Performance at 200 concurrent users

A ten-track review of whether this platform can serve **200 simultaneous chemists** doing agentic
work: many concurrent turns, heavy MCP tool calls, heavy Postgres activity, durable jobs.

Every number below was **measured on this branch** — against live PostgreSQL 16.15 / pgvector 0.8.0,
a live Temporal broker, the real MCP servers over real sockets, the built UI server against a fake
upstream, and (for the end-to-end lane) a real model. Where a figure is modelled rather than
observed it says so. Probes are in the session scratchpad; each track's long-form report names its
own harness.

**Read the arithmetic, not the adjectives.** This repository's own rule is that prose is evidence
about what its author believed and never about what the code does, and this review found four
documented claims falsified by measurement — each noted in place below.

---

## Verdict

**No, not as configured — and the reason is not the one the architecture is defended against.**

The platform does not run out of CPU, memory, Postgres capacity or event-loop headroom at 200 users.
Measured end to end against a real model, a turn is **8.32 s wall / 0.581 s CPU — 7% machinery,
93% waiting on the LLM.** What it runs out of is **admission permits**, and it runs out of them
while sitting at **35% of one core**, refusing two thirds of offered load with an `HTTP 200` that no
5xx-based alarm can see, under an autoscaler watching a CPU number that never moves.

Everything else this review found is either a *silent cliff* (a path that is fast in every test and
23×–880× slower in production, listed in §4) or a *cascade* (a saturation that converts into an
outage rather than a queue, §5).

The good news is proportionate: **the five highest-value fixes are one-line changes** with measured
payoffs between 52× and 880×, and none of them requires an architectural change. The capacity
ceiling itself is a `values.yaml` number — legitimately the LLM endpoint's throughput budget to set,
which is exactly what that ceiling was built to express.

---

## Method

Ten parallel tracks, each measuring rather than reading: front door / asyncio, Postgres, agent turn
hot path, connector client, MCP fleet, Temporal, capacity & deployment, caching & retrieval, the UI,
and an empirical load lane. Findings that appear in more than one track are marked **[×N]** — four
of the top findings were reached independently by three or four tracks arriving from different
directions, which is the strongest signal in this document.

---

## 1 — The shape of the problem: occupancy, not throughput

The single most useful measurement in the review:

| | measured |
|---|---|
| turn wall clock (real model) | **8.32 s** |
| turn CPU (front door, `/proc/<pid>/stat`) | **0.581 s** |
| ratio | **7% machinery / 93% LLM wait** |
| CPU per turn across `conc` 8 → 32 | flat at **0.50–0.57 s** |
| load shed at `conc=32` | **33 of 48** |
| CPU while shedding | **35.0% of one core** |

A permit is held for the *whole* 8.3 s; the CPU it consumes is 0.58 s of that. So **occupancy is ~14×
CPU**, and every capacity control in the system that is denominated in CPU is measuring the wrong
quantity. A slower model doubles occupancy and leaves CPU flat.

This is why the front door is a single-server queue: throughput is flat at **1.5–1.8 turns/s from
concurrency 1 to 64** against a zero-latency model, while p50 goes 0.561 s → 9.457 s. Little's law
fits exactly (c=8: 8 / 1.61 = 4.97 s predicted vs 5.08 s measured). Added concurrency becomes
queueing, precisely as `D-119` measured at 50 users — this review reproduces that shape at 64 and
finds the cause unchanged.

**Consequence for the whole document:** the loop-blocking findings in §4 matter for *latency,
jitter and the shed threshold*, not for aggregate CPU. The permit ceiling in §3 is what decides
whether 200 users are served at all.

---

## 2 — Capacity model, and what saturates in what order

### The turn-rate sensitivity (state your assumption, it changes the answer)

Concurrent turns in flight = users × turns/hour × 8.3 s / 3600. The shipped fleet admits **48**.

| chemist intensity | turns/user/hr | in flight at 200 users | fits in 48? |
|---|---|---|---|
| occasional (1 turn / 5 min) | 12 | 5.5 | yes, comfortably |
| steady (1 turn / 2 min) | 30 | 13.8 | yes |
| **engaged (1 turn / 60 s)** | 60 | **27.7** | yes, but no burst headroom |
| **heads-down (1 turn / 30 s)** | 120 | **55.3** | **no** |
| Monday 09:00 burst | — | spikes ≫ steady state | **no** |

So "200 users" alone does not decide it. **48 permits serve 200 *steady* users and fail 200
*engaged* ones**, with no headroom for the burst that a shared work rhythm guarantees. That is the
honest framing, and it is why the recommendation in §7 is to raise the ceiling *and* fix the
autoscaler rather than to pick a bigger number.

### Order of saturation, scaling 10 → 200

1. **~15–20 users** — `servers/calc` (4 slots, 1 replica; one CREST search takes all four). §3.3
2. **~35–50 users** — connector-`calc` Temporal queue; modelled p50 queue wait **~1.04 h**. §3.4
3. **~48 turns in flight** — the fleet admission ceiling. §3.1 — *but the HPA never gets there* (§3.2),
   so the practical wall is **16**.
4. **~170 users per UI pod** — the BFF upstream socket pool (512) below the SSE streams held (600). §5.2
5. **~200 SSE streams per API pod** — uvicorn's connection limit, which fails *liveness*. §5.1
6. Postgres — **never** the bottleneck in the load lane (pool peaked 20/48, zero waiters through
   c=100). It becomes one only via the silent cliffs in §4.1 and the write volume in §4.4.

---

## 3 — Tier 1: the platform cannot reach 200 engaged users as configured

### 3.1 — The admission ceiling is 48, and it is a hard boot refusal **[×4]**

`6 replicas` (`deploy/helm/chemclaw/values.yaml:72`) × `1 uvicorn worker`
(`core/config/service.py:59`; **>1 raises `ValueError`** at `core/config/__init__.py:382`) ×
`8 permits` (`core/config/service.py:119`) = **48**, pinned by
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS: "48"` (`values.yaml:784`), which makes any higher
product a boot refusal in every pod (`core/config/__init__.py:392-405`).

The single-worker refusal is *correct* and well argued — five guarantees are per-process in-memory
(rate limiter, budget tracker, attachment store, session LRU, metrics registry) — and it structurally
prevents the classic "N workers on 1 CPU" wound. But it means the only scaling axis is replicas, and
the replica count is chosen by an autoscaler that cannot see the constraint.

**Measured headroom:** 1 core ÷ 0.55 s CPU/turn ÷ 8.3 s turns ⇒ **~15 permits per process before CPU
binds** — roughly 2× the shipped 8.

### 3.2 — The autoscaler watches CPU; saturation is invisible to CPU **[×4]**

The HPA scales on `targetCPUUtilizationPercentage: 70` of a `500m` request
(`templates/service-route.yaml:82-88`, `values.yaml:71-73,546`) = 350 millicores.

Measured with the semaphore **100% full** — 8 turns streaming plus 150 idle SSE streams:

```
CPU 6.19 s over 28.4 s = 218 millicores  →  44% of target, while completely saturated
```

So the fleet sits at `minReplicas: 2` = **16 concurrent turns for 200 users**, and the excess is
shed. `chemclaw_turns_shed_total` exists; nothing acts on it. `values.yaml:64-68` already names this
as gap DEP-4 — the measurement is what is new.

### 3.3 — The shed is an `HTTP 200`, so no 5xx alarm can see it

Measured wire shape at 400 and 800 offered turns: **`HTTP 200`** followed by an SSE frame
`{"type":"error","code":"at_capacity","retryable":true}` — 743 of them. Meanwhile
`core/config/service.py:116` still documents "shed with 503" (stale; the 503s at
`api/middleware.py:87,120` are pool-checkout and durable-unreachable).

A platform that refuses two thirds of its users while reporting 100% availability and 35% CPU will
be diagnosed as healthy.

### 3.4 — `servers/calc` is a one-search-at-a-time singleton, fleet-wide **[×3]**

`replicas: 1` (`servers/calc/deploy/deployment.yaml:21`), `calc_max_concurrent_requests: 4`
(`engine/config.py:151`), `CHEMCLAW_CREST_THREADS=4` (`Containerfile:116`), and `admission.py:131`
clamps an over-budget cost to the whole limit — its own comment: *"the expensive one takes the pod
exclusively"*. So **one conformer search occupies the entire platform's calculation capacity**, for
up to 4 h.

There is **no HPA, no PDB, no topology spread and no `terminationGracePeriodSeconds` on any of the
seven MCP servers**, so there is no capacity lever to pull and any rollout is a 100% outage of that
capability. Measured real work: `optimize_geometry` aspirin **24.5 s**, a 50-atom fragment
**389.6 s**; refusal rate at concurrency 60 was **68%**.

`crest_timeout_seconds=14400` against `connector.yaml`'s `request_timeout: 900` is a **16×** mismatch,
and `asyncio.shield` (`admission.py:63-67`) holds the slot after the caller has given up — so the
pod stays occupied for up to 4 h serving a request nobody is waiting for.

### 3.5 — A capacity refusal is classified as bad data and never retried **[×3]**

The sharpest defect in the review, because both sides are individually correct.

`servers/calc/engine/admission.py:135` refuses with a `ValueError` whose text ends **"Retry once one
finishes"**. On the wire that is indistinguishable from a domain refusal:

```
admission ValueError  →  McpRequestRefused (core/mcp_session.py:432)
                      →  CalcToolError     (connectors/calc/remote.py:235)
                      →  _BAD_DATA_TYPES   (durable/publish.py:203)  →  NON-RETRYABLE
```

`durable/publish.py:198-203` states the intent precisely — *"an unparameterised solvent, an atom
index past the molecule, a SMILES outside a predictor's domain"* — and deliberately excludes
`CalcServerError` because an unreachable server *is* worth retrying. **The taxonomy has two buckets,
refused and broke, and saturation is a third one it does not have.** It only bites under load, which
is exactly the 200-user case: at one CREST question per user per day the calc pod is at ~790%
utilisation, so essentially every cache *miss* fails permanently, carrying the serving side's own
advice to retry.

The guard that would have caught `8 offered > 4 admitted` ships inert —
`CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS: "0"` (`values.yaml:667`) — and by its own comment
covers only the durable path, not the interactive one.

### 3.6 — The connector-`calc` queue is ~4× under-provisioned, with no wait bound

1 pod × 8 slots × 300 s median job = **96 jobs/h** against ~400/h demand. Measured on the real broker
(200 jobs, 1 s activity, 8 slots): wall 27.4 s, p50 schedule→start 10.3 s, p95 19.2 s — linear, so at
300 s activities **p50 wait ≈ 1.04 h, p95 ≈ 1.98 h**. `queue_wait_timeout()`
(`durable/publish.py:293`) is passed at every *core* call site and at **none** of the connector-bundle
ones (`calc/workflows.py:105`, `bo/workflows.py:214`, `results/workflows.py:88`), so the wait is
unbounded up to the child's 5 h timeout.

---

## 4 — Tier 2: silent cliffs (fast in every test, catastrophic in production)

These share a signature: **nothing in the suite can see them**, because each needs a long-lived
connection, a warm process, a real corpus or a bound connector to appear.

### 4.1 — psycopg auto-prepare turns vector search into a sequential scan **[×2]**

`prepare_threshold` appears **nowhere in `src/`** (verified), and `core/db.py:254` sets only
`connect_timeout` and `options` — so psycopg3's default of **5** applies on long-lived pooled
connections. On execution 11, Postgres switches the statement to a **generic plan**, and the generic
plan cannot use HNSW because the `ORDER BY` operand is a parameter.

Measured on 100k chunks with the verbatim statement from `ingest/documents/index.py:879`:

```
prepare=False : 16 10 9 10 9 8 8 10 10 9 8 8 9 6 7 8 …   (25 execs, all ~8 ms)
default       : 16 13 9 10 9 10 9 10 7 10 |1305 1317 1363 1307 …
```

**9 ms → 1,280 ms, permanent for that connection**, reproduced twice; `EXPLAIN (GENERIC_PLAN)`
confirms `Parallel Seq Scan on document_chunks`. Linear in corpus size — 1M chunks ≈ **13 s**.

The same shape was found independently on the **scoped lexical note query**
(`retrieval/vector_index.py:353,413`, `note_id = ANY(%(ids)s::text[])` with ~20,000 ids): first-5
median 119 ms → last-5 median **6,986 ms (×58.9)**, onset at execution 11 every time, across three
trials. And again at `science/fingerprints/store.py:494`.

Each such query pins one of `pg_pool_max_size=16` for 5–8 s **on the turn's critical path**.

> **One-line fix:** `prepare_threshold=None` in the pool `kwargs` at `core/db.py:254`.

### 4.2 — Every MCP tool call re-validates its own schema against the meta-schema

`mcp/server/lowlevel/server.py:536,573` call `jsonschema.validate` on input and structured output of
every call. That helper runs `check_schema` — re-compiling a *static* schema — every time, on the
event loop, outside every offload.

```
props.solvent_properties, in-process:
  the tool function                    0.0053 ms
  ToolManager.call_tool (kit wrappers) 0.0164 ms
  lowlevel CallToolRequest handler     8.3726 ms   ← 170× the work it wraps
```

Reproduced standalone on a 25-property schema: **7.071 ms/call vs 0.008 ms with a memoised
validator — 880×.** Worst per server: `calc.search_binding_modes` **15.1 ms**, `chem.describe_sites`
10.3, `props` 9.2. Counterfactual on a throwaway process: **12.62 → 4.87 ms p50, 2.6× throughput.**

The irony worth recording: `calc.calculation_key` is deliberately left ungated by admission control
(`engine/admission.py:46-53`) *so it stays answerable while the pod is full* — and it costs **14.8 ms
CPU, ~93% of it revalidating its own schema.**

> **Fix:** memoise the validators in `mcp_server_kit/app.py`, which already reaches
> `server._mcp_server` (`app.py:411`).

### 4.3 — A fresh SSL context per connector per turn, on the one event loop

`connectors/registry.py:364` and `core/mcp_session.py:307` construct `httpx.AsyncClient` with no
`verify=`, so httpx parses the certifi CA bundle from scratch — for endpoints that are plain
in-cluster `http://`. Reproduced in the project venv, for the 7 clients one turn opens:

```
7 default clients (one turn): 156.1 ms
7 shared-ctx clients      :   0.4 ms      ← 390×
```

cProfile puts `load_verify_locations` at **0.433 s of 1.371 s — the largest `tottime` entry**. That
is ~156 ms of *blocking* CPU on the loop that serves every user on the pod, per turn, before a tool
runs.

| concurrent turns | connector open p50 | max loop stall |
|---|---|---|
| 1 | 271 ms | 136 ms |
| **8 (shipped cap)** | **2,104 ms** | **1,154 ms** |
| 32 | 9,378 ms | 5,112 ms |

**The cliff:** `connector_open_timeout_seconds=15`; at 32 concurrent opens p50 is already 9.4 s.
Around 45–50, healthy connectors time out *from client CPU alone*, `transport.py:229` records them
unreachable, and `reachability.py:65` then serves **30 s of turns with no tools at all**.

Proof of cause (probe-only patch, one shared context): open p50 2,104 → **1,279 ms**, loop stall
1,154 → **224 ms**; at n=32, 5,112 → **294 ms**.

### 4.4 — The checkpointer rewrites the whole thread every superstep **[×3]**

`agent/compaction.py:27-34` states that compaction narrows what is *sent* and leaves graph state
untouched, and `grep RemoveMessage src/` is empty — so a thread grows monotonically. Upstream's
`_dump_blobs` (`langgraph/checkpoint/postgres/base.py:549`) rewrites the *entire* messages channel
per superstep.

Measured, 10 turns on one thread with incompressible content:

| | |
|---|---|
| conversation | 464 kB |
| written to `checkpoint_blobs` | **24.3 MB** |
| on disk / WAL | 26.3 MB / 27.3 MB |
| **amplification** | **52×, quadratic in session length** |
| per-turn wall clock, no LLM | 72 ms → **249 ms** |

At the review's load that is **~10–58 GB/day**; the chart's own 30-day example implies
**300 GB – 1.7 TB**. `values.yaml:1240` states the row count; the byte volume is stated nowhere.
`retention.py` prunes by *thread*, so an active session keeps every superseded copy — and
`retention_enabled` is **absent from all 36 chart config keys**, so nothing prunes at all by default.

Two aggravating factors: every write is its own transaction (`checkpointer.py:537` autocommit), and
`AsyncPostgresSaver._cursor` holds **one process-wide `asyncio.Lock`** (`aio.py:374`) so its
8-connection pool can never use more than one — measured at **261 ms/turn, 30 turns/s** vs 180 ms /
43 turns/s with a per-turn saver.

### 4.5 — The token-floor ratchet has a blind spot ~75% the size of what it measures

`tests/test_context_floor.py:406` calls `build_langgraph_agent(...)` **without the `connectors=`
argument** that exists at `agent/langgraph_agent.py:148`. So the guard measures 61 tools / **42,730
tokens** against its 43,500 ceiling (98% full), while a shipped turn also binds **52 connector tools
with zero name overlap** — measured through `convert_to_openai_tool` at **~32,000 more tokens**, for
a real prefix of **~74,700**.

The function's docstring is emphatic that reading the `ToolNode` means *"any future tool source — a
middleware, a connector, upstream — lands here the moment it is bound"*. That is true of the
**method** and false of the **fixture**.

Two consequences:

- **The compaction policy is floored.** At a 74,700 prefix, `effective_trigger(73_500)` → **1**, i.e.
  *clear every reclaimable tool result on every model call*, and the thread budget collapses to
  25,300 of 100,000. `CLAUDE.md` and `agent/context_budget.py` both assert the shipped configuration
  is **not** floored, and `tests/test_compaction.py` asserts it — against the connector-less prefix.
- **There is no prompt caching on the shipped provider.** `agent/llm_provider.py:405`:
  `if settings.llm_provider != "anthropic": return []`, and `values.yaml:567` ships
  `openai_compatible`. So ~74,700 tokens of identical bytes are re-prefilled at full price on every
  model call — **~88% of every request**. At 4,000 turns/hr that is ≈ **587k prompt tokens/second**.

> **Highest-leverage fix in the review, and it is one flag on somebody else's endpoint:**
> vLLM `--enable-prefix-caching` (or equivalent). Then the deferred-tool-schema plan
> (`D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`) saves a further ~32,000, and profile
> routing saves up to 37,267 (`property-lookup` = 5,463 vs `default` = 42,730).

---

## 5 — Tier 3: saturations that become outages instead of queues

### 5.1 — At the connection limit, `/healthz` returns 503 and the kubelet kills a busy pod

`--limit-concurrency 256` (`deploy/entrypoint.sh:49`) counts **open sockets including idle
keep-alives** and answers 503 *above* the ASGI app (`uvicorn/.../h11_impl.py:225-226`). Proven: 20
*idle* keep-alive sockets at limit 20 → `fresh /healthz -> 503`. With 255 SSE streams held at 256:
`{'/healthz': 503, '/readyz': 503, '/sessions': 503, 'POST turn': 503}`.

Reachable because `service_max_event_streams_total: 200` (`core/config/service.py:282`) is **78% of**
`service_max_connections: 256` (`:199`) — and **nothing cross-checks the two**, in a config validator
that carefully cross-checks fleet turns, fleet Postgres connections and fleet calc requests.

The cascade: one pod lost → the survivor takes all 200 streams → `/readyz` drains at ~30 s →
`/healthz` **SIGKILLs at ~60 s** (`values.yaml:386-397`), killing every in-flight turn. The liveness
probe's stated premise — *"`/healthz` does no work, so its budget is about a wedged event loop"* — is
falsified: here a restart makes it strictly worse.

*(Measured contrast, worth recording: the MCP fleet's probes are **sound** under the same pressure —
with all 4 `calc` slots on 24 s optimisations for 55 s, `/healthz` answered **53/53, p50 37 ms, max
197 ms, zero over the 3 s timeout**. The dedicated readiness pool and single-flight work exactly as
documented there. The defect is specific to the front door's socket accounting.)*

### 5.2 — The UI's upstream socket pool is smaller than the streams it holds

`server/proxy.ts:56-62` creates **one** `http.Agent` with `maxSockets: 512` (`server/config.ts:247`)
and `timeout: 0`, shared by SSE streams and ordinary calls. `src/hooks/useJobStreams.ts:50` opens
`MAX_JOB_STREAMS = 3` per tab, held for the tab's life, with no visibility gating. **200 × 3 = 600 >
512.**

Measured against the real built `dist/server.js`:

```
fake upstream:  {"status":"ok","open":512}          ← exactly maxSockets
GET /api/healthz through the BFF:  curl: (28) timed out after 20002 ms with 0 bytes
```

With the pool full, *any* request queues — including `POST /api/sessions/{id}/messages` — and Node's
agent queue has no timeout here, so it hangs rather than failing. **Wall at 512/3 ≈ 170 users per UI
pod.**

### 5.3 — The fleet Postgres budget is short by 3×

`core/config/__init__.py:408` computes `pooled_processes × pg_pool_max_size`, i.e. **one pool per
process**. Measured via `pg_stat_activity` and the `chemclaw_pg_pool_max_size` gauge, a turn-serving
process holds **three**: the stores pool, the `/readyz` pool (a different `options` key,
`api/routes/ops.py:181`), and the checkpointer's own (`agent/checkpointer.py:539`) — **24, not 8.**

Shipped chart: declared 112 ≤ `maxConnections: 136` → **passes**; real ≈ **272**. The runtime alert
is honest (`_process_max_connections` already sums foreign pools); the startup check is not. No
PgBouncer anywhere — and adding one would silently break `pg_advisory_lock`
(`kg/git_submitter.py:562`), prepared statements and the saver's pipeline mode.

### 5.4 — Work that outlives the user, and completions that vanish

- **Abandoned turns hold permits.** Since `D-2026-08-27` a disconnect only *detaches*: the turn runs
  to completion holding 1 of 8 permits for up to 600 s. Measured with cap 2: two disconnected clients
  held both permits and the next real chemist got `at_capacity`. The Stop button is implemented
  correctly, but there is **no `pagehide`/`beforeunload` handler** that sends `/turn/stop` — and
  `logger.ts:273` already uses `keepalive: true` from `pagehide`, so the mechanism exists one file away.
- **Push-back and job records are dropped at 60 s.** Both carry `schedule_to_close_timeout = 60 s`
  (`durable/notify.py:102`, `connector_job.py:1007`) on the queue that also runs 900 s template steps.
  Measured: 8 slots held by long activities → a 50 ms light activity waits 41.6 s, and the real shape
  returns `DROPPED: ActivityError: Activity task timed out` at **60.1 s**. Both are swallowed
  best-effort, so the chemist's session shows "running" forever and `job_records` — the only copy that
  outlives Temporal history — is lost.
- **MCP sessions are never garbage-collected.** `FastMCP.streamable_http_app()` never passes
  `session_idle_timeout`, which defaults to `None`. Measured: 500 un-DELETEd sessions on `chem` grew
  RSS **150.3 → 223.1 MB (149 kB each)**, plus a live anyio task each, and an orphaned session id
  still answered **HTTP 200** ten seconds after its client exited. Chemclaw3 opens one session per
  turn per connector; at a 2% DELETE failure rate that is ~2,400 orphans ≈ 20 h to OOMKill a 512Mi pod.
- **Detached-turn recovery is a herd amplifier.** `sendMessage.ts:636-651`: **210 full-transcript GETs
  over 630 s at a fixed 3 s interval, no jitter, no backoff even when every request fails** — and the
  trigger is any dropped turn stream, so a rollout puts every in-flight turn into it in lockstep
  (~16.7 req/s of unpaginated `GET /sessions/{id}/messages`). The correct helper already exists at
  `useJobStreams.ts:275`: capped exponential backoff with jitter.

---

## 6 — The cheap wins

Ordered by measured payoff per line changed. None is architectural.

| # | Change | Where | Measured effect |
|---|---|---|---|
| 1 | `prepare_threshold=None` in pool kwargs | `core/db.py:254` | vector search **1,280 ms → 9 ms**; note query **6,986 → 119 ms** |
| 2 | Memoise jsonschema validators | `mcp_server_kit/app.py` | per-call **7.07 → 0.008 ms**; server throughput **2.6×** |
| 3 | Share one `ssl.SSLContext` across clients | `connectors/registry.py:364`, `core/mcp_session.py:307` | **156 → 0.4 ms** blocking CPU/turn; loop stall at cap **1,154 → 224 ms** |
| 4 | Enable prefix caching on the LLM endpoint | deployment, not code | removes ~88% of **587k prompt tok/s** |
| 5 | `CREATE INDEX ON reaction_records(reaction_id)` | migration | **200 ms / 195 MB → 1.0 ms / 1.6 MB** at 500k rows |
| 6 | Add `MAX_UPSTREAM_SOCKETS` ↑ and a separate Agent for SSE routes | `server/proxy.ts:56` | removes the 170-user/pod wall |
| 7 | Enable `retention_enabled` in the chart + raise the per-pass cap | `values.yaml` | stops unbounded 10–58 GB/day growth |
| 8 | Reclassify admission refusal as retryable | `durable/publish.py:203` + a distinct exception | durable calc jobs queue instead of failing |
| 9 | Cross-check `max_event_streams_total` vs `max_connections` | `core/config/__init__.py` | closes the liveness cascade |
| 10 | Bind `connectors=` in the floor ratchet | `tests/test_context_floor.py:406` | makes the 43,500 ceiling mean what it says |

---

## 7 — Sizing recommendation

Starting points to measure against, not claims.

**Front door.** `service_max_concurrent_turns` 8 → **16** (measured headroom is ~15 before CPU binds);
HPA **4–16 replicas on `turns_in_flight / turn_capacity`**, not CPU — the metric already exists,
the prometheus-adapter does not; `requests.cpu` 500m → **1** (a single event loop must not be
CFS-throttled by dense packing); `service_fleet_max_concurrent_turns` → **192**; and either raise
`service_max_connections` above `200 + turns + probes + scrape` or lower the stream cap.

**Postgres.** 16 vCPU / 64 GB / NVMe / ≥500 GB, retention **on** with the per-pass cap at 5,000,
`pg_pool_max_size` → 12 with the real 3-pools-per-process arithmetic in the startup check, and
`prepare_threshold=None`. PgBouncer only if the advisory-lock and pipeline-mode consequences are
addressed first.

**MCP fleet.** `servers/calc` **6 replicas × 8 slots × 8 CPU**, plus a PDB and a
`terminationGracePeriodSeconds` above the longest inline call. Every other server 3 replicas × 2 CPU
with a sized executor — note `mcp_server_kit` currently sizes only a 1-thread *readiness* executor
(`app.py:479`), leaving tool bodies in CPython's `min(32, os.cpu_count()+4)` where `cpu_count()` is
the **node's**: 32 threads in a 1-CPU cgroup. Chemclaw3 fixed exactly this for itself in
`core/executor.py`; the fix never crossed repos.

**Temporal.** `connector-calc` 1 → **4+ pods**, and pass `queue_wait_timeout()` at the connector-bundle
call sites so a queued job fails fast instead of after 5 h.

**A note on RDKit sizing.** Two docstrings in the fleet justify concurrency ceilings with "RDKit
releases the GIL … real parallelism". Measured on 4 cores: fingerprinting **0.91×** on 4 threads;
`chem.render_structure` on a 241-atom molecule **0.94–0.98× at concurrency 1 through 16**, throughput
flat at ~22/s. Offloading RDKit off the loop *does* work (measured: 31 heartbeats vs an ideal 68
during 330 ms of work, max lag 8.5 ms — `D-119`'s decision #1 stands), but it buys **no parallelism**.
Raising CPU limits does nothing for RDKit throughput; only more pods do.

---

## 8 — What is sound

A clean area is a real result, and this codebase has a lot of them. Verified, not assumed:

- **Failure is graceful.** No 503 storm, hang, OOM or pool timeout at c=400; goodput degrades ~50%
  under 50× overload with no collapse. Multi-turn growth is flat (25 turns/session, 0.28–0.30 s each).
- **Postgres was never the load-lane bottleneck** — pool peaked 20/48, zero waiters through c=100, and
  the dominant wait was `Client:ClientRead` (Postgres waiting on the app).
- **Indexing is otherwise excellent** — all 397 SQL literals extracted by AST and cross-checked
  against `pg_indexes`; §6/5 is the only real gap.
- **Every timeout is bounded** and **every Temporal retry policy is bounded** — no unbounded retry and
  no retry storm is possible anywhere; a `loop.set_debug` audit over 8 turns found **no** sync psycopg,
  `requests`, file IO, RDKit or hashing on the request path.
- **Temporal payloads are safe** — worst realistic `ConformerEnsemble` is **82 kB** against the 2 MB
  limit; Hessians never cross the wire. Temporal's own DB is not a bottleneck (~5.6 events/s).
- **Memory is not the wall** at the front door: 347–358 MB baseline, 587 kB per compiled graph,
  ~1.2 MB per concurrent turn; 40 turns = +49 MB against a 1Gi limit.
- **Every `lru_cache` and module-level cache in `src/` is bounded**, none keyed on unbounded user
  input, and no cache warming happens at import. The KG single-flight lock is correct (8 threads → 1
  parse). The embedding cache design is correct. `cached_compute` single-flights (20 concurrent
  misses → 1 compute) and holds no connection across the network call.
- **The LLM HTTP client is pooled** across turns; `as_structured_tool`'s `@cache` works exactly as
  documented; manifest discovery is free (0.0001 ms) with no per-turn filesystem walk.
- **The UI has no polling amplification** — steady state is **6.7 req/s at 200 users**, one 30 s health
  probe, `document.hidden`-guarded and in-flight-latched. Its job-stream reconnect is textbook
  (capped exponential backoff *with* jitter, abortable sleeps, 401 terminal, controllers aborted on
  unmount). All heavy chemistry (RDKit 6.9 MB, Ketcher 19.7 MB) is behind dynamic `import()`.
- **MCP input bounds hold** — 1500-carbon chains, 120-residue peptides, 12 stereocentres and 40-deep
  branches across six enumeration tools: nothing over 1.2 s, refusals prompt and well worded. The
  egress guard costs nothing per request.
- **`service_uvicorn_workers` refused above 1** makes the classic "N workers on 1 CPU" wound
  structurally impossible, and `core/executor.py` sizes the thread pool from caps that already exist.

---

## 9 — What this review could not measure

- **Multi-replica behaviour.** Fleet figures are scaled from one process, not observed. The
  per-process guards (rate limiter, budget, attachment store, session LRU) are known to be N× across
  replicas; the *user-visible* consequence at 6 replicas was not exercised.
- **Heavy `calc` under real concurrency** — needs more cores than this box has.
- **A real soak.** Everything here is minutes. The leak in §5.4 (MCP sessions) is a 20-hour effect and
  was measured by extrapolation from a 500-session probe, not observed to OOM.
- **The browser → tenant identity hop**, which remains the one unproven leg for reasons already on
  record.

---

## Provenance

Ten parallel review tracks, 2026-09-04, all measurements on this branch. Long-form per-track reports
(front door, Postgres, agent turn, connector client, MCP fleet, Temporal, capacity, caching &
retrieval, UI, load lane) were produced with their harnesses; the findings above are the subset that
survived cross-checking, with `[×N]` marking independent rediscovery.
