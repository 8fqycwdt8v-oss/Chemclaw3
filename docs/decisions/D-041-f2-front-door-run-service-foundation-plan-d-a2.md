# D-041 — F2: front-door run service (foundation-plan D-A2)

**Context.** The decisive gap: the agent was only ever *built* (in tests), never *run*. A chemist
needs a browser surface, and someone has to own the MCP tool lifecycle the constructor leaves open.

- **One ASGI service.** `service/app.py::create_app` (FastAPI) builds/holds one agent per process and
  a per-session `AgentSession`; `service/runner.py::run_turn` opens the MCP contexts for the turn
  (`AsyncExitStack` over `agent.mcp_tools` — the lifecycle the agent docstring delegates to its
  caller), runs `agent.run(..., stream=True, session=…)`, and translates streamed updates into typed
  events. When the harness is on, the *same* `agent.run` drives its completion loop — no separate
  driver. The agent factory is injectable, so the whole HTTP surface is tested with a fake streaming
  agent (no live model/MCP/creds).
- **Typed turn contract.** `service/events.py` is a discriminated union on `type`
  (plan/tool_call/token/job_started/approval_request/answer/error) serialized one-per-SSE-line, so
  the web UI now and Slack/mobile later render one contract, not a bespoke stream each. Tool calls are
  extracted **duck-typed** from update contents (MAF's function-call content class is not a stable
  export), keeping the runner version-robust. A failed turn becomes one user-safe `ErrorEvent`, never
  a mid-stream 500 or a leaked trace.
- **Thin built-in web chat** (`service/static/`), not an adopted generic UI — full control over plan
  display, tool trace, citations, and the approval affordances a generic chat UI can't render. The
  messages endpoint is POST+SSE, so the page reads the response body as a stream (native `EventSource`
  is GET-only). Config: `service_host`/`service_port`/`service_cors_origins` (empty CORS = safe
  default). Deps: `fastapi`/`uvicorn`/`sse-starlette`.
- **Deferred within F2:** emitting `PlanEvent` from harness todo state and a real `JobStartedEvent`
  when a tool launches a Temporal job — both land with F3's durable session + job→session push-back.
  Identity (Entra OIDC on every non-health route) is F4.
