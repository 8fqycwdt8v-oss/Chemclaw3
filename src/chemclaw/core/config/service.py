"""Settings for the front-door run service: binding, limits, sessions, budgets.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from chemclaw.core.config.dsn import DatabaseDsn


class ServiceSettings(BaseSettings):
    """The front-door run service: the one ASGI trust boundary.

    How the server binds, what a request may cost (size, concurrency, wall clock, tokens), and how
    durable sessions and job push-back reach the browser.
    """

    # The ASGI service that runs the agent for a chemist: builds the agent, opens the turn's MCP
    # sessions, streams the response and serves the browser chat. Binds all interfaces inside the
    # container; the OpenShift Route and NetworkPolicy gate ingress.
    service_host: str = "0.0.0.0"
    service_port: int = Field(default=8080, gt=0)
    # Explicit opt-in to boot unauthenticated on a non-loopback bind. With `entra_required` False
    # every request runs as the shared dev principal with gates open, so the front door refuses an
    # exposed interface without this.
    service_allow_insecure: bool = False
    # Comma-separated browser-origin allow-list; empty allows no cross-origin access. `*` is refused
    # (`_no_wildcard_origin`).
    service_cors_origins: str = ""
    # Exists only to refuse values above 1 at startup (`_guards_that_the_comments_already_demand`);
    # `deploy/entrypoint.sh` passes no `--workers`. The rate limiter, budget tracker, live-session
    # LRU, metrics and a running turn's event pump are per process, so scale out with `replicas` and
    # session affinity instead. Still the middle factor of the fleet product `replicas × workers ×
    # cap`.
    service_uvicorn_workers: int = Field(default=1, gt=0)
    # Validity of a turn's claim on its session (`session_turns`) before another process may take
    # it. A lease, refreshed every third of this interval, so no connection is held for the turn;
    # above worst event-loop delays and below the turn timeout, so a crashed worker frees its
    # session in about a minute.
    service_turn_claim_lease_seconds: float = Field(default=60.0, gt=0)
    # Messages that may wait in one session behind its running turn; each runs as its own sender's
    # turn, and past this the next is refused 409. One place per sender per session. A waiter holds
    # a stream, so a process holds at most `service_max_concurrent_turns` × this (past it 429),
    # which the connection backstop in `core/config/__init__.py` charges.
    service_turn_queue_max: int = Field(default=4, ge=1)
    # How often a waiting message asks whether it is next; the ask refreshes its lease, so it must
    # be shorter than the turn-claim lease (checked at startup). In-process waiters are woken at
    # once.
    service_turn_queue_poll_seconds: float = Field(default=1.0, gt=0)
    # Participants besides the sender who may follow one running turn (`GET
    # /sessions/{id}/turn/stream`), each with its own bounded buffer. A watch also takes a
    # `service_max_event_streams_per_user` slot.
    service_turn_max_watchers: int = Field(default=4, ge=0)
    # How long a watcher's membership is trusted before re-reading it on the next event, bounding
    # how long a removed member keeps watching.
    service_turn_watch_recheck_seconds: float = Field(default=5.0, gt=0)
    # Poll interval for cross-replica turn relay (`agent/turn_remotes.py`): a reattach or Stop
    # landing on another replica becomes a row the holder polls for, and relayed frames come back
    # the same way. Polls run only while there is something to relay.
    service_turn_relay_poll_seconds: float = Field(default=0.25, gt=0)
    # Lifetime of a relay request without refresh, and how long the asker waits for the holder's
    # first answer before 503. Must exceed the poll interval (checked at startup).
    service_turn_relay_lease_seconds: float = Field(default=10.0, gt=0)
    # Max characters in one chat message; larger is a clean 422.
    service_max_message_chars: int = Field(default=100_000, gt=0)
    # Security headers on every response: a CSP scoped to the self-served chat UI, nosniff,
    # `X-Frame-Options: DENY` and HSTS. Turn off only where the ingress sets its own policy.
    service_security_headers: bool = True

    # Durable session store. `memory` is in-process (dev/test); `postgres` persists each turn's
    # messages to `session_messages` so a fresh process resumes the thread. Session state is the
    # conversation layer, not Temporal job state. `session_store_dsn` may name another database;
    # empty uses `postgres_dsn`.
    session_store: Literal["memory", "postgres"] = "memory"
    session_store_dsn: DatabaseDsn = ""
    # Cap on the in-process live-session cache; least-recently-used handles are evicted, durable
    # history survives.
    service_max_live_sessions: int = Field(default=1000, gt=0)
    # Most sessions `GET /sessions` returns, newest first (it fills a sidebar).
    service_max_listed_sessions: int = Field(default=100, gt=0)
    # Most sessions `GET /plans/pending` reads a plan for. Each read is a checkpointer statement
    # serialized behind one lock (`agent/checkpointer.py`), so a long scan would stall every turn.
    # Sessions that cannot hold a plan are skipped free; unreached ones come back as `unread`.
    service_max_plan_scans: int = Field(default=25, gt=0)
    # Admission control: permits for concurrent turns, held for the whole streamed run; a turn not
    # admitted within the admission timeout is shed. The response has already started, so a shed is
    # an SSE `{"type":"error","code":"at_capacity"}` frame under HTTP 200, not a 503;
    # `chemclaw_turns_shed_total` is the signal. 12 is about what one core carries (turns are mostly
    # waiting on the model). The fleet ceiling below is where the endpoint's real capacity is
    # stated.
    service_max_concurrent_turns: int = Field(default=12, gt=0)
    service_turn_admission_timeout_seconds: float = Field(default=5.0, gt=0)
    # Per-actor cap on simultaneous turns across their sessions, so one principal cannot hold every
    # permit (the rate limit meters rate, not concurrency). 0 disables, the code default, so
    # `chemclaw.cli.live_storm` can drive admission from one credential; the chart sets it. Per
    # process; a fleet-wide per-actor limit belongs at the ingress. A value at or above
    # `service_max_concurrent_turns` is refused.
    service_max_concurrent_turns_per_actor: int = Field(default=0, ge=0)
    # Threads kept above what the admission caps can occupy in the shared `asyncio.to_thread` pool
    # (`core/executor.py`), so short calls (token validation, probes, reconnects) never queue behind
    # a parse or embedding. The reserved part derives from the caps; tune this only if short calls
    # queue.
    service_thread_pool_headroom: int = Field(default=8, gt=0)
    # Front-door pods this deployment may reach, which turns the per-process cap into what the LLM
    # endpoint sees (`replicas × workers × cap`). The chart derives it from
    # `autoscaling.maxReplicas` (or `service.replicas`); 1 suits a CLI or dev run.
    #
    # `fleet_max_concurrent_turns` is the endpoint's permitted ceiling, declared by the operator;
    # when set, startup refuses a product above it. 0 = undeclared.
    service_fleet_replicas: int = Field(default=1, gt=0)
    # Front-door pods during a rolling update (chart's `chemclaw.frontDoorProcessesAtRolloutPeak`),
    # for the connection budget's per-pod readiness term. 0 falls back to the steady figures.
    service_fleet_replicas_at_rollout_peak: int = Field(default=0, ge=0)
    service_fleet_max_concurrent_turns: int = Field(default=0, ge=0)
    # Per-principal request budget (`api/rate_limit.py`), spent in `require_principal` so it covers
    # every authenticated route and no probes. A token bucket: `per_minute` refills, `burst` caps a
    # spend, so no window edge doubles the peak. 0 disables, the code default; the chart sets it.
    # Per process; fleet-wide limits belong at the ingress.
    service_rate_limit_per_minute: float = Field(default=0.0, ge=0)
    service_rate_limit_burst: float = Field(default=30.0, gt=0)
    # Principals the limiter remembers before evicting the least recent; the key is
    # attacker-influenced, so the map must be bounded. Eviction gives that caller one free burst.
    service_rate_limit_max_principals: int = Field(default=10_000, gt=0)
    # Hard ceiling on a request body, refused with 413 before anything reads it
    # (`core.asgi.BodySizeLimit`); otherwise multipart parsing spools the whole body first. Above
    # `attachment_max_bytes` to fit the multipart envelope. 0 disables.
    service_max_request_bytes: int = Field(default=4_000_000, ge=0)
    # The three uvicorn transport bounds, read by `deploy/entrypoint.sh`; none can be imposed from
    # inside the ASGI app. `keepalive_seconds` reclaims idle connections; `max_header_bytes` bounds
    # the request line plus headers. `max_connections` (`--limit-concurrency`) counts every open
    # socket and answers 503 before routing, including to `/healthz`, so hitting it gets the pod
    # killed. Startup therefore requires it to cover streams + turns (with watchers) + waiting
    # messages + `service_connection_headroom`.
    service_max_connections: int = Field(default=512, gt=0)
    # Sockets kept above what the caps can occupy: kubelet probes, the scrape, and a browser's
    # ordinary requests while its streams are open.
    service_connection_headroom: int = Field(default=64, gt=0)
    service_keepalive_seconds: int = Field(default=15, gt=0)
    service_max_header_bytes: int = Field(default=32_768, gt=0)
    # Wall-clock bound on one streamed turn, i.e. how long it holds its admission permit; without it
    # a hung model or slow-reading client pins a permit. On expiry the client gets one user-safe
    # error event.
    service_turn_timeout_seconds: float = Field(default=600.0, gt=0)
    # Wall-clock bound on one send to an SSE client. A client that stops reading blocks the
    # transport, where the turn timeout cannot convert to a clean teardown; sse-starlette closes the
    # body iterator in the serving task, running the turn's own teardown. Far below the turn timeout
    # and far above `service_sse_ping_seconds`.
    service_sse_send_timeout_seconds: float = Field(default=60.0, gt=0)
    # Whether a client disconnect detaches from a running turn (it completes into the transcript;
    # Stop is `POST /sessions/{id}/turn/stop`) rather than cancelling it. The cost, an abandoned
    # turn billed whole, is bounded by the loop cap and turn timeout.
    service_turn_survives_disconnect: bool = True
    # How long a stop from an unloading page (`?reason=unload`) waits before cancelling, so a reload
    # can reattach (`GET /sessions/{id}/turn/stream`). A second unload stop does not move the
    # deadline. 0 makes unload stops immediate.
    service_turn_unload_grace_seconds: float = Field(default=20.0, ge=0, le=300)
    # Unload stops one turn may defer; beyond this they are immediate, so a reload loop cannot keep
    # a turn alive.
    service_turn_unload_grace_max_deferrals: int = Field(default=3, ge=1)
    # Turn and token budgets against runaway cost. When enabled, the front door meters each turn's
    # reported usage and counts turns per session and per user, refusing (429) a turn over a cap. A
    # cap of 0 is unlimited on that dimension. Token metering reads `usage_metadata`, so a provider
    # reporting none meters 0. Off by default.
    budget_enabled: bool = False
    budget_max_turns_per_session: int = Field(default=100, ge=0)
    budget_max_tokens_per_session: int = Field(default=2_000_000, ge=0)
    budget_max_turns_per_user: int = Field(default=1000, ge=0)
    budget_max_tokens_per_user: int = Field(default=20_000_000, ge=0)
    # Largest conversation a turn may be admitted onto, in bytes of the stored `messages` blob
    # (`agent/checkpointer.stored_thread_bytes`). A memory bound, independent of `budget_enabled`:
    # every turn loads its whole thread. `tests/test_deploy_chart.py` holds the pod limit against
    # `service_max_concurrent_turns` threads of this size. 0 disables.
    session_max_thread_bytes: int = Field(default=1536 * 1024, ge=0)
    # Distinct users the in-process budget tracker keeps counters for; the least recently active is
    # evicted. Per-session counters are bounded by `service_max_live_sessions`.
    budget_max_tracked_users: int = Field(default=10_000, gt=0)
    # Rolling window for the durable per-user counters (`api/budget_store.py`), anchored at a
    # principal's first turn in the window. Durable counting engages exactly when `session_store ==
    # "postgres"`; there is no separate flag.
    budget_window_hours: float = Field(default=24.0, gt=0)
    # Fraction of any cap at which `chemclaw_budget_warnings_total` increments and a WARNING names
    # the scope, once per turn (from `record`). Reaches metrics and logs, not the chemist. 0
    # disables; 1.0 is excluded because `_near` (`used >= cap * f and used < cap`) would never fire.
    budget_warn_fraction: float = Field(default=0.8, ge=0, lt=1)
    # Job push-back: a finished Temporal job writes a `session_events` row and the front door tails
    # the table to wake the owning session. This is the tailer's poll interval.
    session_event_poll_seconds: float = Field(default=2.0, gt=0)
    # Concurrent push-back event streams (`GET /sessions/{id}/events`) per user, per process; each
    # polls the database for its lifetime. Past the cap, 429. Exact fleet-wide counting is not worth
    # a durable write per stream.
    service_max_event_streams_per_user: int = Field(default=5, gt=0)
    # Keepalive interval for idle SSE streams, under the ~60 s idle timeout of typical proxies and
    # load balancers.
    service_sse_ping_seconds: int = Field(default=15, gt=0)
    # Event streams across all users on this process, refused with 429; binds only in aggregate.
    service_max_event_streams_total: int = Field(default=200, gt=0)
    # How long `/readyz` reuses its connector sweep; the route is unauthenticated, so uncached it is
    # a fan-out anyone can trigger. Connector states are reported, not gating. 0 probes every
    # request.
    service_readiness_cache_seconds: float = Field(default=5.0, ge=0)
    # Statement budget for the readiness `SELECT 1`, separate from `pg_statement_timeout_seconds` so
    # "not ready" is answered quickly; well under the kubelet's probe timeout.
    service_readiness_db_timeout_seconds: float = Field(default=2.0, gt=0)

    @field_validator("service_cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        """Refuse `*`, the one entry that makes this allow-list allow everything.

        `api/middleware._add_cors` passes entries to `CORSMiddleware` verbatim. Checked per entry,
        because `*` is as dangerous inside a list.
        """
        for origin in (part.strip() for part in value.split(",")):
            if origin == "*":
                raise ValueError(
                    "service_cors_origins may not contain '*': that allows every browser origin, "
                    "which is the opposite of an allow-list. Leave it empty for no cross-origin "
                    "access (the default, and what a same-origin embedded UI needs), or name the "
                    "origins that may call this API."
                )
        return value

    # Env var holding the read-only MCP face's bearer; the middleware fails closed when it is unset.
    mcp_face_token_env: str = "CHEMCLAW_MCP_FACE_TOKEN"
