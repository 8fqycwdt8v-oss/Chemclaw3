# Sweep: resource lifecycle (round 1)

Cross-cutting read of `src/` for who opens / who closes / what happens on cancellation, for every
DB pool, HTTP client, file handle, subprocess, MCP session, Temporal client and git worktree.
Everything below was reproduced against the live sandbox (Postgres + Temporal up, venv synced).
Scripts are under `/tmp/claude-0/`.

---

## A process opens three Postgres pools; every bound and every gauge counts one

- **Severity**: high
- **Location**:
  - `src/chemclaw/core/db.py:60` (`_POOLS`, keyed by `(dsn, options)`), `:238` (`bind_pool_metrics`
    binding `chemclaw_pg_pool_max_size` to `settings.pg_pool_max_size`), `:269` (`pool_stats`)
  - `src/chemclaw/agent/checkpointer.py:367` (`_checkpoint_pool`, a *second* `AsyncConnectionPool`
    with `max_size=settings.pg_pool_max_size`)
  - `src/chemclaw/core/config/__init__.py:217-226` (the fleet check: `pg_fleet_pooled_processes *
    pg_pool_max_size`)
  - `src/chemclaw/api/routes/ops.py:77-80` (the `/readyz` probe, the second core pool)
- **Trigger**: any front-door process under the shipped chart. It needs only (a) one ordinary store
  call, (b) one kubelet readiness probe, (c) one turn. Each produces a distinct pool:
  `_POOLS` is keyed on the libpq `options` string, and `/readyz` passes
  `statement_timeout_seconds=service_readiness_db_timeout_seconds` (2.0) where every other call
  site takes the default (30.0) — two keys, same DSN. The checkpointer is a third pool by
  construction.
- **Consequence**: the per-process Postgres connection ceiling is `3 × pg_pool_max_size`, while
  every mechanism that is supposed to bound it counts `1 × pg_pool_max_size`:
  - `Settings` startup validator computes `pooled_processes × pg_pool_max_size`.
  - the gauge `chemclaw_pg_pool_max_size` reports `settings.pg_pool_max_size`.
  - the shipped alert
    `sum(chemclaw_pg_pool_max_size) > max(chemclaw_pg_fleet_max_connections)`
    (`deploy/helm/chemclaw/templates/prometheusrule.yaml:254`) therefore compares a 3×-understated
    left-hand side.
  - `pool_stats()` (the `pool_size` / `pool_available` / `requests_waiting` gauges) never sees the
    checkpointer pool at all, so the saturation signal D-119 introduced is blind to the pool that
    serves every turn's state.

  `values.yaml:285` calls `postgres.maxConnections: 136` "a provisioning requirement, not a
  preference", derived as 17 pooled processes × 8. With `service.autoscaling.maxReplicas: 6` and
  `CHEMCLAW_SESSION_STORE: "postgres"`, the six front-door pods alone can open 6 × 3 × 8 = **144**
  — past the whole declared fleet ceiling before a single worker or connector is counted; the real
  fleet figure is ~232 against a provisioned 136. The failure mode is the one D-119 documents:
  connect timeouts against an idle-looking database.

- **Evidence** (`/tmp/claude-0/t6.py`, run against the live Postgres):

```
distinct core pools: 2
   key: -c statement_timeout=30000 -> pool_max: 16
   key: -c statement_timeout=2000 -> pool_max: 16
checkpointer pool_max: 16
REAL per-process ceiling: 48
gauge chemclaw_pg_pool_max_size reports: 16.0
fleet check computes: 16
```

  And the metric blindness, separately (`/tmp/claude-0/t5.py`):

```
after shared pool: pool_stats = {'pool_size': 3, 'pool_available': 3, 'requests_waiting': 0}
after checkpointer: pool_stats = {'pool_size': 3, 'pool_available': 3, 'requests_waiting': 0}
checkpointer pool stats: {... 'pool_max': 16, 'pool_size': 1, 'pool_available': 1 ...}
```

  Two comments in the tree assert the property that is false here:
  - `core/db.py:222` — "`chemclaw_pg_pool_max_size` is the per-process half of the fleet connection
    budget: `sum()` of it across pods is what the deployment may open". It is one third of what a
    front-door pod may open.
  - `core/db.py:216` — "Binding it where the pool is opened means a process cannot acquire a pool
    without also acquiring its witness." The checkpointer pool is acquired with no witness; it is
    not built through `_pool_for` and never calls `bind_pool_metrics`.
  - `core/config/store.py:81` states the total as "`max_size × distinct DSNs × processes`" while
    the check two files away implements `processes × max_size`, dropping the middle factor — and
    the real multiplier is distinct *(dsn, options)* pairs plus the checkpointer, not distinct DSNs.

