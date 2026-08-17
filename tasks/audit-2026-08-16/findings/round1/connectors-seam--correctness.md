# Connectors seam — CORRECTNESS

Slice: `src/chemclaw/connectors/{registry,manifest,server,server_entry,transport,identity,caller,jobs,queues,worker,health}.py`
Lens: correctness (wrong answers, crashes, lost work, silently dropped data).

Environment used for the measurements below: `sudo -n dockerd` + `make up` (Postgres/pgvector +
Temporal on `localhost:7233`), `uv run`. Baseline for the slice's own suite before any of this:
`tests/test_connector_{registry,manifest,jobs,transport,identity}.py` → **106 passed**.

---

## The durable-launch idempotency key omits the calculator/pipeline version, so a completed pre-upgrade run is served as the current answer

- **Severity**: high
- **Location**: `src/chemclaw/connectors/jobs.py:254` (`job_workflow_id`), consumed at
  `src/chemclaw/connectors/jobs.py:351` and the re-join branch at
  `src/chemclaw/connectors/jobs.py:386-403`
- **Trigger**: any declared connector job asked for twice with byte-identical launch arguments,
  across a change to the thing that computes the number, while Temporal still retains the first
  execution. Concretely, for `qm`/`compute_dft_energy`:
  1. `CHEMCLAW_HPC_LAUNCH_INTERFACE=mock` (or `nextflow` at `hpc_pipeline_version=v2.3.0`).
     A chemist asks `compute_dft_energy(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP")`.
     The workflow runs to completion.
  2. The operator points the deployment at the real cluster / bumps the pipeline version.
  3. The same request is made again.
- **Consequence**: `job_workflow_id` hashes only `[connector, job, payload]`, and `payload` is the
  model-authored params only. Nothing about *which calculator* produced the number is in it. So the
  workflow id is unchanged; `start_workflow` under `WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY`
  refuses the duplicate of a **completed** run; and the launcher hands back the first run's output —
  for `qm` its workflow id (`jobs.py:403`), which the model then polls and reads as the answer, and
  for the five `calc` jobs (all of which carry `inline_wait_seconds`) the finished
  `ConnectorJobResult` envelope itself (`jobs.py:391-399`), returned as the tool's value with no
  indication it is a rejoin.

  This defeats the rule the code states for itself twice. `connectors/qm/specs.py:81-82`
  (`qm_job_key`) includes the pipeline version "*because a real pipeline update changes the numbers,
  so it must be a cache miss, not a stale hit (D-011/D-033)*", and `connectors/qm/cache.py:70-85`
  (`calc_version`) adds the backend on the grounds that "*a deployment that ran the mock and then
  pointed at a real cluster served that fabricated number as a cache hit — carrying a `calc_refs`
  provenance stamp that says a DFT calculation produced it*". Both keys are correct. Neither is ever
  consulted, because the *launch-level* dedup fires first and the workflow that would consult them
  never runs. The mock backend synthesizes an energy from the hex digits of a job id
  (`connectors/qm/activities.poll_hpc_status`), so the wrong answer here is a fabricated number
  reported as a DFT result.

  The same argument applies to `calc`: `calc_version` there is stamped by the remote
  `Chemclaw3-mcp` server per call (`connectors/calc/remote.py:295-313`), so a server upgrade is a
  correct miss at the store — and an invisible no-op at the launcher.

  `connectors/qm/cache.py:5-9` already names this mechanism ("*the deduplication that did exist (a
  deterministic workflow id) holds only while Temporal retains that execution*"), so the retention
  window is the exact size of the exposure, not a reason it does not exist.
