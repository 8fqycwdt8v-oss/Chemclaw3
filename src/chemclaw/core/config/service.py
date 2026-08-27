"""The front-door run service (plan Phase F2/F3): binding, limits, sessions, budgets.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class ServiceSettings(BaseSettings):
    """The front-door run service (plan Phase F2/F3): binding, limits, sessions, budgets.

    Grouped because these knobs all guard the one ASGI trust boundary: how the server binds,
    what a request may cost (size, concurrency, wall-clock, token budgets), and how durable
    sessions + job push-back reach the browser.
    """

    # The ASGI service that actually *runs* the agent for a chemist: it builds the agent, opens
    # the MCP tool lifecycle for the turn, streams the response, and serves the browser chat
    # surface. `service_host`/`service_port` bind the server (the OpenShift Route front-ends it,
    # F6). `service_cors_origins` is a comma-separated allow-list for browser origins that may
    # call the API (empty = none, the safe default; a same-origin embedded UI needs none). These
    # are the only front-door knobs; identity/OIDC is layered on in F4. Binds all interfaces
    # inside the container; the OpenShift Route + NetworkPolicy gate ingress.
    service_host: str = "0.0.0.0"
    service_port: int = Field(default=8080, gt=0)
    # Explicit opt-in to boot *unauthenticated on a non-loopback bind* (SEC-2). With
    # `entra_required` False every request runs as the shared dev principal with all
    # authorization gates open — safe only behind loopback. The front door refuses to start in
    # that mode on an exposed interface unless this is set, so an exposed unauthenticated
    # deployment is a conscious decision (one loud env var), never a default. Loopback dev and
    # Entra-enforced deployments never need it.
    service_allow_insecure: bool = False
    # A comma-separated allow-list of browser origins. Empty (the default) is *no* cross-origin
    # access; `*` is refused below, because it is the one value that turns an allow-list into no
    # list at all and there is no deployment that needs it — a same-origin embedded UI needs none,
    # and a browser client that does need access has an origin to name.
    service_cors_origins: str = ""
    # How many uvicorn worker *processes* the container starts (`deploy/entrypoint.sh`). One
    # asyncio event loop saturates one CPU, and a load test measured throughput flat at
    # ~1.18 turns/s from 10 to 50 concurrent users on a 4-CPU box — a single-loop ceiling.
    #
    # The per-session turn guard is no longer among the reasons to keep this at 1: under
    # `session_store="postgres"` a turn takes a leased row in `session_turns`, so two turns on one
    # session cannot be admitted by two processes (D-121). What is still per-process is
    # *capability*, not correctness — the admission semaphore (so the deployment's real cap is
    # this many times `service_max_concurrent_turns`), the event-stream caps, uploaded attachments
    # and harness todos, all of which live in one process's memory and are therefore invisible to
    # a sibling worker. A chemist who uploads a file and then asks about it needs both requests on
    # the same process, and no ingress can pin below the pod. So the supported way to use more
    # CPU is still `replicas` with session affinity at the Route; raise this only for a
    # deployment that does not use attachments or the harness. Under `session_store="memory"`
    # there is no shared claim at all and this must stay 1.
    service_uvicorn_workers: int = Field(default=1, gt=0)
    # How long a turn's claim on its session (`session_turns`, D-121) stays valid before another
    # process may take it. A lease rather than a lock because a lock would have to be held on a
    # pooled connection for the turn's whole duration; the cost of a lease is that exclusion holds
    # only while the holder is scheduled often enough to refresh it, which the front door does
    # every third of this interval. Sized well above the worst measured event-loop scheduling
    # delay (~10 s under 50 concurrent users) and well below the wall-clock turn timeout, so a
    # crashed worker frees its session in about a minute rather than at the next restart.
    service_turn_claim_lease_seconds: float = Field(default=60.0, gt=0)
    # Max characters accepted in one chat message at the front door (SEC-4). Bounds the request
    # body at the trust boundary so an oversized POST is a clean 422, not an unbounded
    # allocation. Generous for a real message (~25k tokens); raise it for a workflow that posts
    # more.
    service_max_message_chars: int = Field(default=100_000, gt=0)
    # Response security headers on the browser surface (SEC-5). When on (the safe default),
    # every response carries a Content-Security-Policy scoped to the self-served chat UI (self +
    # one inline <style> block + data: images), X-Content-Type-Options: nosniff,
    # X-Frame-Options: DENY, and Strict-Transport-Security. Off is only for a deployment
    # fronting its own header policy at the ingress/Route. HSTS is inert over plain-HTTP dev, so
    # leaving this on locally is harmless.
    service_security_headers: bool = True

    # Durable session store (plan Phase F3). The agent's conversation history must survive a pod
    # restart, so a session is resumable. `memory` keeps the classic in-process provider
    # (dev/test); `postgres` persists each turn's messages to `session_messages` keyed by
    # session id, so a fresh process over the same DSN resumes the thread. **Session state is
    # not Temporal job state** — it is the conversation layer (D-002), and the table is a read
    # model rather than the turn's state. `session_store_dsn` lets it point at a database other
    # than the
    # calculation/fingerprint DSN; empty falls back to `postgres_dsn` (one database in the
    # simple deployment).
    session_store: Literal["memory", "postgres"] = "memory"
    session_store_dsn: str = ""
    # Cap on the front door's in-process live-session cache (COR-3). The service holds the live
    # AgentSession object per session id; without a bound this map grows for the pod's whole
    # lifetime. When the cap is exceeded the least-recently-used session is evicted — its
    # durable history survives in the session store, only the in-process handle is dropped.
    # Sized generously for concurrent chemists; raise it for a busier front door.
    service_max_live_sessions: int = Field(default=1000, gt=0)
    # Cap on how many of a caller's sessions `GET /sessions` returns, newest first. A chemist
    # who has used the system for a year owns thousands of session rows, and the route exists to
    # populate a sidebar — an unbounded list would be a slow query rendering a list nobody
    # scrolls.
    service_max_listed_sessions: int = Field(default=100, gt=0)
    # Admission control on concurrent agent turns (AG-15). Each turn holds one permit for its
    # whole streamed run, so at most this many turns hit the shared internal LLM endpoint at
    # once; a turn that cannot get a permit within the admission timeout is shed with 503
    # (retry) rather than piling onto a saturated endpoint. Tune to the endpoint's real
    # throughput budget — the default is deliberately conservative. Health and push-back streams
    # are not gated (they are not LLM-bound).
    service_max_concurrent_turns: int = Field(default=8, gt=0)
    service_turn_admission_timeout_seconds: float = Field(default=5.0, gt=0)
    # Threads kept *above* whatever this process's own admission caps can occupy, in the one
    # `asyncio.to_thread` pool they all share (`core/executor.py`). They exist for the calls that
    # are microseconds long and must never wait behind a corpus parse or an embedding: bearer-token
    # validation on every request, a readiness probe, an SSE reconnect. Without a sized pool the
    # loop's stock default is `min(32, cpu_count + 4)` — 8 on a 4-CPU pod, which is exactly
    # `service_max_concurrent_turns`, so the admission cap could fill the pool on its own and
    # authentication latency became a function of corpus size (measured: a queued short call waited
    # 0.2 ms at 1 concurrent `load_notes`, 565.5 ms at 8, 813.4 ms at 16). The *reserved* half is
    # derived from the caps that already exist, so raising a cap widens the pool with it; this is
    # the only part an operator tunes, and only if short calls are seen queuing.
    service_thread_pool_headroom: int = Field(default=8, gt=0)
    # The two numbers that turn the *per-process* cap above into the load the shared LLM endpoint
    # actually sees, because a process cannot discover either of them.
    #
    # The guard is per-process by design and stays that way: SCALE-1 rejected a fleet-wide admission
    # counter because bounding a *resource* is not worth a durable write and a heartbeat on every
    # turn. But that decision left the real ceiling — `replicas × uvicorn workers × the cap above` —
    # written down nowhere and checked by nothing. With `maxReplicas: 6` the shipped chart admits 48
    # concurrent turns against an endpoint sized by whoever set `8`, and an operator raising the cap
    # to "use the box better" multiplies the fleet's demand sixfold without touching anything named
    # fleet. That is the gap: not that the guard is per-process, but that nobody states the product.
    #
    # `fleet_replicas` is how many front-door pods this deployment may reach — the chart derives it
    # from `autoscaling.maxReplicas` (or `service.replicas` when the HPA is off), so it is the same
    # number the HPA obeys and cannot drift from it. 1 is the honest default for a CLI, a test or a
    # single-pod dev run.
    #
    # `fleet_max_concurrent_turns` is the ceiling the LLM endpoint's throughput budget permits,
    # declared by the operator who knows it. When it is set, the validator refuses a configuration
    # whose product exceeds it, at startup, in every pod — so the multiplication fails loudly at
    # deploy time instead of silently at 3am. 0 = undeclared, which is the code default for the same
    # reason `budget_enabled` and the rate limiter are off in code: a dev run has no fleet.
    service_fleet_replicas: int = Field(default=1, gt=0)
    service_fleet_max_concurrent_turns: int = Field(default=0, ge=0)
    # Per-principal request budget (`api/rate_limit.py`), spent inside `require_principal` so it
    # covers every authenticated route and none of the probes. The two guards above are scoped to
    # *turns*, so a caller holding them at zero could still drive `/proposals`, `/jobs`,
    # `/schedules` and `/sessions` as fast as the network allowed — every one of which does real
    # work against Temporal or Postgres. A loop with no LLM call in it was free.
    #
    # A token bucket: `per_minute` is the sustained refill and `burst` the ceiling a caller may
    # spend at once. A fixed window would let someone spend a whole allowance at its last
    # millisecond and the next at its first, so the observed peak is twice the configured rate at
    # the moment the system can least absorb it.
    #
    # 0 disables, and that is the code default for the same reason `budget_enabled` is off in code
    # and on in the chart (REV-16): a CLI, a test and a single-user dev run have no reason to be
    # throttled, and a limiter that fires there is one people switch off everywhere.
    #
    # Per process, like `service_max_concurrent_turns`, and with the same caveat: `maxReplicas`
    # multiplies the real ceiling, and a fleet-wide limit belongs at the ingress.
    service_rate_limit_per_minute: float = Field(default=0.0, ge=0)
    service_rate_limit_burst: float = Field(default=30.0, gt=0)
    # How many principals the limiter remembers before evicting the least recently seen. A map
    # keyed by caller identity is the classic unbounded-growth bug (fixed three times in this
    # codebase, most recently for metric label series, D-152), and here the key is
    # attacker-influenced — minting tokens for many `oid`s is exactly the way around a per-principal
    # limit. Eviction costs that caller one free burst and costs the process nothing.
    service_rate_limit_max_principals: int = Field(default=10_000, gt=0)
    # Hard ceiling on a request body, refused with 413 *before* anything reads it
    # (`core.asgi.BodySizeLimit`). `attachment_max_bytes` was the only size check and it runs inside
    # `parse_attachment` — by then Starlette's multipart parser has already written the whole body
    # to a spooled temp file (RAM to 1 MB, then the pod's ephemeral disk), so a 5 GB upload was
    # ingested in full and then refused. Above `attachment_max_bytes` because a multipart envelope
    # carries boundaries and headers around the file; 0 disables.
    service_max_request_bytes: int = Field(default=4_000_000, ge=0)
    # The three uvicorn transport bounds, read by `deploy/entrypoint.sh`. Settings rather than
    # literals in the script for the usual reason — every threshold is one config value — and here
    # for a second one: they are the only knobs in the system an operator must tune *against the
    # connection count*, and burying them in a shell script is where they would never be found.
    #
    # None of these can be imposed by the application: by the time a request reaches an ASGI app,
    # uvicorn has accepted the socket and parsed the headers. `max_connections` bounds sockets, not
    # turns — deliberately far above `service_max_concurrent_turns`, since a connection waiting for
    # an admission permit or holding an SSE stream is doing nothing expensive; it is the backstop,
    # not the policy. `keepalive_seconds` reclaims an idle connection's slot. `max_header_bytes`
    # bounds the request line plus headers, without which a client can dribble an unbounded header
    # block for as long as it likes.
    service_max_connections: int = Field(default=256, gt=0)
    service_keepalive_seconds: int = Field(default=15, gt=0)
    service_max_header_bytes: int = Field(default=32_768, gt=0)
    # Wall-clock bound on one streamed turn — how long a turn may hold its admission permit. The
    # admission timeout only bounds the *wait* for a permit; without this, a hung model stream
    # or a deliberately slow-reading SSE client pins a permit indefinitely, and a handful of
    # such streams collapses the whole front door's capacity (every other turn is shed 503). On
    # expiry the client gets one user-safe error event and the permit is released. Generous for
    # a real turn (an async QM job is submitted, not awaited, within the turn), finite against a
    # stall.
    service_turn_timeout_seconds: float = Field(default=600.0, gt=0)
    # Wall-clock bound on one *send* to an SSE client, which is a different stall from the one
    # above and cannot be caught by it. `service_turn_timeout_seconds` is an `asyncio.timeout`
    # entered inside the turn's generator, so it converts to an error event only while that
    # generator is executing; a client that has stopped reading parks the generator at a `yield`
    # and blocks the *transport* instead, where the same cancellation tears the stream down with
    # no teardown of the turn at all (it is left to the async-generator garbage collector, in a
    # context the turn's contextvar tokens do not belong to). sse-starlette answers this bound by
    # closing the body iterator in the task serving the stream, which runs the turn's own
    # teardown. Deliberately far below the turn timeout — a bound that is not reached first
    # catches nothing — and far above `service_sse_ping_seconds`, so an idle-but-healthy stream
    # is never cut.
    service_sse_send_timeout_seconds: float = Field(default=60.0, gt=0)
    # Whether a client disconnect detaches from a running turn (the turn completes; its answer
    # lands in the transcript; Stop is the explicit `POST /sessions/{id}/turn/stop`) or cancels it
    # as every disconnect used to (`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`). On by
    # default because losing a 10-minute multi-tool turn to a Wi-Fi handoff is the worse failure;
    # the cost — an abandoned turn runs to completion and is billed whole — is bounded by the loop
    # cap and `service_turn_timeout_seconds`, and a deployment that prefers cost over completion
    # turns this off and gets the old posture exactly.
    service_turn_survives_disconnect: bool = True
    # Turn/token budgets — the runaway-cost guard (service.budget). A single turn is already
    # iteration-capped (`harness_max_loop_iterations`), but nothing caps the *number*
    # of turns, so a client or an automated push-back loop could accumulate unbounded LLM spend.
    # When `budget_enabled`, the front door meters each turn's reported token usage and counts
    # turns per session and per user, refusing (HTTP 429) a turn that would exceed a cap. Caps
    # are per running process and best-effort — they reset on restart, bounding a live process's
    # runaway (the missing ceiling above the per-turn loop cap), not a durable rolling-window
    # quota (deferred). A cap of 0 means unlimited on that dimension, so a deployment can enable
    # just the guard it wants; the defaults are generous for a real chemist but finite against a
    # loop. Token metering reads each streamed chunk's `usage_metadata`, so a provider reporting
    # no usage meters 0 and the turn caps bind. Off by default.
    budget_enabled: bool = False
    budget_max_turns_per_session: int = Field(default=100, ge=0)
    budget_max_tokens_per_session: int = Field(default=2_000_000, ge=0)
    budget_max_turns_per_user: int = Field(default=1000, ge=0)
    budget_max_tokens_per_user: int = Field(default=20_000_000, ge=0)
    # Cap on distinct users the in-process budget tracker keeps counters for. The tracker lives
    # for the pod's lifetime, so without a bound its per-user map grows with every principal
    # ever seen (a slow leak); past the cap the least-recently-active user's counters are
    # evicted (reset) — acceptable for a best-effort guard whose durable rolling-window quota is
    # a conscious deferral. The per-session map is bounded by `service_max_live_sessions` (the
    # session lifecycle bound).
    budget_max_tracked_users: int = Field(default=10_000, gt=0)
    # Job→session push-back (plan F3-T2/T3): a finished Temporal job writes a `session_events`
    # row; the front door tails the table and wakes the owning session (appending the result,
    # flipping the `awaiting` todo) instead of the user polling. This is the tailer's poll
    # interval — a LISTEN/NOTIFY-free fallback that is simple and correct; lower it for snappier
    # wake-ups.
    session_event_poll_seconds: float = Field(default=2.0, gt=0)
    # Cap on concurrent push-back event streams (`GET /sessions/{id}/events`) per user, **per
    # process**. The turn semaphore only guards POSTed turns; each event stream polls the database
    # for its whole lifetime, so without a bound one user (or a pile of abandoned tabs) can
    # accumulate hundreds of forever-polling streams and exhaust Postgres connections for
    # everyone. A real client needs one stream per open session view; past the cap the request is
    # refused with 429. Per process, not per deployment: a user spread over `replicas ×
    # service_uvicorn_workers` processes can hold that multiple. Deliberately left that way —
    # this bounds a *resource*, and paying a durable write plus a heartbeat per stream to make an
    # approximate ceiling exact would cost more than the thing it protects.
    service_max_event_streams_per_user: int = Field(default=5, gt=0)
    # How often an idle SSE stream sends a keepalive comment frame (D-159). Neither stream set
    # one, so a long tool wait had no signal of any kind on the wire: the turn stream can be
    # silent for the length of an inline calc job or an MCP `request_timeout`, and the push-back
    # stream is silent by nature until a job lands. Anything between the browser and the pod that
    # reaps idle connections — a proxy, a load balancer, a phone's radio — was free to drop it,
    # and the client could not tell that from a slow answer. Comfortably under the 60s such
    # intermediaries typically use.
    service_sse_ping_seconds: int = Field(default=15, gt=0)
    # The same cap across *all* users on this process. The per-user cap alone bounds one client;
    # it does not bound the pod, so 50 concurrent chemists at the per-user cap is 250 forever-
    # polling streams on one event loop — each a task and a periodic pooled query. This is the
    # pod-level ceiling, refused with the same 429. Sized as a generous multiple of the per-user
    # cap so it only binds in the aggregate case the per-user cap cannot see.
    service_max_event_streams_total: int = Field(default=200, gt=0)
    # How long `/readyz` may reuse its connector sweep. The route is unauthenticated by necessity
    # (a kubelet cannot present a token) and the kubelet probes every 10 s per pod, so an uncached
    # sweep is an N-connector HTTP fan-out that any caller can trigger at will. The connector
    # states are *reported*, never gating, so the only cost of caching is that a reported state
    # can be up to this stale. 0 probes on every request (the pre-cache behavior).
    service_readiness_cache_seconds: float = Field(default=5.0, ge=0)
    # The database probe's own statement budget, deliberately not `pg_statement_timeout_seconds`.
    # A `SELECT 1` that has not answered in two seconds has answered: the store is not serving.
    # Keeping this separate from the store timeout is what makes probing safe at all — the argument
    # against holding readiness on the database (`connectors/server.py`) is an argument against an
    # *unbounded* wait, and a readiness route exists to say "not ready" quickly. Well under the
    # kubelet's own probe timeout, so the answer arrives rather than being cut off as a timeout
    # whose cause the pod never logs.
    service_readiness_db_timeout_seconds: float = Field(default=2.0, gt=0)

    @field_validator("service_cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        """Refuse `*`, the one entry that makes this allow-list allow everything.

        `api/middleware._add_cors` splits on commas and passes the result to `CORSMiddleware`
        verbatim, so `*` reached `allow_origins=["*"]` with nothing between. The harm is bounded
        today — `allow_credentials` is left False and this API authenticates with a bearer rather
        than a cookie, so a hostile origin cannot ride a user's session — but that bound rests on
        two properties of *other* modules, and neither is pinned anywhere. A guard rail on the knob
        itself does not.

        Checked on every entry rather than on the whole string, because the dangerous value is just
        as dangerous in a list beside real origins.
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