- **Fix**: make the number one function of the pools that exist rather than of a setting.
  1. `pool_stats()` and the max-size gauge should sum over the pools the process actually holds.
     Register the checkpointer pool with `core/db` (a `register_pool(pool)` seam) so
     `bind_pool_metrics` reports `sum(p.get_stats()["pool_max"])` and `pool_size` across all of
     them; that makes the Prometheus alert correct with no rule change.
  2. Give the `/readyz` probe the same pool as everything else. Its 2 s bound is a *statement*
     timeout; a `SET LOCAL statement_timeout` on the borrowed connection buys the same bound
     without a second pool. Alternatively key `_POOLS` on the DSN alone and apply the per-call
     timeout as a transaction-local `set_config`, which is what
     `apply_vector_recall_settings` already does for pgvector's knobs.
  3. Make the startup fleet check read the same aggregate (pools per process, not one), and
     re-derive `postgres.maxConnections` from it.

---

## `memory_store()` publishes the store before `setup()` has run — 7 of 8 concurrent turns fail on a cold database

- **Severity**: medium
- **Location**: `src/chemclaw/agent/scratchpad.py:134-155` (`memory_store`)
- **Trigger**: a deployment with `CHEMCLAW_AGENT_MEMORY_ENABLED=true` and
  `CHEMCLAW_SESSION_STORE=postgres`, taking more than one concurrent turn against a database where
  `AsyncPostgresStore.setup()` has not previously run (first deploy of the feature; any fresh
  database; a new schema).
- **Consequence**: the global is assigned *before* the awaited migration:

```python
_store = AsyncPostgresStore(await _checkpoint_pool())
await _store.setup()
```

  A second turn arriving inside `setup()` sees a non-`None` `_store`, returns it, and every store
  operation raises `psycopg.errors.UndefinedTable: relation "store" does not exist` — surfacing to
  the chemist as `api/runner.py`'s classified `internal` turn failure. This is verbatim the defect
  `agent/checkpointer.py:121-134` documents having fixed for `checkpointer()` and
  `_checkpoint_pool()` ("Both … assigned their global *before* awaiting the work that makes the
  object usable … `relation "checkpoints" does not exist` on a cold start with traffic, which is
  every deploy of a two-replica chart"). `memory_store` shares that module's pool and reuses its
  shape, but takes no lock and publishes early.

- **Evidence** (`/tmp/claude-0/t7.py` — 8 concurrent `memory_store()` + one store read, against a
  freshly created schema on the live Postgres):

```
(0, 'ok')
(1, 'UndefinedTable: relation "store" does not exist')
(2, 'UndefinedTable: relation "store" does not exist')
(3, 'UndefinedTable: relation "store" does not exist')
(4, 'UndefinedTable: relation "store" does not exist')
(5, 'UndefinedTable: relation "store" does not exist')
(6, 'UndefinedTable: relation "store" does not exist')
(7, 'UndefinedTable: relation "store" does not exist')
```

  Re-running against the now-migrated schema gives 8/8 `ok` (`/tmp/claude-0/t7b.py`), which fixes
  the blast radius to *cold databases only* — and is why this is invisible to the suite.

  Control, same file, same pool, same pattern, with the lock (`/tmp/claude-0/t10.py`): 8 concurrent
  `checkpointer()` calls on a fresh schema → 8/8 `ok`. The fix is already written 40 lines away.

  A second, related claim that is now false: `agent/checkpointer.py:358-363` says
  "**Called only from `checkpointer()`, which already holds `_init_lock` — so this takes no lock of
  its own**". `scratchpad.memory_store` calls `_checkpoint_pool()` directly, outside the lock. I
  could not turn that into a reproduction — `AsyncConnectionPool.open()` acquires an uncontended
  `asyncio.Lock` and never suspends, so `_pool` is published within one scheduling slice
  (`/tmp/claude-0/t8.py`: 1 pool created, not 2) — but the comment licenses the missing lock on a
  premise that stopped being true, and one upstream `await` inside `open()` turns it into two open
  pools of which only one is ever closed.

- **Fix**: give `memory_store` the same treatment `checkpointer()` has — acquire
  `checkpointer._initialization_lock()`, re-check under it, and assign `_store` only after
  `setup()` returns. Better still, fold both into one `async def _ready(global, factory)` helper so
  the third lazy Postgres singleton cannot reintroduce this a third time; and correct
  `_checkpoint_pool`'s docstring (or make it take the lock itself, guarding with a re-entrancy-free
  private `_pool_lock`).

