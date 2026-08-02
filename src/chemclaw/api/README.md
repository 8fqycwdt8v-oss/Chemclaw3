# `chemclaw.api` — the front door

**Responsibility:** the ASGI surface that runs the agent for a real caller — HTTP + SSE, behind
OIDC. `create_app` (`app.py`) is the composition root and the only factory: middleware, `app.state`
seeding, gauges and router inclusion, no route code. The routes live in `routes/` (one module per
resource — see `routes/README.md`), reading process state through `state.py`'s typed
`state(request)` accessor; `deps.py` holds the authorization dependencies (`CurrentUser`,
`CurrentSession`, hold/proposal gates); `schemas.py` the wire shapes; `middleware.py` the
cross-cutting HTTP armor. `runner.py` owns the per-turn lifecycle (build or
resolve the agent, open the MCP tool sessions, stream events, close them), with the three pure
readers that lifecycle uses beside it — `runner_trace.py` (reassemble a streamed tool call),
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

Run it: `uvicorn chemclaw.api.app:create_app --factory --port 8080`, or `make chat` for the
terminal path to the same agent.
