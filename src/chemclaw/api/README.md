# `chemclaw.api` — the front door

**Responsibility:** the ASGI surface that runs the agent for a real caller — HTTP + SSE, behind
OIDC. `create_app` (`app.py`) is the composition root and the only factory: middleware, `app.state`
seeding, gauges and router inclusion, no route code. The routes live in `routes/` (one module per
resource — see `api/routes/README.md`), reading process state through `state.py`'s typed
`state(request)` accessor; `deps.py` holds the authorization dependencies (`CurrentUser`,
`CurrentSession`, hold/proposal gates); `schemas.py` the wire shapes; `middleware.py` the
cross-cutting HTTP armor. `runner.py` owns the per-turn lifecycle (build or
resolve the agent, open the MCP tool sessions, stream events, close them), with the three pure
readers that lifecycle uses beside it — `runner_trace.py` (the events a tool call and its result become),
`runner_usage.py` (the turn's token arithmetic), `runner_answer.py` (score the final answer);
`auth.py` is the single
authorization gate; `events.py` the SSE envelope; `budget.py` the per-turn cost meter. The
Prometheus registry the `/metrics` route renders is **not** here — it is `core/metrics.py`, because
every process has something to count and only one of them is the front door.

## The boundary against `agent/`

`agent/` decides *what the assistant does*; `api/` decides *who is allowed to ask and how the
answer gets out*. Nothing here reasons about chemistry, and nothing in `agent/` knows it is being
served over HTTP — which is what lets the CLI (`chemclaw.cli.chat`) drive the same agent with no
web stack at all.

## Durability is not here

A turn is a request. Anything long-running is a Temporal job: the tool returns a `job_id`
immediately and the result arrives later, pushed back into the session (F3). If it survives a pod
restart, it is in `durable/`; if it dies with the connection, it belongs here.

Run it: `CHEMCLAW_SERVICE_HOST=127.0.0.1 CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true uvicorn
chemclaw.api.app:create_app --factory --host 127.0.0.1 --port 8080`, or `make chat` for the terminal
path to the same agent (which exports the third variable for you).

**Three facts, not two, and the third is why this line changed.** `CHEMCLAW_SERVICE_HOST` is what
the app is told, `--host` is the socket, and unauthenticated dev needs both on loopback:
`create_app` refuses to boot on the first, and `require_principal` refuses a request that arrived on
the second. `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY` is a different question entirely — where this
process's *model gateway* is — and it became necessary when
`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard` moved that check out of
`api/middleware.py` and stopped exempting a loopback bind. The old exemption meant a deployment
bound to loopback never had its gateway checked at all; the dev posture is now stated rather than
inferred from a socket, and this paragraph said two of the three for as long as it stood.
