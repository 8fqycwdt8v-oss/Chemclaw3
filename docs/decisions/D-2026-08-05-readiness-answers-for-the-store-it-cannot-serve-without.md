# D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without — readiness answers for the store it cannot serve without

**Status:** accepted · **Date:** 2026-08-05

## Context

`GET /readyz` built the agent and probed every enabled connector. It never touched Postgres.

Under `session_store="postgres"` — what `values.yaml` sets, so what every real deployment runs —
the front door cannot serve a single turn without the database:

- `SessionTurnClaims.claim` takes the cross-process turn lease (D-121); no claim, no turn.
- `PostgresHistoryProvider.get_messages` is the conversation.
- `SessionOwnerStore.lookup` is what authorizes a reattach after a pod restart (D-066).
- `PostgresAuditSink` is the GxP trail, and it is durable by default (D-122).

So the route probed the things that cost *a capability* and skipped the thing that costs *the
service*. An unreachable connector means the agent answers without one tool — which is exactly why
its state is reported rather than gating, and why `connectors_required` exists for deployments that
disagree. An unreachable database means every request 503s from `_database_unavailable`, while the
pod goes on telling the kubelet it is ready to receive them.

Nothing recorded this as a decision. There is no ADR, no backlog row, and no comment on the route.
`connectors/server.py` does carry the nearest thing to an argument, about its own startup:

> `on_start` is launched inside the pool ... an unreachable one would hold readiness for the whole
> pool timeout

That is a real concern and it is an argument against an **unbounded** wait, not against asking the
question.

## Decision

**`/readyz` probes Postgres and gates on it, under `session_store="postgres"` only.**

A `SELECT 1` on a borrowed connection, bounded by `service_readiness_db_timeout_seconds` (2 s) —
its **own** budget, deliberately not `pg_statement_timeout_seconds` (30 s). This is what makes the
probe safe: a readiness route exists to report "not ready" *quickly*, so a `SELECT 1` that has not
answered in two seconds has answered. The distinction between a bounded probe and an unbounded hold
is the whole of the reply to the objection above.

**Cached on the same `service_readiness_cache_seconds` window as the connector sweep**, for the
same reason: the route is unauthenticated by necessity (a kubelet cannot present a token) and any
caller may hit it at will, so an uncached probe is one database round trip per request from
anywhere.

**503 with a body that names the failure**, rather than an exception. A kubelet reads only the
status; an operator running `curl` reads the reason, and "database unreachable" is the reason.

**`/healthz` is untouched, and this is load-bearing.** Liveness must not follow readiness here.
Restarting every front-door pod because a shared database is down destroys the capacity that would
serve the moment it returns, and a restarted pod is no closer to reaching it. Draining them from the
Route is the entire correct response. `tests/test_service.py` pins both sides of that in one test,
because it is the pair that matters and not either half.

**Not probed under `session_store="memory"`**, where there is no store to answer for — a dev run, a
CLI-shaped deployment and the whole offline test suite would otherwise report unready for lacking a
database none of them use. The probe follows the same switch every other durable-session behaviour
follows (D-122's audit sink, D-121's turn claim, D-066's owner store).

**The pre-probe verdict is `True`.** Readiness must not report a store unreachable on the strength
of never having asked; the kubelet's first probe answers within one interval, and refusing traffic
until then would turn every rollout into a needless gap.

## Consequences

**A database outage now drains the front door instead of black-holing requests into it.** Clients
get a router-level failure with retry semantics rather than 503 bodies from pods the router still
believes in, and `kubectl get pods` shows the cause rather than a fleet that looks healthy while
answering nothing.

**One more thing that can make a pod unready, and it is shared.** Every front-door pod probes the
same database, so a database outage takes the whole Deployment out of the Route at once. That is
correct — none of them can serve — but it means readiness is no longer a purely local signal, and an
operator reading "0/6 ready" should look at the store before the pods. `ChemclawDatabaseUnavailable`
already alerts on the same condition from the other side.

**A slow database is not a down one.** Two seconds is a threshold, and a store degraded to
3-second responses will flap this probe while still technically serving. That is the intended
reading — a front door whose every session lookup takes three seconds is not serving usefully — but
it is a threshold and not a proof, and `service_readiness_db_timeout_seconds` exists so a
deployment that disagrees can say so.

**The workers and connector servers are unchanged.** Their readiness is `is_running` and the MCP
session manager respectively (D-2026-08-01-every-process-carries-its-own-witness), and neither
becomes useless without Postgres in the way the front door does: a worker with no database polls,
fails its activities, and Temporal retries them, which is the designed behaviour.