- **Evidence**:

  `job_workflow_id` is provably independent of both knobs (`/tmp/audit/probe3.py`, `/tmp/audit/probe4.py`):

  ```
  # CHEMCLAW_HPC_LAUNCH_INTERFACE=mock, CHEMCLAW_HPC_PIPELINE_VERSION=""
  mock/unversioned payload: {'molecule_smiles': 'CCO', 'method': 'B3LYP', 'basis_set': 'def2-SVP'}
  workflow id: qm-compute_dft_energy-29776d63ecaa48fb
  calc_version: mock-unversioned      qm_job_key: 941c5e787b19328d

  # CHEMCLAW_HPC_LAUNCH_INTERFACE=nextflow, CHEMCLAW_HPC_PIPELINE_VERSION=v2.4.0
  nextflow/v2.4.0 payload: {'molecule_smiles': 'CCO', 'method': 'B3LYP', 'basis_set': 'def2-SVP'}
  workflow id: qm-compute_dft_energy-29776d63ecaa48fb     <-- identical
  calc_version: nextflow-v2.4.0       qm_job_key: cfe3264294f6c05c   <-- both changed
  ```

  And the Temporal half, against the live broker (`/tmp/audit/probe5.py`, same reuse policy the
  launcher passes):

  ```
  first run result: result-from-OLD-PIPELINE
  second launch refused: WorkflowAlreadyStartedError
  what the caller is handed back: result-from-OLD-PIPELINE
  ```

  The launcher's `except WorkflowAlreadyStartedError` branch is exactly this path, and its comment
  ("*the idempotency contract succeeding … rejoining it is what makes a re-ask feel like a cache
  hit*") is written for the case where the science has not changed.
- **Fix**: put the calculator identity into the launch key. The cheapest correct version is to let a
  job declare where its version comes from and fold it in, e.g. an optional
  `JobSpec.version_ref: "module:function"` resolved like `precondition` is, hashed alongside the
  payload:

  ```python
  def job_workflow_id(connector: str, job: str, payload: dict[str, Any], version: str = "") -> str:
      return f"{connector}-{job}-{stable_hash([connector, job, payload, version])}"
  ```

  with `qm` declaring `chemclaw.connectors.qm.cache:calc_version` (which already returns
  `"{interface}-{pipeline}"`). Resolving it at *launch* keeps it out of the replay path, the same
  argument `JobSpec.precondition` makes for itself. A job that declares nothing keeps today's key,
  so no in-flight history is orphaned.

---

## An endpoint that declares no `tools` bypasses the state_changing/read_only partition entirely, and exposes everything the server serves

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/manifest.py:187-219` (`_check_classification`),
  `src/chemclaw/connectors/manifest.py:222-235` (the `Endpoint` comment),
  `src/chemclaw/connectors/registry.py:427` and `:440` (`allowed_tools=… if endpoint.tools else None`)
- **Trigger**: a bundle whose manifest declares an `endpoint:` with a `url` and no `tools:` key.
  `tools` defaults to `[]` and the manifest loads clean.
- **Consequence**: three answers go wrong at once, all of them quietly.
  - `_check_classification([], [], [])` is vacuously satisfied, so the "*partition, not two optional
    hints*" the docstring calls "*the whole point*" never runs.
  - `state_changing_tool_names()` returns nothing for the bundle, so `chemclaw.agent.plan_gate`
    treats **every** tool that server advertises as read-only and lets it through under an
    unapproved plan — the precise fail-open the docstring says is impossible ("*Refusing to load is
    the only option that cannot be wrong quietly*").
  - `_mcp_connection` sets `allowed_tools=None`, which `transport._allowed` reads as "everything
    this server offers" — so the agent is handed the server's whole surface, including anything the
    manifest never named. `endpoint_tool_names()` simultaneously reports `[]`, so no validator, no
    profile and no prose check can see what was actually advertised.

  Related, and a comment that contradicts its own module: `manifest.py:233-235` states "*An
  undeclared tool is treated as a read: core cannot infer a bundle's semantics, and guessing 'write'
  would gate every connector's whole surface the day this shipped.*" `_check_classification`, fifty
  lines above it, refuses to load an undeclared tool instead. The comment is stale — and it happens
  to describe exactly the fail-open the empty-`tools` case really produces, which is how the hole
  reads as intended behaviour to the next reader.
- **Evidence** (`/tmp/audit/probe6.py`, a bundle dir containing only the manifest quoted below):

  ```yaml
  name: loose
  description: a bundle that declares an endpoint and no tools
  endpoint:
    transport: http
    url: http://127.0.0.1:9911/mcp
    auth: {mode: none}
  ```

  ```
  discovered: ['loose']
  state_changing_tool_names(): []
  endpoint_tool_names(): []
  allowed_tools: [None]
  ```
- **Fix**: make the empty case unrepresentable rather than permissive — either
  `tools: list[str] = Field(min_length=1)` on both endpoint variants, or a `model_validator` on
  `HttpEndpoint`/`StdioEndpoint` rejecting an endpoint with no declared tools. Then delete the stale
  "*undeclared tool is treated as a read*" paragraph at `manifest.py:233-235`, which no longer
  describes any branch in the file.

---

## A connector that accepts TCP and then goes silent blocks every turn's startup for the full manifest `request_timeout`, not the advertised 5 s

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/registry.py:70` (`_CONNECT_TIMEOUT_SECONDS`),
  `:287-296` (`_session_kwargs`), `src/chemclaw/connectors/transport.py:184-192` (`_hold`, the
  unbounded `session.initialize()`), `src/chemclaw/connectors/registry.py:517-518`
  (`open_connector_specs` awaits every `__aenter__` before the model is called)
- **Trigger**: a connector whose pod accepts the connection and never answers the MCP
  `initialize` — a wedged process, a saturated worker pool, or an ingress/LB that accepts and
  blackholes. This is the ordinary Kubernetes shape of "the service is up and the app is dead"; it
  is not a refused connection.
- **Consequence**: `_CONNECT_TIMEOUT_SECONDS = 5.0` bounds only the TCP/TLS handshake. The MCP
  handshake inherits the *tool-call* budget from `_session_kwargs`
  (`read_timeout_seconds = request_timeout_seconds(endpoint)`), and the httpx read bound is
  deliberately looser still (`+ _READ_TIMEOUT_GRACE_SECONDS`). So `HeldConnectorSession.__aenter__`
  — which every turn awaits before the first model call — blocks for the manifest's declared
  `request_timeout`: **60 s for `calc`, 120 s for `bo`**, on every turn, against a 600 s turn
  deadline. The comment on `_CONNECT_TIMEOUT_SECONDS` claims the opposite ("*Short, because a dark
  connector must degrade quickly — the whole point of `HeldConnectorSession`*"), and
  `transport.py`'s module docstring credits the design with keeping "a dark fleet" from costing
  turn time. Both are true only for a host that *refuses*.

  Two different questions are being answered with one number: "is this server alive" and "how long
  may one calculation take". The registry already separates connect from read for httpx and then
  hands the handshake the read budget.
- **Evidence** (`/tmp/audit/probe7.py` — an `asyncio` server that accepts, reads the POST and never
  replies; timing is around `registry.open_connector_specs`, i.e. exactly what a turn pays):

  ```
  connector blackhole is unreachable (ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception))
  request_timeout=5s  -> turn startup blocked 5.1s,  unreachable=['blackhole']
  request_timeout=20s -> turn startup blocked 20.1s, unreachable=['blackhole']
  ```

  Linear in the manifest's `request_timeout`, not capped at `_CONNECT_TIMEOUT_SECONDS`. Extrapolated
  to the shipped manifests that is 60 s (`calc`, `connector.yaml`) and 120 s (`bo`).
- **Fix**: bound the handshake separately from the tool call. `create_session` takes
  `session_kwargs`, so the cleanest change is inside `HeldConnectorSession._hold`:

  ```python
  async with create_session(self._spec.connection) as session:
      async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):   # ~10 s, sibling of _CONNECT_TIMEOUT_SECONDS
          handshake = await session.initialize()
  ```

  keeping `read_timeout_seconds` as the per-tool-call bound it is documented to be. A connector that
  is up answers `initialize` in milliseconds; one that does not is dark by any useful definition.

---

## A manifest-declared tool the server no longer serves disappears silently

- **Severity**: low
- **Location**: `src/chemclaw/connectors/transport.py:201-211` (`_allowed`)
- **Trigger**: a connector's server drops or renames a tool the manifest still declares — routine
  now that the capability lives in a separately-released repo (`Chemclaw3-mcp`), and explicitly
  anticipated by `chem`/`safety`'s own manifests ("*the tool list below now exists in two
  repositories … nothing structurally forces them to agree*").
- **Consequence**: `_allowed` intersects the manifest's allow-list with what the session advertises
  and returns the intersection. A name in the allow-list that the server does not serve is dropped
  with no log line, no metric and no entry in `open_connector_specs`'s `unreachable` list — so the
  turn shows full capability while a tool is gone. Meanwhile `endpoint_tool_names()` and
  `state_changing_tool_names()` keep reporting the tool as present, because they read the manifest,
  not the session: profiles narrow to a name that no longer resolves, `plan_gate` gates a name
  nothing can call, and `make skill-validate` / `prose-validate` / `template-validate` all pass
  against a tool that does not exist. This is the D-117 defect shape — a capability that "simply
  stopped working" — which `enabled()` guards against for *bundle* names (`registry.py:178-182`,
  loud error) and nothing guards for *tool* names.
- **Evidence** (`/tmp/audit/probe6.py`, second half — allow-list of two, server serving one):

  ```
  declared 2, server serves 1 -> kept: ['similar_molecules'] (no warning emitted)
  ```

  `_allowed`'s body is `[tool for tool in tools if tool.name in keep]`; there is no branch for
  `keep - {t.name for t in tools}`.
- **Fix**: compute the missing set in `_allowed` (or in `_hold`, where the connector name is in
  scope) and report it the way `open_connector_specs` reports an unreachable connector — one
  `logger.warning` naming the bundle and the absent tools, plus a counter. It is the same
  degradation, one level finer.

---

## `BearerAuthMiddleware` strips the presented token but not the expected one, so one env var disagrees with itself

- **Severity**: low
- **Location**: `src/chemclaw/connectors/server.py:206-221` (`dispatch`), against
  `src/chemclaw/connectors/identity.py:148-157` (`_EnvBearerAuth.auth_flow`)
- **Trigger**: the bearer token env var named by the manifest (`CHEMCLAW_CHEM_TOKEN`,
  `CHEMCLAW_SAFETY_TOKEN`, …) carries trailing whitespace — a trailing newline is the normal result
  of `TOK=$(cat /run/secrets/tok)` without `-n`, of a here-doc, or of a YAML block scalar in a Helm
  values file.
- **Consequence**: the client sends the value verbatim (`f"Bearer {token}"`), the server compares
  `offered.strip()` against an **unstripped** `expected`, and the two halves of one credential
  never match. Every `/mcp` request 401s; `HeldConnectorSession` absorbs it as "connector
  unreachable" and the bundle's tools vanish from the turn. The failure is closed, so this is not a
  bypass — it is a correct-looking configuration that silently costs a whole capability, and the
  logs say "refused an unauthenticated MCP request", which points the operator at the wrong thing.
- **Evidence** (`/tmp/audit/probe8.py`, both halves of the real comparison, same env var):

  ```
  client sent: 'Bearer s3cret\n' | server expected: 's3cret\n' | accepted: False
  ```
- **Fix**: strip both sides, or neither. `expected = os.environ.get(token_env, "").strip()` in
  `dispatch`, and `token = os.environ.get(self._token_env, "").strip()` in `_EnvBearerAuth`, keeps
  the `not expected` fail-closed branch intact and makes the two readers of one variable agree.

---

## `endpoint_tool_names(servers=…)` membership-tests its argument inside a loop

- **Severity**: low
- **Location**: `src/chemclaw/connectors/registry.py:638` and `:655`
- **Trigger**: calling it with a one-shot iterator, which its own signature invites
  (`servers: Iterable[str] | None`). `endpoint_tool_names(name for name in profile.mcp_server_names)`,
  or any generator/`filter`/`map` expression.
- **Consequence**: `manifest.name not in servers` consumes the iterator. The first enabled bundle
  tested exhausts it (or advances it past the match), so every later bundle reports "not selected"
  and the function returns a silently truncated surface. `chemclaw.agent.chemclaw_agent.advertised_tool_names`
  then answers "what will this profile's agent be able to call" with a subset, and the four
  validators that check declared names against it accept references to tools they can no longer see.
- **Evidence**: today's only caller passes `profile.mcp_server_names`, a `frozenset`
  (`agent/profiles.py:53`), so this is latent rather than live. The defect is the annotation
  promising more than the body supports — `Iterable` is the one protocol that does not survive being
  used twice.
- **Fix**: either narrow the annotation to `Collection[str] | None`, or normalize on entry:
  `selected = None if servers is None else frozenset(servers)`. The second costs one line and makes
  the signature honest.

---

## Checked and found sound

Recorded so the absence of a finding is legible rather than an omission.

- **`transport.HeldConnectorSession` task affinity and failure absorption.** Opened a real session
  against a closed port through `open_connector_specs`; the anyio task group's failure arrives as an
  `ExceptionGroup` (an `Exception`, so `_hold`'s `except (Exception, asyncio.CancelledError)` does
  catch it), `_failure` is set, `connected` is `False`, the name lands in `unreachable`, the
  `chemclaw_connectors_unreachable_total` counter fires. The `_opened.set()` in `finally` releases
  the waiter on both paths. `connected` is read in `open_connector_specs` with no `await` between
  the `gather` and the comprehension, so the `_shut_down`-clears-`_task` transition cannot race it.
- **`registry.health_url`'s suffix re-rooting.** Walked the `os.path.commonprefix` arithmetic against
  both shipped deployment shapes (Helm's per-bundle Service `…:8814/mcp` → `…:8814/healthz`, and
  `connectors_dev`'s `…:8810/<name>/mcp` → `…:8810/<name>/healthz`) plus the degenerate cases
  (empty tail, partial-token tail such as `/mcp` vs `/metrics`, an override with no path). Every one
  re-roots correctly or takes the documented `return endpoint.health_url` fallback.
- **`request_timeout_seconds` / `_READ_TIMEOUT_GRACE_SECONDS` ordering.** The claim that the MCP
  session bound must trip before the httpx read bound holds: httpx's read timeout is per-socket-read
  and resets on each byte, and `mcp/client/streamable_http.py:427-431` does swallow an SSE-stream
  exception at `logger.debug` and only reconnects on a `last_event_id`, exactly as the comment says.
- **`prepare_job_launch`'s `exclude_none=True`.** Audited every `params_model` the shipped manifests
  reference (`calc/specs.py`'s five, `qm/specs.py:QmJobSpec`, `science/bo/problem.py:CampaignSpec`
  and its nested `OptimizationProblem`): every nullable field defaults to `None`, so dropping it is
  a round-trip identity. Verified separately that pydantic v2's `exclude_none` does **not** recurse
  into a `dict[str, Any]` field's values, so a future `type: object` inline param would not lose a
  caller-supplied `null` either.
- **`server._sanitize_tool_errors` / `_bind_caller_per_tool_call`.** The patched signature matches
  the installed `ToolManager.call_tool(self, name, arguments, context=None, convert_result=False)`
  exactly, `FastMCP` calls it with those keywords (`mcp/server/fastmcp/server.py:349`), `Tool.run`
  does set `__cause__` via `raise ToolError(...) from e` so the `ValueError` pass-through is real,
  pydantic's `ValidationError` is a `ValueError` and therefore does pass through, and
  `mcp.shared.context.RequestContext` does carry a `request` field, so
  `getattr(request_ctx.get(None), "request", None)` resolves. Binding is applied after sanitizing,
  which puts it outermost as its docstring claims.
- **Middleware ordering in `connector_app`.** `add_middleware` prepends, so the built stack is
  `BodySizeLimit → BearerAuthMiddleware → CallerLogMiddleware`; the body cap and the credential
  check both run before `CallerLogMiddleware.dispatch` reads anything, as the comments assert.
  `/healthz` and `/metrics` are FastAPI routes defined before `app.mount("/")`, so the exemption
  cannot be reached through the mounted MCP app.
- **`server_entry.main`'s single `service_port`.** Not a collision: `deploy/helm/chemclaw/templates/deployment-connectors.yaml:64`
  sets `CHEMCLAW_SERVICE_PORT` per connector Deployment from `.Values.connectorPort`, and each bundle
  is its own pod.
- **`caller.py`.** `bind_caller`/`reset_caller` are token-symmetric, defaults are `""` rather than
  `None`, and `BaseHTTPMiddleware` spawns the downstream task with a copy of the context taken
  *inside* `dispatch`, so the binding is visible downstream and the reset governs only the
  middleware's own context.
