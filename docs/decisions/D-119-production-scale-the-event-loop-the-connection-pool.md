# D-119 — Production scale: the event loop, the connection pool, and a guard that switched itself off

**Context.** A 50-concurrent-user load test against the live stack (Postgres 16 + pgvector,
Temporal, the connector fleet, 50 signed identities, `session_store=postgres`) with a stub LLM at a
fixed 400 ms think-time measured three things the code review had only inferred.

Throughput was **flat at ~1.18 turns/s from 10 concurrent users to 50** — five times the load for
1.7% more work and 5× the latency (p50 7.4 s → 37.3 s). That is a serialization point, not a
resource limit: added concurrency became queueing. The box had 4 CPUs and the service used one.

**32 Postgres connect timeouts** occurred while peak concurrent connections was 28 of
`max_connections=100`. The database was idle. The connects timed out because the single event loop
could not schedule them inside `pg_connect_timeout_seconds`. The real load was churn: **401
connections opened for 150 turns**.

And every one of those 32 failures was the same call site — the rollback watermark (D-107), whose
handler is deliberately non-fatal. So the churn did not merely cost latency: it **silently disarmed
a correctness guard**, precisely under the conditions (loaded server, slow turns, impatient users)
that make the failure it guards against likely.

**Decision.**

1. *Blocking work leaves the loop.* RDKit depiction, parsing and descriptors in the `chem`
   connector; `structure_from_smiles(..., optimize=True)` at every async call site; and
   `spec.cache_key(structure)` in all eight `run_cached_*` wrappers — the last of which was
   invisible, being an argument expression evaluated before `run_cached`'s own offload, and which
   shells out to `xtb --version` on its first call in a process. The long `subprocess.run` in
   `xtb_cli`/`crest_cli` was already offloaded; only the version probe was not. `gather_evidence`
   moves from a sequential list comprehension to `asyncio.gather`.

2. *Connections are pooled per process.* `chemclaw/db.py` gains `connection()` and `pooling()`,
   entered once by the front door's lifespan, each worker, and each connector app. Pools are keyed
   by `(dsn, merged libpq options)` so a migration's untimed connection cannot share a pool with a
   request path's bounded one, and `_merged_options` (D-107) is unchanged, so a DSN's own
   `search_path` still survives — the test-schema isolation depends on it. Pool exhaustion raises
   `ConnectionError`, the same retryable infrastructure fault an unreachable database raises.

3. *The disarmed guard becomes loud, and stays non-fatal.* Failing the turn would trade a
   conditional future fault (this session breaks only if the client also disconnects mid-tool-call)
   for a certain immediate one (every turn fails whenever the session store hiccups). A mitigation
   must not take down what it mitigates. What was wrong was the silence, so it is now an ERROR plus
   `chemclaw_rollback_watermark_unavailable_total`.

**The one thing deliberately not done.** `service_uvicorn_workers` exists and defaults to **1**.
`active_turns` — the 409 that stops two turns interleaving on one session's thread — and the
admission semaphore are per-process in-memory guards; with N workers each sees 1/N of the traffic,
so two turns on one session landing on different workers would both be admitted and corrupt the
thread. Moving the guard to a Postgres advisory lock was considered and rejected: the lock is
connection-scoped, so it would pin one pooled connection for a turn's whole duration —
reintroducing exactly the exhaustion this ADR removes. Threads, not processes, are what the change
actually buys, and they touch neither guard. The same hazard already exists across `replicas` and
remains tracked in `BACKLOG.md`.

**Guarantees traded, in full.** A connector state reported by `/readyz` may be up to
`service_readiness_cache_seconds` (5 s) stale. A note changed *outside* this process may be
invisible for up to `graph_cache_ttl_seconds`, raised 5 s → 60 s — which costs nothing real,
because the only out-of-process writer is the knowledge-sync sidecar on a 300 s cadence, so the
shorter window bought scans and no freshness. Both are settable to 0. Nothing else changed
behaviour.

**Also landed.** One Temporal client per process instead of one gRPC channel (and, under mTLS, one
TLS handshake plus three blocking PEM reads) per job launch and status poll. One `httpx.AsyncClient`
per readiness sweep instead of one per connector. `configure_logging`/`configure_telemetry` at the
front door, which had never called either — so `CHEMCLAW_OTEL_ENABLED` was inert at the one process
a chemist talks to. And the correlation id becomes per-turn ambient state
(`agents.identity_context`) rather than a value bound inside `build_agent`: agents are cached per
profile for the pod's life, so every turn from every user had been sharing one id, which made the
GxP audit trail unable to separate two conversations.
