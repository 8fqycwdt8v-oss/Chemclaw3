# Implementation Tickets: the foundation build, ticket by ticket

> **Companion to** `docs/foundation-plan.md` (the staged *how*) and `docs/foundation-assessment.md`
> (the *what/why*). This document is the **executable backlog**: every phase F0–F9 broken into
> small, individually-shippable tickets grounded in the **real symbols in this repo**. Each ticket
> names the files it touches, the concrete signatures/config it adds, the tests that prove it, and
> its acceptance bar. Conventions follow `docs/implementation-plan.md`: config-not-magic-numbers
> (`CHEMCLAW_` prefix, one `pydantic-settings` source), an ADR per decision, and `make lint type
> test` green plus the phase **CHECKMATE** (G1–G7) as the done-gate.
>
> **Ticket format**
> - **Goal** — one sentence.
> - **Touch** — files created (＋) / changed (~).
> - **Build** — concrete interfaces, signatures, config keys (real names).
> - **Test** — `tests/…` file + the behavior it proves (real tests, not mocks of our own code).
> - **Done when** — acceptance check.
> - **Deps** — prerequisite ticket ids.
>
> **Reality anchors (verified against the tree):** the single agent constructor is
> `build_agent(chat_client=None, *, actor="unknown", correlation_id=None, audit_sink=None,
> allowed_skills=None) -> Agent` in `agents/chemclaw_agent.py`; its chat client is built by
> `_default_chat_client()` → `AnthropicClient(model=settings.agent_model)`. **There is no
> `create_harness_agent` and no production run-loop anywhere** — the agent is only built in
> `tests/test_agent.py`; the MCP lifecycle (`async with *agent.mcp_tools`) is only exercised in
> `tests/test_mcp_transport.py`. Config is `Settings` (`env_prefix="CHEMCLAW_"`, `extra="forbid"`).
> These are the seams the tickets below move.

---

## Legend / global conventions (apply to every ticket)

- **New config keys** land in `chemclaw/config.py` `Settings`, `CHEMCLAW_`-prefixed, typed, with a
  default that is safe for local dev; `extra="forbid"` means every new key must be declared there.
- **New SQL** lands in `infra/sql/NNN_*.sql` (next number after `001_calculation_results.sql`) and is
  wired into the existing migration ledger (`make db-migrate`, D-034).
- **New durable work** uses the existing two task queues (`hpc-jobs`, `background-jobs`); no new
  durable store — durability stays in Temporal (D-002).
- **New knowledge** goes through the PR-gate (`propose_knowledge_note` / `NoteSubmitter`); **serving
  copies** (indices) are not gated (D-018).
- **Every ticket ends green:** `make lint type test`, plus new tests it introduces.

---

# Phase F0 — LLM provider seam + tool-calling spike ⭐ blocks everything

Goal of the phase: the agent talks to the internal OpenAI-compatible endpoint with a **single
generic API credential** (not Entra), and we have a documented verdict on whether the internal model
can drive MAF tool-calling + the harness todo tools.

### F0-T1 — Provider config block
- **Goal:** add the provider-selection config the adapter reads; keep Anthropic as a dev fallback.
- **Touch:** ~`chemclaw/config.py`.
- **Build:** add fields
  `llm_provider: Literal["openai_compatible", "anthropic"] = "openai_compatible"`,
  `llm_base_url: str = ""`, `llm_model: str = "gpt-oss"` *(placeholder)*,
  `llm_api_key: str = ""` *(the one generic credential)*,
  `llm_tls_ca_bundle: str = ""` *(path; empty = system store)*,
  `llm_timeout_seconds: float = Field(default=60.0, gt=0)`,
  `llm_max_retries: int = Field(default=3, ge=0)`,
  `llm_temperature: float = Field(default=0.0, ge=0)`,
  `llm_max_tokens: int = Field(default=4096, gt=0)`.
  Add a `model_validator(mode="after")` `_llm_provider_config`: if `llm_provider=="openai_compatible"`
  require non-empty `llm_base_url`. **Keep `agent_model`** for now (Anthropic dev path) — do not delete
  until F1 fully cuts over.
- **Test:** ~`tests/test_config.py` — asserts defaults, that `openai_compatible` with empty
  `llm_base_url` raises `ValidationError`, and that `CHEMCLAW_LLM_BASE_URL` env override lands.
- **Done when:** config round-trips; validator fires.
- **Deps:** —

### F0-T2 — Provider adapter (the one place a client class is imported)
- **Goal:** replace `_default_chat_client()` with a config-selected factory; no client class imported
  at a call site.