---

## The HPC launcher opens a TCP+TLS connection per poll — 43,200 per 24 h run

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/qm/hpc/nextflow.py:80` (`_client`), used by `launch_run`
  (`:113`), `poll_run` (`:130`) and `fetch_artifacts` (`:152`, its own client); driven by
  `src/chemclaw/connectors/qm/activities.py:104-141` (`_poll_nextflow`)
- **Trigger**: any real (non-mock) QM/DFT job. `_poll_nextflow` loops
  `await nextflow.poll_run(handle)` every `hpc_poll_interval_seconds` (default **2.0 s**) for the
  life of the run; `hpc_run_timeout_seconds` defaults to **86400** and the activity's own docstring
  says "during an up-to-24h run".
- **Consequence**: `poll_run` builds and tears down a whole `httpx.AsyncClient` per call, so the
  connection pool it contains is discarded before it can ever be reused. That is one TCP connect —
  and against a real Seqera/Tower endpoint one full TLS handshake — per poll: **43,200 handshakes
  per QM job per day**, on the qm worker's event loop, times however many QM jobs are in flight.
  This is precisely the cost model `core/db.py:14-19` describes for Postgres ("the cost is not the
  database … but the *event loop*: a connect that cannot be scheduled within the timeout fails")
  and that `agent/llm_provider._tls_http_client` was `@cache`d to avoid ("an uncached factory built
  a fresh `AsyncClient` — a fresh connection pool, a fresh TLS context — for every question asked").
  The same lesson has been applied to Postgres, to the LLM client and to the Temporal client; the
  HPC poll loop is the one long-lived hot loop it was not applied to.
- **Evidence** (`/tmp/claude-0/t9.py` — a local keep-alive HTTP server counting accepted TCP
  connections, 50 polls each way):

```
50 poll_run() calls -> TCP connections accepted by the launcher: 50
50 calls on ONE shared client   -> TCP connections accepted: 1
```

- **Fix**: hold one client for the poll loop. The smallest correct change is to give
  `_poll_nextflow` an `async with` client and pass it into `poll_run`/`fetch_artifacts`, so the
  client's lifetime is the run's; a `@cache`d module client (the shape `llm_provider` uses) also
  works but is worse here because the activity, not the process, owns the run. `fetch_artifacts`
  must keep its own client — the cross-origin-header argument in its comment is sound and
  unaffected.

---

## The checkpointer pool is never closed by any production shutdown path

- **Severity**: medium
- **Location**: `src/chemclaw/agent/checkpointer.py:379` (`close_checkpointer`),
  `src/chemclaw/agent/scratchpad.py:158` (`close_memory_store`), against
  `src/chemclaw/api/app.py:154` (the front door's lifespan) and
  `src/chemclaw/durable/serve.py:68` (each worker's entrypoint)
- **Trigger**: SIGTERM to any pod that has taken at least one turn (a rolling update, a node
  drain, an HPA scale-down — the shipped chart's ordinary lifecycle).
- **Consequence**: both shutdown paths wrap their body in `async with db.pooling()`, whose exit
  closes every pool in `core/db._POOLS`. The checkpointer's pool is not one of them, and
  `close_checkpointer` / `close_memory_store` have **zero** non-test callers in the tree. So the
  connections serving turn state — and, when agent memory is on, the memory store sharing that pool
  — are dropped rather than closed at every deploy, leaving the server to reap them. That is
  literally the third bullet `durable/serve.py:13` names as the reason graceful shutdown was built:
  "The pod's own cleanup never runs: `db.pooling()`'s connections are dropped rather than closed".
  The module that carries that argument closes one pool and misses the other.
- **Evidence** (`/tmp/claude-0/t5.py`, the tail of the run):

```
after pooling() exit: pool_stats = {'pool_size': 0, 'pool_available': 0, 'requests_waiting': 0}
checkpointer pool closed? False
```

  and `grep -rn "close_checkpointer" src/` returns only the definition and two docstring mentions;
  every call site is under `tests/`.

- **Fix**: close it where the other pools are closed. Either register the checkpointer pool with
  `core/db` so `pooling()`'s `finally` covers it (which also fixes the metrics half of finding 1),
  or add `await close_memory_store(); await close_checkpointer()` to the front door's lifespan
  `finally` and to `durable/serve.serve_worker`. Registering is better: it removes the possibility
  of a fourth pool being added and missed again.

---

## What I checked and found sound

Recording these because a short findings list should say what it ruled out.

- **Per-turn LLM clients.** `build_chat_model` runs on every graph build (once per turn) and
  constructs a fresh `ChatOpenAI`/`ChatAnthropic`. Measured (`/tmp/claude-0/t3.py`,
  `/tmp/claude-0/t4.py`): 10 builds → 10 distinct `AsyncOpenAI`/`AsyncAnthropic` objects but **1**
  underlying httpx client, because `langchain_openai._client_utils` and
  `langchain_anthropic._client_utils` memoize the transport. No per-turn socket churn. (Note this
  is upstream behaviour, not a first-party guarantee — it is a candidate row for
  `tests/test_upstream_surface.py`, which asserts six other such couplings.)
- **Connector MCP clients.** `registry.connector_http_client` builds one `httpx.AsyncClient` per
  connector per turn, and `registry._mcp_connection`'s docstring claims the adapter closes it.
  Verified in the installed distribution:
  `langchain_mcp_adapters/sessions.py:358` enters `async with (client, streamable_http_client(...),
  ClientSession(...))`, so the client is closed on every path including a failed handshake. One
  detail of that docstring is wrong without consequence: it says the library's `timeout`/`auth`/
  `headers` "all three arrive empty"; `timeout` in fact arrives as
  `httpx.Timeout(DEFAULT_STREAMABLE_HTTP_TIMEOUT, read=DEFAULT_STREAMABLE_HTTP_SSE_READ_TIMEOUT)`.
  The factory ignores it, so behaviour matches the manifest either way.
- **`HeldConnectorSession`** (`connectors/transport.py`). The task-affinity argument holds: the
  session is entered and exited on one task, `_hold`'s `finally` sets `_opened` unconditionally so a
  failed connector cannot strand a waiting turn, and `__aenter__`'s `except BaseException:
  await self._shut_down()` covers cancellation *during* connect. `_shut_down` sets `_stop` before
  awaiting, so even a second cancellation that skips the await leaves a holder task that will still
  unwind on its own.
- **git worktrees** (`kg/git_submitter.py`). The failure path is genuinely covered: the worktree is
  released in `_submit_locked`'s `finally`, `_release_worktree` swallows `BaseException` (stated and
  argued), `_sweep_leftover_worktrees` reclaims a SIGKILLed submission's directory *and* explains
  correctly why `git worktree prune` alone would not, and `_run` kills + reaps the child on both
  `TimeoutError` and `CancelledError`. The `flock` is held on an open file description and released
  by the kernel on process death.
- **SSE admission slots.** `api/routes/streams._SlotBoundEventStream` correctly moves the release
  from the generator's `finally` (which never runs when a client vanishes before the first
  `__anext__`) to the response's `__call__`; `event_streams` pops its key at zero, so it is bounded
  by concurrent streams rather than by lifetime principals. The turn route's equivalent window is
  bounded by the durable claim's lease, as its comment says.
- **`beating()`** (`durable/heartbeat.py`) cancels *and awaits* the wrapped task in a `finally`, so
  no detached work survives a heartbeat failure or a cancellation.
- **File handles.** The only non-`with` `open()` in `src/` is the submit lock, which is closed in a
  `finally`. `ingest/documents/sync._read_and_parse` hands the raw `os.open` descriptor to
  `os.fdopen(..., closefd=True)` and sets `descriptor = -1` so the `finally` cannot double-close —
  correct. No async generator yields while holding a raw file handle or a DB connection outside an
  `@asynccontextmanager`.
- **Unbounded caches.** Every keyed-by-identity map I could find is either a `BoundedLru`
  (`api/state`, `api/budget`, `api/rate_limit`, `agent/attachments`, `cli/mock_llm`), keyed by
  configuration rather than by user input (`api/auth._jwks_clients`, `kg/graph._NOTES_CACHE`,
  `kg/conflicts._INDEX_CACHE`, `connectors/jobs._PARAMS_MODELS`), FIFO-trimmed
  (`core/embeddings._CACHE`), or popped at zero (`api/routes/streams.event_streams`). I found no
  call site that should use `core/bounded.py` and does not.
- **Temporal client.** One per process, cached, failure never poisons the singleton — as documented.
  (`_CONNECT_LOCK` is built at module scope rather than lazily like the checkpointer's; on 3.11 an
  `asyncio.Lock` binds at first await, so this only matters for a process that runs two event loops
  and contends on the first connect in the second one. Not reproduced; noted only because the two
  modules disagree about the same hazard.)