- **Touch:** ＋`agents/llm_provider.py`, ~`agents/chemclaw_agent.py`.
- **Build:** `def build_chat_client() -> Any` in `agents/llm_provider.py`:
  - `openai_compatible` → MAF's OpenAI-compatible chat client (`agent_framework.openai` `OpenAIChatClient`
    or `OpenAIResponsesClient`, whichever MAF exposes) constructed with `base_url=settings.llm_base_url`,
    `api_key=settings.llm_api_key`, `model=settings.llm_model`, timeout/retries from config, and an
    `httpx` client honoring `settings.llm_tls_ca_bundle` when set.
  - `anthropic` → the current `AnthropicClient(model=settings.agent_model)` (unchanged dev path).
  - Raise a clear `RuntimeError` naming the missing config (mirrors today's `ANTHROPIC_API_KEY` guard).
  Rewire `_default_chat_client()` to `return build_chat_client()` (or inline-delete it and have
  `build_agent` call `build_chat_client()`); **no `from agent_framework.anthropic import …` outside
  this module.**
- **Test:** ＋`tests/test_llm_provider.py` — with `llm_provider="anthropic"` and a fake key returns the
  Anthropic client; with `openai_compatible` + a base_url returns the OpenAI-compatible client of the
  expected type; missing base_url raises. (No network — assert on constructed type/attrs, monkeypatching
  the MAF client classes.)
- **Done when:** switching provider is one config change; grep shows the Anthropic import only in
  `agents/llm_provider.py`.
- **Deps:** F0-T1.

### F0-T3 — Streaming + generation params
- **Goal:** thread streaming and temperature/max-tokens/stop from config so F2's front door can stream.
- **Touch:** ~`agents/llm_provider.py` (pass `temperature`, `max_tokens`), confirm MAF `Agent.run`
  streaming shape for the chosen client (`run_stream`/async iterator).
- **Test:** ~`tests/test_llm_provider.py` — the constructed client carries the configured
  temperature/max_tokens; a thin streaming smoke test against a fake client yields chunks.
- **Done when:** params are config-driven, no literals.
- **Deps:** F0-T2.

### F0-T4 — Tool-calling capability spike (the H0 risk) 🔬
- **Goal:** **evidence** that the internal model can (a) select+call MAF function tools, (b) drive the
  harness todo tools and plan/execute transition, (c) return the structured outputs the agent needs.
- **Touch:** ＋`scripts/spike_toolcalling.py` (a runnable probe, not shipped in the request path),
  ＋`docs/spikes/f0-toolcalling.md` (the verdict).
- **Build:** a script that points `build_chat_client()` at the internal endpoint and runs a scripted
  battery: single tool call (`compute_xtb_energy`), a 3-tool chain, a forced-structured-output call, and
  a mock todo add/list/complete cycle. Emit pass/fail + failure taxonomy (wrong-tool, malformed-args,
  no-call, bad-structured-output).
- **Test:** the spike is the test; `docs/spikes/f0-toolcalling.md` records model id, prompt shims tried,
  and the verdict. If weak → record the mitigation chosen (constrained/grammar decoding, a tool-call
  format shim, few-shot tool exemplars, or a **stronger planning model** while a cheaper model executes —
  add `llm_planning_model: str = ""` config if that path is taken).
- **Done when:** `docs/spikes/f0-toolcalling.md` says **PASS** or **PASS-with-mitigation X**; the
  mitigation (if any) is a config knob, not a code fork.
- **Deps:** F0-T2 (needs a live endpoint; may run against a stand-in OpenAI-compatible server first).

> **CHECKMATE F0:** `Agent.run` completes end-to-end against the internal endpoint with ≥1 real tool
> call; provider switch is one config change (no hardcoded Anthropic outside `llm_provider.py`); the
> spike verdict is documented with a mitigation if weak; endpoint/token/CA all config from env.
> **ADR D-A1** (internal LLM adapter, generic credential).

---

# Phase F1 — Harness backbone (autonomous plan/execute), wired on `main`

Goal of the phase: introduce the plan→approve→execute harness (foundations #1/#2) over the **full**
current tool+skill+middleware set — building it against MAF primitives, since no `create_harness_agent`
exists in-tree today.

### F1-T1 — Harness config block
- **Touch:** ~`chemclaw/config.py`.
- **Build:** `harness_enabled: bool = False`,
  `harness_autonomy: Literal["plan_only", "execute"] = "plan_only"`,
  `harness_max_loop_iterations: int = Field(default=25, ge=1)`.
- **Test:** ~`tests/test_config.py` — defaults + env override + literal validation.
- **Done when:** config present; default keeps today's behavior (harness off).
- **Deps:** —

### F1-T2 — Todo provider + plan/execute mode (MAF context providers)
- **Goal:** the self-managed todo list and the mode transition, as MAF `ContextProvider`s + function
  tools, so the model owns a visible plan.
- **Touch:** ＋`agents/harness/todo.py` (todo state + `add_todo`/`complete_todo`/`list_todos` tools +
  an `awaiting(job_id)` state), ＋`agents/harness/mode.py` (plan_only vs execute mode provider),
  ＋`agents/harness/__init__.py`.
- **Build:** a `TodoProvider(ContextProvider)` holding an ordered list of `Todo(id, text, status ∈
  {open, awaiting, completed})`; tools mutate it; a `loop_should_continue()` predicate = "any open todo
  and iterations < cap". Mode provider injects the plan/approve/execute framing per `harness_autonomy`.
  Reuse MAF's harness primitives if the installed `agent_framework` exposes them; otherwise implement
  the three providers directly (they are small).
- **Test:** ＋`tests/test_harness_todo.py` — add/list/complete transitions; `awaiting→completed`;
  `loop_should_continue` true until all todos closed or cap hit. Pure unit, no LLM.
- **Done when:** todo lifecycle + predicate proven without a model.
- **Deps:** F1-T1.

### F1-T3 — `build_agent` grows a harness path (full battery retained)
- **Goal:** when `harness_enabled`, assemble the agent with the todo/mode providers **and every current
  tool/skill/middleware**; else return today's classic agent (fallback stays load-bearing).
- **Touch:** ~`agents/chemclaw_agent.py`.
- **Build:** branch in `build_agent`: keep the existing `tools=[…]` list (xtb/solubility/pka, qm submit/
  status, find/expand/gather_evidence, `*_mcp_capability_tools()`, suggest_next_experiment,
  propose_knowledge_note, record_confirmed_answer), append the harness todo tools, and add
  `TodoProvider`/mode to `context_providers` **after** `history` and **before** `compaction` (load →
  plan → trim order). Preserve `RoleFilteredSkillsSource`, `make_audit_middleware`, and compaction on
  **both** paths. The harness must **not** drop any current tool (the reduced-toolset regression the
  branch analysis warned about).
- **Test:** ~`tests/test_agent.py` — with `harness_enabled=True` the agent exposes the todo tools **and**
  the full capability tool set (assert names present); with it False the tool set is exactly today's.
  Assert compaction + audit present on both.
- **Done when:** both paths build; harness path is a strict superset of tools.
- **Deps:** F1-T2.

### F1-T4 — Plan→approve→execute + runaway cap
- **Goal:** `plan_only` proposes a plan and stops for approval (the pre-execution GxP gate);
  `execute` runs the capped completion loop.
- **Touch:** ~`agents/harness/mode.py`, and the run driver (F2-T1 hosts the actual loop — this ticket
  defines the predicate + approval boundary the driver calls).
- **Build:** an approval boundary function `requires_plan_approval(autonomy) -> bool` and the capped
  loop contract `run_until_done(agent, *, max_iterations)` signature (implemented in F2-T1). The
  plan-approval reuses the existing **interaction-approval** seam (`interaction_approval_timeout_seconds`,
  `tests/test_interaction_approval.py`) rather than a new mechanism.
- **Test:** ＋`tests/test_harness_loop.py` — with a fake agent, `execute` stops at `max_iterations`;
  `plan_only` yields a plan and does not execute todos.
- **Done when:** plan-only stops for approval; execute honors the cap; fallback path untouched.
- **Deps:** F1-T3 (loop wired in F2-T1).

> **CHECKMATE F1:** harness runs over the **full** current tool+skill set; plan→approve→execute
> demonstrated behind `harness_enabled`; classic fallback intact and tested; `make lint type test`
> green. **ADR D-020 finalized** (harness is the backbone; fallback load-bearing against MAF
> `[Experimental]` churn).

---

# Phase F2 — Front door + run harness (make the agent actually run)

Goal of the phase: a browser chat surface + the ASGI service that builds the agent, opens the MCP
lifecycle, runs a turn, streams — the missing caller the agent docstring describes.

### F2-T1 — Run-loop service (ASGI)
- **Goal:** one FastAPI app that owns session→agent→MCP lifecycle→turn→stream, and hosts the harness
  completion loop from F1-T4.
- **Touch:** ＋`service/app.py` (FastAPI factory), ＋`service/runner.py` (`run_turn` /
  `run_until_done`), ＋`service/__init__.py`, ~`pyproject.toml` (add `fastapi`, `uvicorn`, `sse-starlette`).
- **Build:**
  - `create_app() -> FastAPI` with routes: `POST /sessions` (start), `POST /sessions/{id}/messages`
    (send a turn, SSE stream back), `GET /healthz`/`GET /readyz`.
  - `runner.run_turn(session, user_msg) -> AsyncIterator[Event]`: builds the agent via `build_agent(...)`,
    opens **`async with *agent.mcp_tools:`** once per turn (or per session — decide in ADR; per-session
    keeps subprocesses warm), calls `agent.run`/`run_stream`, yields plan/tool/answer events.
  - Config: `service_host: str = "0.0.0.0"`, `service_port: int = Field(default=8080, gt=0)`,
    `service_cors_origins: str = ""`.
- **Test:** ＋`tests/test_service.py` — `httpx.AsyncClient` against `create_app()` with a **fake chat
  client** injected (via `build_agent(chat_client=…)`): start a session, send a message, assert the SSE
  stream carries a tool-call event and a final answer; `GET /healthz` is 200. MCP lifecycle opened/closed
  exactly once (assert via a spy).
- **Done when:** a turn runs end-to-end in-process with a fake client and the MCP context is managed in
  one place.
- **Deps:** F1-T3 (agent build), F1-T4 (loop), F0-T3 (streaming).

### F2-T2 — Web chat surface (thin built-in)
- **Goal:** a minimal embedded chat UI (SSE) rendering plan/tool-trace/citations/approvals — the
  recommended thin built-in (full control over the PR-gate/approval affordances a generic UI can't render).
- **Touch:** ＋`service/static/index.html` + `service/static/app.js` (no build step; vanilla + SSE),
  ~`service/app.py` (mount static + an `EventSource` endpoint).
- **Build:** render streamed events: the **plan/todo list**, tool-call trace, cited note ids (linkable),
  "job started (id …)" for async work, and **[Approve]/[Reject]** buttons wired to the interaction- and
  plan-approval endpoints.
- **Test:** ＋`tests/test_service_ui.py` — the static page serves 200 and references the SSE endpoint;
  an event-render smoke test (parse a sample event stream → assert DOM contract via a tiny JSDOM-free
  string check, or mark as a manual/Playwright check in `docs/spikes`). Keep UI logic thin.
- **Done when:** a chemist can open the page, send a message, watch a plan + tool use, get a cited answer.
- **Deps:** F2-T1.

### F2-T3 — Turn UX contract (events)
- **Goal:** a stable event schema so surfaces (web now, Slack/mobile later) share one contract.
- **Touch:** ＋`service/events.py` (`pydantic` `Event` union: `PlanEvent`, `ToolCallEvent`,
  `TokenEvent`, `JobStartedEvent`, `ApprovalRequestEvent`, `AnswerEvent`, `ErrorEvent`), ~`service/runner.py`.
- **Test:** ＋`tests/test_service_events.py` — each event serializes/deserializes; the runner emits the
  documented sequence for a scripted turn.
- **Done when:** one typed event contract, reused by the runner and UI.
- **Deps:** F2-T1.

> **CHECKMATE F2:** a chemist opens a browser, asks a multi-step question, watches a plan + tool use,
> gets a cited answer — in a container against the internal LLM; MCP lifecycle handled once in the
> service. **ADR D-A2** (front-door service).

---

# Phase F3 — Durable session + job → session push-back

Goal of the phase: sessions survive pod restarts (foundation #6), and a finished Nextflow/BO job
**wakes the session** (the missing `notify_agent`/plan-1.7 callback) instead of polling.

### F3-T1 — Postgres session/history store
- **Goal:** replace `InMemoryHistoryProvider` (constructed at `agents/chemclaw_agent.py:114`) with a
  durable provider keyed by user+thread, resumable.
- **Touch:** ＋`agents/session_store.py` (`PostgresHistoryProvider` implementing MAF's history-provider
  interface), ＋`infra/sql/002_sessions.sql` (`sessions`, `session_messages` tables),
  ~`agents/chemclaw_agent.py` (inject the provider; keep in-memory as a test/dev default when
  `session_store="memory"`).
- **Build:** config `session_store: Literal["memory", "postgres"] = "memory"`,
  `session_store_dsn: str = ""` (falls back to `postgres_dsn`). Provider persists/loads message groups;
  **session state ≠ Temporal job state** (D-002 upheld); compaction still applies on top.
- **Test:** ＋`tests/test_session_store.py` (Postgres-backed, mirrors `tests/test_postgres_store.py`
  style) — append messages, reload by session id, assert history round-trips; a "restart" = new provider
  instance over the same dsn sees prior turns.
- **Done when:** a session survives a fresh provider instance (proxy for pod restart).
- **Deps:** F2-T1.

### F3-T2 — Session-events channel (job → session)
- **Goal:** a durable channel a completing workflow writes and the front door tails.
- **Touch:** ＋`infra/sql/003_session_events.sql` (`session_events(session_id, kind, payload,
  created_at, consumed_at)`), ＋`workflows/notify.py` (`record_session_event` activity),
  ＋`service/session_events.py` (a tailer the service runs as a background task, `LISTEN/NOTIFY` or poll).
- **Build:** config `session_event_poll_seconds: float = Field(default=2.0, gt=0)` (only if polling).
  Prefer Postgres `LISTEN/NOTIFY` to avoid busy-wait; poll is the fallback.
- **Test:** ＋`tests/test_session_events.py` — writing an event row surfaces it to a subscribed tailer;
  consumed rows are marked.
- **Done when:** an event written by an activity reaches the service without polling the job.
- **Deps:** F3-T1.

### F3-T3 — `awaiting → completed` end-to-end
- **Goal:** a todo that launched a job is `awaiting(job_id)`; the callback completes it and the harness
  loop resumes with now-unblocked follow-ups.
- **Touch:** ~`workflows/qm_job.py` (on completion, call `record_session_event` best-effort with the
  session id passed through the job input — see F4-T3 for `requested_by`/session propagation),
  ~`service/runner.py` (on a session event, append the result and flip the matching `awaiting` todo).
- **Test:** ＋`tests/test_awaiting_flow.py` — a fake job completion event flips a specific `awaiting`
  todo to `completed` and the loop predicate re-activates dependent todos. Uses the Temporal test env
  (`tests/temporal_env.py`) for the workflow half.
- **Done when:** a long job's result appears in the session on completion; `awaiting→completed` shown;
  no busy-wait.
- **Deps:** F3-T2, F1-T2.

> **CHECKMATE F3:** a session survives a front-door restart and resumes; a long job's result appears
> in-session on completion with no polling; `awaiting→completed` visible; durability stays in Temporal.
> **ADR D-A3** (session + callback).

---

# Phase F4 — Entra ID identity & RBAC, system-wide (mandatory)

Goal of the phase: users authenticate via Entra OIDC; **every backend workflow is user-specific via
Entra** (required, authorizing input, reject-if-absent); the two non-Entra bridges (Temporal, HPC)
carry identity as a claim; the generic LLM credential is the one documented exception.

### F4-T1 — User auth at the front door (Entra OIDC)
- **Touch:** ＋`service/auth.py` (JWT validation), ~`service/app.py` (auth dependency on all non-health
  routes).
- **Build:** config `entra_tenant_id`, `entra_client_id`, `entra_jwks_url`, `entra_audience`,
  `entra_required` (`bool = True`; `False` only for local dev). Validate signature against tenant JWKS,
  **check `aud`** (confused-deputy, §7), extract `oid`/`upn` + app-roles/groups into a
  `Principal(oid, upn, roles: frozenset[str])`. Reject unauthenticated when `entra_required`.
- **Test:** ＋`tests/test_auth.py` — a signed test JWT (local key) validates and yields the `Principal`;
  wrong audience/expired/absent → 401; `entra_required=False` yields a dev principal.
- **Done when:** every non-health route requires a valid Entra token in prod config.
- **Deps:** F2-T1.

### F4-T2 — Workload identity federation for backend services
- **Touch:** ＋`agents/identity/workload.py` (mint short-lived Entra tokens from the pod SA token),
  ~`agents/llm_provider.py` is **NOT** touched (LLM stays on the generic key — the documented exception).
- **Build:** config `entra_workload_federation_enabled: bool = False`, `entra_workload_client_id`,
  `entra_token_endpoint`, `entra_sa_token_path` (`= "/var/run/secrets/…/token"`). A
  `get_service_token(scope) -> str` helper caches until near expiry.
- **Test:** ＋`tests/test_workload_identity.py` — with a fake token endpoint, exchanges the SA token for
  a service token and caches it; refresh past expiry.
- **Done when:** backend components can obtain their own Entra token with no stored client secret.
- **Deps:** F4-T1.

### F4-T3 — Every backend workflow user-specific via Entra (the core rule)
- **Goal:** make the Entra `oid` a **required** field on every workflow input and **reject** a run
  without it.
- **Touch:** ~`workflows/models.py` (`QMJobInput.requested_by: str` → **required Entra oid**, drop the
  `"unknown"` default; add `session_id: str | None` for push-back), ~`agents/qm_tools.py`
  (`submit_qm_job` takes the principal from context and passes `requested_by`), and the BO/ELN/report/
  memory workflow inputs get the same required `requested_by`.
- **Build:** a small `require_actor(principal) -> str` guard raised as **non-retryable bad-data** when
  absent (mirrors `require_canonical_smiles` in `prepare_input`). `qm_job_key` **still excludes**
  `requested_by` (cache identity is molecular, not per-user — D-011 preserved), but the workflow
  **authorizes** on it (F4-T5).
- **Test:** ~`tests/test_qm_workflow.py` + ~`tests/test_qm_tools.py` — a job with no `requested_by`
  is rejected before submission; the cache key is unchanged by the actor (two users, one cached compute).
- **Done when:** no workflow starts without an Entra actor; cache identity unaffected.
- **Deps:** F4-T1.

### F4-T4 — OBO for user-scoped downstream (ELN/LIMS)
- **Touch:** ＋`agents/identity/obo.py` (`exchange_obo(user_token, scope) -> str`), consumed by the
  future ELN/LIMS adapters via the F7 seam (no concrete source now).
- **Build:** config `entra_obo_enabled: bool = False`. The helper is generic; a source opts in by
  calling it. **Wired but dormant** until a user-scoped source exists.
- **Test:** ＋`tests/test_obo.py` — with a fake token endpoint, exchanges a user token for a downstream
  token; failure surfaces a typed error.
- **Done when:** OBO is available for any user-scoped source to call; not yet used by a concrete source.
- **Deps:** F4-T2.

### F4-T5 — Authorization at one point + wire existing seams
- **Goal:** one authorization gate for expensive triggers; real actor into audit; roles into skills.
- **Touch:** ＋`agents/authz.py` (`authorize_trigger(principal, action) -> None` raising `Forbidden`),
  ~`agents/chemclaw_agent.py` (pass `actor=principal.oid`, `allowed_skills=roles→skills`),
  ~`agents/qm_tools.py` + BO/report submit tools (call `authorize_trigger` **before** starting the
  workflow), ~`service/runner.py` (build the agent with the request principal).
- **Build:** config `entra_role_skill_map` (JSON string → `dict[str, list[str]]`) and
  `entra_expensive_actions` (list). `make_audit_middleware(actor=principal.oid)` now records the real
  actor; `RoleFilteredSkillsSource` is fed the principal's role→skill set. Authorization is **one**
  function called at the trigger boundary, not scattered.
- **Test:** ＋`tests/test_authz.py` + ~`tests/test_audit.py` + ~`tests/test_skill_access.py` — an
  unauthorized role cannot trigger an expensive action even in `execute` mode; audit events carry the
  real oid; skills are filtered by role.
- **Done when:** unauthorized users can't trigger expensive paths (even autonomously); identity shows
  end-to-end incl. the Temporal payload; authorization lives at one point.
- **Deps:** F4-T3, F1-T3.

### F4-T6 — Temporal + HPC identity bridges (§7)
- **Touch:** ~`workflows/worker.py`/client construction (mTLS/API-key auth to Temporal),
  ＋`agents/identity/hpc_bridge.py` (map Entra oid → HPC/Nextflow service identity, **log every
  mapping**).
- **Build:** config `temporal_tls_cert`, `temporal_tls_key`, `temporal_tls_ca`, `temporal_api_key`;
  `hpc_bridge_identity`, `hpc_bridge_log_dsn` (or reuse audit sink). Identity rides **inside** the
  workflow payload (F4-T3), not the transport (§7.2).
- **Test:** ＋`tests/test_hpc_bridge.py` — an Entra oid maps to the HPC identity and the mapping is
  logged; Temporal client picks up mTLS config (constructed-args assertion, no live broker).
- **Done when:** both bridges carry identity as a claim; every HPC mapping is logged.
- **Deps:** F4-T3.

> **CHECKMATE F4** (+ security review): every backend workflow is user-specific via Entra (no-actor
> run rejected, authorized against that identity); an unauthorized user can't trigger an expensive path
> even in `execute` mode; identity shows end-to-end incl. the Temporal payload; authorization at **one**
> point; the generic LLM key is the one documented exception plus the two transport bridges.
> **ADR D-A4** (Entra on OpenShift; Managed Identity → Workload Identity Federation).

---

# Phase F5 — HPC/Nextflow real execution path

Goal of the phase: turn the mock spine real — **only `workflows/activities.py` changes**, as its
docstring promises. Replace `submit_to_hpc`/`poll_hpc_status` with a Nextflow launch+poll.

### F5-T1 — Launch-interface decision + adapter skeleton
- **Goal:** pick the launch interface and put it behind one seam.
- **Touch:** ＋`workflows/hpc/nextflow.py` (`launch_run`, `poll_run`, `fetch_artifacts`),
  ＋`docs/adr/` note, ~`workflows/activities.py`.
- **Build:** ADR **D-A5a** choosing **Seqera Platform (Tower) API** (recommended: REST run status) vs
  `nextflow` CLI over SSH vs an internal REST launcher. Config `hpc_launch_interface`,
  `hpc_api_base_url`, `hpc_api_token` (via the HPC bridge, F4-T6), `hpc_pipeline_name`,
  `hpc_pipeline_version`, `hpc_artifact_store_url`. Keep the `HpcJobHandle` seam so the workflow is
  untouched.
- **Test:** ＋`tests/test_nextflow_adapter.py` — against a fake HTTP endpoint: `launch_run` returns a
  handle; `poll_run` transitions submitted→running→succeeded; `fetch_artifacts` pulls the result blob.
- **Done when:** the adapter drives a fake Nextflow lifecycle; interface chosen in an ADR.
- **Deps:** F0-T1 (config pattern), F4-T6 (HPC identity).

### F5-T2 — Replace the mocked activities
- **Touch:** ~`workflows/activities.py` (`submit_to_hpc` → `nextflow.launch_run`; `poll_hpc_status` →
  heartbeat-poll `nextflow.poll_run` + `fetch_artifacts`; keep `activity.heartbeat()` against
  preemption), ~`workflows/models.py`/parse (real parser, e.g. cclib, in `parse_qm_output`).
- **Build:** delete the mock template/regex and `hpc_mock_*` config once the real path lands (or keep
  them behind `hpc_launch_interface="mock"` for local/CI — **recommended**, so tests need no cluster).
- **Test:** ~`tests/test_qm_workflow.py` — with `hpc_launch_interface="mock"` the existing durable test
  still passes; with a fake Nextflow endpoint the real adapter path runs end-to-end in the Temporal test
  env; kill-the-worker mid-poll resumes without re-running completed steps.
- **Done when:** real Nextflow path runs durably; mock retained for CI.
- **Deps:** F5-T1.

### F5-T3 — Pipeline version in the cache key + generalize naming
- **Touch:** ~`workflows/knowledge.py`/`workflows/models.py` (fold `hpc_pipeline_version` into the QM
  cache key so a pipeline update is a **miss**, not a stale hit — D-011/D-033), rename
  `QMJobWorkflow`→`CalculationWorkflow` (+ `qm_job_key`→`calculation_key`) per plan 1c.5, keeping
  back-compat aliases if any external id references exist.
- **Test:** ＋`tests/test_calc_key_versioning.py` — bumping `hpc_pipeline_version` changes the key
  (cache miss); same version hits.
- **Done when:** pipeline version is in the key; naming no longer implies a `sleep`.
- **Deps:** F5-T2.

### F5-T4 — Worker placement
- **Touch:** ~`workflows/worker.py` (the `hpc-jobs` worker runs where it can reach the launcher),
  deploy manifest note carried into F6.
- **Test:** ~`tests/test_workers.py` — the `hpc-jobs` worker registers the real activities; heartbeat
  timeout wired from config.
- **Done when:** worker topology documented + registered; heartbeats guard preemption.
- **Deps:** F5-T2.

> **CHECKMATE F5** (+ durability spike): a real Nextflow pipeline runs end-to-end durably (kill the
> worker mid-run → resumes, no re-run of completed steps); result cached; note PR-gated; pipeline
> version in the key. **ADR D-A5** (Nextflow HPC backend).

---

# Phase F6 — OpenShift deployment & delivery

Goal of the phase: the stack runs in-cluster with OIDC, secrets, workers, and probes.

### F6-T1 — Container images
- **Touch:** ＋`deploy/Containerfile.service`, ＋`deploy/Containerfile.worker`,
  ＋`deploy/Containerfile.mcp` (or a single multi-target image with different entrypoints).
- **Build:** UBI-based, **rootless / non-root UID** (OpenShift SCC-friendly), no secrets baked;
  entrypoints: `uvicorn service.app:create_app`, `python -m workflows.worker`, `python -m
  mcp_servers.molfp.server`.
- **Test:** a CI build job builds all images; a `docker run … --user 1001` smoke starts each entrypoint
  with `--help`/health.
- **Done when:** images build rootless and start.
- **Deps:** F2-T1, F5-T4.

### F6-T2 — Helm chart / manifests
- **Touch:** ＋`deploy/helm/` (Deployments for service + `hpc-worker` + `background-worker` + MCP;
  Service; **Route** for the front door; HPA for stateless; readiness/liveness probes;
  `NetworkPolicy` egress to HPC + internal LLM + Postgres only; ConfigMap + Secret / ExternalSecrets).
- **Build:** every endpoint/CA/queue is one config value from env/secret into the single pydantic
  `Settings`; no second config source.
- **Test:** `helm template` + `kubeconform`/`kubelint` in CI; a `docs/spikes/f6-deploy.md` dry-run note.
- **Done when:** manifests render and lint; probes defined.
- **Deps:** F6-T1.

### F6-T3 — Stateful dependencies
- **Touch:** ＋`deploy/helm/` values for Temporal (**self-host vs Temporal Cloud** — ADR **D-A6a**;
  self-host keeps everything in-cluster + OIDC-consistent), Postgres/pgvector (operator or managed) with
  mTLS + `statement_timeout` (already `pg_statement_timeout_seconds`).
- **Test:** the chosen Temporal target is reachable from a worker pod in a dry-run/dev namespace.
- **Done when:** Temporal + Postgres reachable in-cluster.
- **Deps:** F6-T2.

### F6-T4 — CI/CD
- **Touch:** ~existing CI (extend the `make lint type test` gate → build images → push to internal
  registry → deploy via OpenShift Pipelines/Tekton or Actions→registry). Migrations
  (`make db-migrate`, D-034) run as a **pre-deploy Job**.
- **Test:** CI is green on a PR; a dry-run deploy job succeeds against a dev namespace.
- **Done when:** merge → build → deploy path exists.
- **Deps:** F6-T2.

### F6-T5 — Observability
- **Touch:** ~config already has `otel_enabled`; add collector endpoint config `otel_endpoint`,
  ＋`deploy/helm/` OTel collector wiring; dashboards for loop iterations, tool latency, job status.
- **Test:** with `otel_enabled=True` and a fake collector, spans are emitted for a turn + a job.
- **Done when:** traces/metrics/logs ship in-cluster; dashboards exist.
- **Deps:** F6-T2.

### F6-T6 — Secrets + workload identity federation
- **Touch:** ＋`deploy/helm/` SA↔Entra federation for each Deployment (F4-T2); **three plain secrets**
  only: the generic LLM API key (F0), Temporal mTLS certs, HPC-bridge credential.
- **Test:** a dry-run confirms pods mint Entra tokens via federation (no stored client secret) and the
  three plain secrets mount.
- **Done when:** no long-lived client secrets in-cluster beyond the three documented plain secrets.
- **Deps:** F4-T2, F6-T2.

> **CHECKMATE F6:** full stack deploys to an OpenShift namespace; front door reachable via a Route
> behind OIDC; workers connect to Temporal + HPC launcher + internal LLM; probes green; secrets never
> in images. **ADR D-A6** (OpenShift topology).

---

# Phase F7 — Generic data-source attachment seam (framework only, no concrete sources)

Goal of the phase: unify the two half-contracts (`ElnAdapter` ingest + `SourceRetriever` retrieve)
into one documented `DataSource` seam + a config-driven registry, proven by **re-hosting the existing
ELN adapter unchanged**. First source = ELN; first *live* connector (later) = a **custom Snowflake
source via an internal data pipeline, no vendor**.

### F7-T1 — The unified `DataSource` contract
- **Touch:** ＋`sources/base.py` (`DataSource` with two independent optional halves), ~`eln/adapter.py`
  and ~`report/evidence.py` re-exported through it (no behavior change).
- **Build:** `class DataSource(Protocol)`: `name: str`; optional `ingest: IngestHalf | None`
  (`fetch_new_entries(since) -> list[RawEntry]`, `map(raw) -> MappedRecord`); optional `retrieve:
  RetrieveHalf | None` (`retrieve(query, filters) -> list[EvidenceChunk]`). Reuse the **existing**
  `RawEntry`/`EvidenceChunk` types verbatim — the seam is the *composition*, not new DTOs. A source
  implements either or both. **Only the contract is fixed, never the shape** (D-018/D-023).
- **Test:** ＋`tests/test_datasource_contract.py` — a source exposing only `ingest`, only `retrieve`,
  and both, all satisfy the protocol; missing-both is rejected.
- **Done when:** one seam expresses ingest+retrieve; existing DTOs unchanged.
- **Deps:** —

### F7-T2 — Config-driven source registry
- **Touch:** ＋`sources/registry.py` (generalize `eln/registry.py`'s `ELN_ADAPTERS` pattern),
  ~`chemclaw/config.py` (`data_sources: str = "eln-json"` → parsed list; `CHEMCLAW_DATA_SOURCES`).
- **Build:** `DATA_SOURCES: dict[str, Callable[[], DataSource]]`; `make_data_source(name)`;
  `active_ingest_sources()` / `active_retrieve_sources()` selected by config. Ingest sources reuse the
  durable `background-jobs` sync + cursor (`eln/cursor.py`, `sync_cursors`); retrieve sources are
  auto-discovered by `gather_evidence` (`agents/research_tools.py` `_text_retrievers()` reads the
  registry instead of a hardcoded `[GraphRetriever()]`).
- **Test:** ＋`tests/test_source_registry.py` — a new source is one registry entry + one config token;
  `gather_evidence` fans out over registry-declared retrievers (assert a second fake retriever's chunks
  appear); ~`tests/test_research_tools.py` updated to the registry path.
- **Done when:** adding a source is one adapter + one registry entry + one config token, zero core edits.
- **Deps:** F7-T1.

### F7-T3 — Re-host the existing ELN adapter behind the seam (the validation)
- **Touch:** ~`eln/json_adapter.py` + `eln/ord_adapter.py` wrapped as `DataSource`s (via a thin
  `ElnDataSource` binding ingest to the existing adapter + retrieve to `GraphRetriever`/
  `FingerprintReactionRetriever`), ~`workflows/eln_sync.py` to go through `active_ingest_sources()`.
- **Build:** **no behavior change** — the ELN JSON/ORD adapters, `sync_entries`, the cursor, the
  fingerprint index, and the PR-gated note path all keep working; only the *wiring* moves to the seam.
- **Test:** the **existing** `tests/test_eln.py`, `test_eln_recipes.py`, `test_eln_workflow.py`,
  `test_cursor.py` pass **unchanged** against the re-hosted adapter (the acceptance bar); add
  `tests/test_eln_datasource.py` asserting the ELN source is reachable via the registry.
- **Done when:** the real first source rides the seam with its behavior/tests unchanged.
- **Deps:** F7-T2.

### F7-T4 — Provenance / identity / PR-gate flow generically through the seam
- **Touch:** ~`sources/base.py` (every `MappedRecord` carries `source_id` + native ref),
  ~ ingest path (user-scoped sources call OBO F4-T4; PR-gate stays terminal for *knowledge*, serving
  copies ungated — the D-018 split, generalized).
- **Test:** ＋`tests/test_source_provenance.py` — an ingested record carries provenance; a knowledge
  proposal routes through the PR-gate; a serving-index write does not.
- **Done when:** provenance + gate split hold for any source, source-agnostically.
- **Deps:** F7-T3.

> **Deferred behind this seam (explicitly NOT this phase):** the **live custom Snowflake ELN
> connector** (durable `background-jobs` sync with a **pipeline cursor** over Snowflake's
> load-timestamp/row-version → note graph + ORD → PR-gate; Snowflake connector/warehouse/query live
> only in that adapter, nothing Snowflake-shaped above the seam) — lands later as the first adapter.
> Also deferred: LIMS/MES/analytical-instrument/literature adapters and their standards
> (AnIML/Allotrope, SiLA2/LAP), and analytical *models*.

> **CHECKMATE F7:** the existing ELN adapter re-hosts behind the `DataSource` seam with behavior/tests
> unchanged; a second different source could attach as one adapter + one config entry with zero core
> change; ingest/retrieve stay independent and source-agnostic; no source-specific type leaks above the
> adapter; PR-gate/serving split preserved. **ADR D-A7** (generic data-source seam).

---

# Phase F8 — Prediction trust + retrieval scale

Goal of the phase: two cheap-now/expensive-later contracts — calibrated uncertainty and a derived
retrieval index.

### F8-T1 — Uniform uncertainty contract
- **Touch:** ＋`calc/uncertainty.py` (`Prediction[T]` = value + `uncertainty` + `in_domain: bool` +
  `method: str`), ~ the xTB/pKa/solubility calculators to return it (generalizing today's
  `pka_uncertainty` / `solubility_rmse_log`), adopting **conformal prediction** where feasible.
- **Test:** ＋`tests/test_uncertainty.py` + ~`tests/test_pka.py`/`test_solubility.py` — every predictor
  returns calibrated uncertainty + an applicability-domain flag; out-of-domain inputs flag.
- **Done when:** predictions carry actionable uncertainty uniformly.
- **Deps:** —

### F8-T2 — Derived pgvector retrieval index (not a replacement)
- **Touch:** ＋`report/vector_index.py` (pgvector embeddings over notes as an **entry-point** into
  existing graph traversal; git-markdown stays source of truth — D-004 intact),
  ＋`infra/sql/004_note_embeddings.sql`, ~`report/retrievers.py` (a `VectorRetriever` behind the F7
  registry).
- **Build:** config `vector_index_enabled: bool = False`, `embedding_model`, `embedding_dim`.
  Optionally model time-bounded facts (Graphiti-style) — deferred sub-item.
- **Test:** ＋`tests/test_vector_index.py` — embeddings index notes; a query returns chunks that then
  seed graph traversal; disabled → today's behavior.
- **Done when:** retrieval scales to a large corpus while graph traversal stays the reasoning path.
- **Deps:** F7-T2.

> **CHECKMATE F8:** predictions carry calibrated uncertainty an analyst can act on; retrieval scales
> while graph traversal (not top-k) remains the reasoning path. **ADRs D-A8** (uncertainty),
> **D-A9** (derived retrieval index).

---

# Phase F9 — Docs, ADRs, autonomy evals (continuous)

Runs alongside every phase.

### F9-T1 — Rewrite `architektur.md` §6 for the real stack
- **Touch:** ~`docs/architektur.md` §6 (OpenShift instead of Azure AI Foundry/Container Apps;
  Nextflow-on-HPC instead of raw SLURM; internal OpenLLM-like adapter instead of Anthropic/Azure OpenAI).
  **Keep §7/§8 (Entra durchgängig)** — adjust only Managed Identity → **Entra Workload Identity
  Federation**; the Temporal-claim + HPC-bridge patterns are unchanged.
- **Done when:** the architecture doc describes the system being built, not a past one.

### F9-T2 — ADR log D-A1…D-A9 (+ finalize D-020)
- **Touch:** ~`DECISIONS.md` — append D-A1…D-A9 (one per CHECKMATE) and finalize D-020 (harness
  backbone). Terse running-log discipline.
- **Done when:** every phase decision has an ADR.

### F9-T3 — Autonomy metrics in the eval layer
- **Touch:** ~`evals/` (register **plan quality** = needed vs planned steps; **did plan/execute help**
  A/B vs single-shot per task type; **runaway/abort rate**). Reuse the existing eval harness
  (`tests/test_evals.py`, `eval_*` config).
- **Test:** ＋`tests/test_autonomy_evals.py` — the three metrics compute on a scripted transcript.
- **Done when:** autonomy must *prove* its value selectively, not by assumption (D-009).

---

## Appendix A — New config keys (all `CHEMCLAW_`-prefixed, in `chemclaw/config.py`)

| Phase | Keys |
|---|---|
| F0 | `llm_provider`, `llm_base_url`, `llm_model`, `llm_api_key`, `llm_tls_ca_bundle`, `llm_timeout_seconds`, `llm_max_retries`, `llm_temperature`, `llm_max_tokens`, (`llm_planning_model` if F0-T4 mitigation) |
| F1 | `harness_enabled`, `harness_autonomy`, `harness_max_loop_iterations` |
| F2 | `service_host`, `service_port`, `service_cors_origins` |
| F3 | `session_store`, `session_store_dsn`, `session_event_poll_seconds` |
| F4 | `entra_tenant_id`, `entra_client_id`, `entra_jwks_url`, `entra_audience`, `entra_required`, `entra_workload_federation_enabled`, `entra_workload_client_id`, `entra_token_endpoint`, `entra_sa_token_path`, `entra_obo_enabled`, `entra_role_skill_map`, `entra_expensive_actions`, `temporal_tls_cert`, `temporal_tls_key`, `temporal_tls_ca`, `temporal_api_key`, `hpc_bridge_identity`, `hpc_bridge_log_dsn` |
| F5 | `hpc_launch_interface`, `hpc_api_base_url`, `hpc_api_token`, `hpc_pipeline_name`, `hpc_pipeline_version`, `hpc_artifact_store_url` (retire `hpc_mock_*` or keep behind `hpc_launch_interface="mock"`) |
| F6 | `otel_endpoint` |
| F7 | `data_sources` |
| F8 | `vector_index_enabled`, `embedding_model`, `embedding_dim` |

## Appendix B — New SQL migrations (`infra/sql/`, after `001_calculation_results.sql`)

`002_sessions.sql` (sessions, session_messages) · `003_session_events.sql` · `004_note_embeddings.sql`
(pgvector). All wired into `make db-migrate` (D-034).

## Appendix C — New top-level modules/dirs

`agents/llm_provider.py` · `agents/harness/` · `agents/session_store.py` · `agents/authz.py` ·
`agents/identity/` (workload, obo, hpc_bridge) · `service/` (app, runner, events, auth, static) ·
`workflows/hpc/nextflow.py` · `workflows/notify.py` · `sources/` (base, registry) ·
`calc/uncertainty.py` · `report/vector_index.py` · `deploy/` (Containerfiles, helm).

## Appendix D — Critical path & parallelism

```
F0 ─► F1 ─► F2 ─► F3 ─► F4 ─┐
      │      │              ├─► F6 (deploy) ─► in-cluster test of F2–F5
F5 ───┴──────┘  (F5 needs F0 config + F4-T6 HPC identity; else independent of F1–F4)
F7, F8 depend only on the spine (schedule after F2 exists); F9 continuous
```

**Minimum usable assistant:** F0 → F1 → F2 → F3, deployed via F6. F4 gates multi-user; F5 makes heavy
compute real; F7 makes future sources (first: the custom Snowflake ELN connector) trivially attachable;
F8 makes predictions trustworthy.
