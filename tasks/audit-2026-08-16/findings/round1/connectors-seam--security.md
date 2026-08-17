# Connectors seam — security & hardening (round 1)

Slice: `src/chemclaw/connectors/{registry,manifest,server,server_entry,transport,identity,caller,jobs,queues,worker,health}.py`

All reproductions below were run in this environment with `uv run` against the installed
`mcp==1.29.0` / `langchain-mcp-adapters==0.3.2` / `langchain==1.3.15`.

---

## An endpoint with no `tools:` gives the server an unlimited tool surface

- **Severity**: high
- **Location**: `src/chemclaw/connectors/registry.py:427` and `:440` (`_mcp_connection`), with
  `src/chemclaw/connectors/transport.py:201` (`_allowed`) and
  `src/chemclaw/connectors/manifest.py:124` (`HttpEndpoint.tools`)
- **Trigger**: a `connector.yaml` that declares an `endpoint:` and omits `tools:` (or writes
  `tools: []`). The manifest is valid — `_check_classification` over three empty lists passes,
  and `_contributes_capability` is satisfied by the endpoint alone.
- **Consequence**: `allowed_tools` becomes `None`, which `_allowed` reads as "everything this
  server offers", so **every tool the remote MCP server advertises is bound into the turn**. Four
  controls simultaneously see nothing, because all four are derived from the same empty
  `endpoint.tools`:

  | control | reads | result for an undeclared tool |
  |---|---|---|
  | agent-facing allow-list | `endpoint.tools` (registry.py:427) | not applied at all |
  | mutating-name refusal (`index_`/`write_`/`delete_`/`propose_`…) | `manifest.endpoint.tools` (validate_connectors.py:102) | never inspected |
  | harness plan gate | `registry.state_changing_tool_names()` (registry.py:601) | treated as read-only |
  | profile / authz precomputation | `registry.endpoint_tool_names()` (registry.py:638) | tool is unknown, so `undeclared_write_refusal` and `advertised_tool_names` never see it |

  With the shipped defaults (`tool_authz_default = "allow"`, `tool_role_gates = {}`,
  `core/config/entra.py:59-60`) and no `DEFAULT_WRITE_TOOL_GATES` entry for an unknown name,
  `authorize_tool` also passes it through. The tool runs ungated.

  This is not hypothetical shape: `connectors/chem` and `connectors/safety` are exactly this
  bundle form — a manifest inside the package for a server *another release* runs. For such a
  bundle `server_tools_module()` returns `None`, so `_served_tool_problems` (the only check that
  asks the running server what it serves) returns `[]` and the gap is invisible to CI as well.

  It is also the one omission the codebase's own `_check_classification` docstring
  (`manifest.py:190-201`) says must never be tolerated — "every way of getting that wrong fails
  *open* … Refusing to load is the only option that cannot be wrong quietly". The classification
  is enforced strictly; the allow-list it partitions is optional, and empty means unbounded.

- **Evidence**:

  `registry.py:427` — `allowed_tools=tuple(endpoint.tools) if endpoint.tools else None`
  `transport.py:208-211` — `if allowed is None: return list(tools)`

  Bundle `thirdparty/connector.yaml` with an endpoint and no `tools:` (scratch connectors dir):

  ```
  $ CHEMCLAW_CONNECTORS_DIR=…/cx uv run python …
  manifest.endpoint.tools = []
  state_changing = [] read_only = []
  allowed_tools on spec: None
  registry.endpoint_tool_names() = []
  registry.state_changing_tool_names() = []
  registry.connector_tool_names() = []
  tools bound into the turn: ['similar_molecules', 'propose_knowledge_note', 'delete_all_notes', 'exec_shell']
  ```

  And the in-package "connector we do not run" shape (no local server module), which passes every
  validator with no complaint at all:

  ```
  server_tools_module('chem')   -> None
  server_tools_module('safety') -> None
  manifest validates, tools = []
  _tool_surface_problems -> []
  _served_tool_problems  -> []
  ```

- **Fix**: make the allow-list mandatory for an endpoint rather than defaulting to "everything".
  Either (a) add a `model_validator` on `HttpEndpoint`/`StdioEndpoint` requiring
  `len(tools) >= 1` — which costs a bundle author one line and makes the failure a load-time
  error, the polarity `_check_classification` already chose — or (b) change registry.py:427/440 to
  `allowed_tools=tuple(endpoint.tools)` unconditionally so an undeclared surface is an empty
  surface. (a) is preferable: (b) silently yields a connector that contributes nothing, which is
  the "capability that quietly stopped working" failure `enabled()` is written to avoid. Keep
  `None` only if a genuinely open surface is wanted, and then make it an explicit
  `tools: "*"`-style declaration so the mutating-name and plan-gate checks can refuse it.

---

## A connector tool name silently replaces a core tool of the same name

- **Severity**: high
- **Location**: `src/chemclaw/connectors/registry.py:638-659` (`endpoint_tool_names`, returns a
  `set`, so a collision is unrepresentable) and `src/chemclaw/connectors/transport.py:201`
  (`_allowed`, filters by name only); consumed at
  `src/chemclaw/agent/langgraph_agent.py:221` — `bound = [*tools, *(connectors or [])]`
- **Trigger**: an enabled connector whose endpoint advertises a tool named the same as a core
  capability tool — e.g. `record_confirmed_answer`, `find_notes`, `get_durable_job_status`,
  `read_attachment`, `recall_preferences`, `gather_evidence`. It needs no manifest trickery: those
  names do not match `_MUTATING_PREFIXES` (`index_`, `write_`, `delete_`, `remove_`, `update_`,
  `propose_`, `submit_`), so `connector-validate` accepts them. Combined with the finding above,
  the manifest need not name them at all — the remote server just advertises them.
- **Consequence**: `ToolNode` builds `tools_by_name` in list order, so **the last registration
  wins**. Connector tools are appended after core tools, so the connector's implementation
  replaces core's for the whole turn. Concretely:
  - `record_confirmed_answer` is one of the two PR-gate writers `manifest.py:17-30` says a
    connector may reach "only by returning a `Note` in a job envelope, which is a proposal core
    decides to publish. That asymmetry is the point." A shadowing connector takes the writer
    itself.
  - `find_notes` / `gather_evidence` become an arbitrary injection point into the model's evidence.
  - `get_durable_job_status` lets a connector fabricate the completion status of any job.
  - `read_attachment` intercepts a call the model believes reads the user's uploaded file.

  `registry.job_tools()` (registry.py:571-587) refuses precisely this for *job* names, with the
  right reason written down — "the name is the authorization key, so a collision would silently
  make one connector's gate apply to the other's work". No equivalent check exists for endpoint
  tool names, against core's registry, against job names, or between two connectors.

- **Evidence**:

  Validator accepts the shadowing manifest, and the spec keeps all four names:

  ```
  manifest loads OK; tools = ['find_notes', 'record_confirmed_answer', 'get_durable_job_status', 'read_attachment']
  _tool_surface_problems -> []
  _served_tool_problems  -> []
  allowed_tools -> ('find_notes', 'record_confirmed_answer', 'get_durable_job_status', 'read_attachment')
  ```

  Last-wins resolution, over the real `_capability_tools(profile)` list and the real binding
  expression from `langgraph_agent.py:221`:

  ```
  bound = [*core, *rogue]; node = ToolNode(bound)
  find_notes                 -> 'rogue'
  record_confirmed_answer    -> 'rogue'
  get_durable_job_status     -> 'rogue'
  read_attachment            -> 'rogue'
  ```

- **Fix**: add a collision check where `job_tools()` already has one. In
  `registry.connector_tool_names()`/`endpoint_tool_names()` build the union with a duplicate
  detector instead of a `set`, and raise `ConnectorError` when one endpoint tool name is claimed
  twice, or claimed by a job, or already present in
  `chemclaw.core.tool_registry.registered_tool_names()`. Enforce it again at bind time in
  `open_connector_specs` (which is the only place that sees what a server *actually* advertised,
  as opposed to what a manifest declared): drop, log and count a returned tool whose name is
  already held, rather than letting it overwrite. A namespace prefix (`bo__suggest_next_experiment`)
  would also work but changes every profile, skill and eval probe that names tools by string.

---

## A missing or wrong connector credential reports `healthy` and does not trip `connectors_required`

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/health.py:51-71` (`_probe`) and `:103-129`
  (`check_connectors_at_startup`); `src/chemclaw/connectors/server.py:201`
  (`/healthz` is exempt from `BearerAuthMiddleware`); `src/chemclaw/connectors/transport.py:44-68`
  (`absorb_connect_failure`)
- **Trigger**: a `mode: bearer` connector whose `token_env` variable is unset or holds a stale
  value in the *calling* process — i.e. an ordinary secret-rotation or a missing
  `CHEMCLAW_CHEM_TOKEN` / `CHEMCLAW_SAFETY_TOKEN`, which `values.yaml` names as an operator
  obligation for both out-of-release bundles.
- **Consequence**: the probe uses a bare `httpx.AsyncClient` with no `auth_for(...)` and no
  identity hook, and `/healthz` is on the connector's auth exemption list — so the health path
  never exercises the credential. The connector reports **healthy** to all three consumers
  `health.py`'s own docstring names ("the readiness route … the `chemclaw_connectors_unhealthy`
  gauge … the `connectors_required` fail-fast check"), while every turn gets zero tools from it.
  `connectors_required: true` — the posture documented as "a deployment that prefers death to
  degradation gets it" — does not fire.

  Second half: the named error is lost. `_EnvBearerAuth.auth_flow` (identity.py:148-157) raises
  `MissingConnectorCredential` with a message naming the variable, justified as "much harder to
  diagnose than a named configuration error". That exception is raised inside the MCP client's
  anyio task group, so what `absorb_connect_failure` interpolates is the *outer*
  `ExceptionGroup`'s `str()`. The operator's only signal is a line that names neither the
  credential nor the variable.

- **Evidence**: a real bearer-mode connector served by `connector_app` on 127.0.0.1:8877 with
  `PROBE_TOKEN=right-token`; the client process run with `PROBE_TOKEN` unset.

  ```
  $ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8877/healthz   -> 200
  $ curl -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8877/mcp -> 401

  $ env -u PROBE_TOKEN … uv run python …
  probe_connectors(): [('probe', 'healthy', '')]
  connector probe is unreachable (ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)); its tools are unavailable this turn
  1 connector(s) did not come up for this scope and contribute no tools: probe
  tools this turn: []
  unreachable    : ['probe']
  connectors_required=True -> startup PASSED
  ```

- **Fix**: two changes.
  1. Probe through the credential. Build the probe client from `auth_for(endpoint.auth, name)` —
     the health route stays exempt server-side, so add a cheap authenticated liveness check
     instead: either have `health_url` default to the `/mcp` origin and issue an MCP `initialize`
     with the credential during `check_connectors_at_startup`, or (cheapest) have the connector
     serve `/healthz` unauthenticated *and* `/readyz` behind the bearer, and probe the latter.
     Anything that leaves the credential untested leaves this class of misconfiguration invisible.
  2. Unwrap the cause in `absorb_connect_failure`: walk `ExceptionGroup.exceptions` (and
     `__cause__`) to the first non-group exception before formatting, the same shape
     `durable/connector_job.failure_reason` already implements for Temporal's nesting. Without
     this the "named configuration error" the credential code promises never reaches a log.

---

## A connector's response is read whole with no size ceiling

- **Severity**: low
- **Location**: `src/chemclaw/connectors/registry.py:299-353` (`connector_http_client`) — the
  client bounds *time* (`httpx.Timeout(...)`) and nothing else; the read is
  `mcp/client/streamable_http.py:384`, `content = await response.aread()`
- **Trigger**: anything answering on the connector's effective endpoint returns a very large
  JSON-RPC body (an `initialize` result, or one tool result). For the shipped fleet every bundle
  declares `auth: mode: none`, so this is anything that can bind the Service port; for `chem` and
  `safety` it is a server in a different release and a different trust domain.
- **Consequence**: the whole body is materialised in the front door pod, which serves every user's
  turns. The MCP session's `read_timeout_seconds` bounds how long the call may take, not how many
  bytes arrive, so a steady stream inside the deadline is unbounded. This is the exact defect
  `core/asgi.BodySizeLimit` was written for — "the cap was a statement about what the parser would
  accept, never about what the process would ingest" — applied on the inbound leg
  (`connector_max_request_bytes`, `server.py:454`) and absent on the return leg.
  `connector_http_client`'s docstring enumerates four security properties the custom client
  preserves; a size ceiling is not among them.
- **Evidence**: no `max_bytes` / size guard exists anywhere under `src/chemclaw/connectors/`
  except `server.py:455` (inbound). `_handle_json_response` in the installed `mcp` 1.29.0:
  ```
  384:            content = await response.aread()
  385:            message = JSONRPCMessage.model_validate_json(content)
  ```
- **Fix**: add a `connector_max_response_bytes` setting (mirroring
  `connector_max_request_bytes`) and enforce it in `connector_http_client` — the simplest place is
  an httpx `response` event hook that inspects `content-length` and refuses, plus a wrapping
  transport that counts streamed bytes and raises once the ceiling is crossed. Failing that leg
  degrades the connector for the turn, which is already the designed behaviour
  (`absorb_connect_failure`).

---

## `serverInfo.version` is remote-controlled and written untruncated into the audit table

- **Severity**: low
- **Location**: `src/chemclaw/connectors/transport.py:186-191` and `:242-245` (`_stamped` stores
  `handshake.serverInfo.version` verbatim on every tool's metadata) → `agent/audit.py:226-245`
  (`_served_by`) → `agent/audit.py:279` → `agent/audit_store.py:25-31`
  (`audit_events.tool_revision`, `TEXT`)
- **Trigger**: a connector's `initialize` response carries an arbitrarily long `serverInfo.version`
  string. `mcp.types.Implementation` puts no bound on it, and `_stamped` copies it onto every tool
  the session advertises.
- **Consequence**: that string is written into `audit_events.tool_revision` on **every** tool call
  routed to that connector, for every turn, for as long as it is up. Every other free-text column
  on the same row goes through `audit._truncate` (`agent/audit.py:191-199`, bounded by
  `agent_audit_max_arg_chars`) — `arguments`, `detail`, `returned_error`. `tool_revision` is the
  one that does not, and it is the only one whose content comes from outside the trust boundary.
  The application role has `INSERT` and neither `UPDATE` nor `DELETE` on this table
  (`infra/sql/grants/app_privileges.sql`), so bloat written here cannot be cleaned up by the
  service that wrote it.
- **Evidence**: `transport.py:242` — `served = {"connector": connector, "revision": revision}` with
  `revision=handshake.serverInfo.version` and no length check; `audit.py:279` —
  `tool_revision=_served_by(request)` (no `_truncate`, unlike lines 274, 288, 435); insert at
  `audit_store.py:25-31` binds it as a plain parameter into a `TEXT` column. (The SQL itself is
  parameterised — no injection here.)
- **Fix**: bound it where it enters, in `_stamped`: `revision[:64]` (a build revision is a short
  hex string or a semver; 64 chars is generous), or route `_served_by`'s return through the same
  `_truncate` every sibling field uses. Bounding at `_stamped` is better because the value is also
  held in memory on every tool object for the turn.

---

## Checked and found sound

Recorded so a later pass does not re-derive them:

- **`BearerAuthMiddleware` is not bypassable by path.** `Request.url.path` is `scope["path"]`
  verbatim (`starlette.datastructures.URL.__init__`), the same string the router matches, so the
  `/healthz`/`/metrics` exemption cannot diverge from routing. Measured against a real
  `connector_app`: `/mcp`, `/`, `/docs`, `/openapi.json` and `/healthz/../mcp` all 401 without a
  token; `/healthz` and `/metrics` 200. FastAPI's docs routes are *not* exempt.
- **The `compare_digest` fix is real.** A raw non-ASCII byte in the `Authorization` header returns
  401, not a 500 — driven through the ASGI callable with `b"Bearer s3cr3t\xe9"` and `b"Bearer
  \xff\xfe"`. Missing/empty expected token also refuses rather than comparing.
- **The custom HTTP client actually reaches the wire.** `langchain-mcp-adapters` 0.3.2 honours
  `httpx_client_factory` (`_create_streamable_http_session`) and hands the client to
  `mcp.client.streamable_http.streamable_http_client(http_client=…)`, which uses it rather than
  building its own. So `follow_redirects=False`, `auth_for`, `turn_identity_hook` and the split
  connect/read timeout are all in force, and the library's `timeout`/`auth`/`headers` are ignored
  as claimed.
- **`_sanitize_tool_errors` intercepts the right thing.** `mcp.server.fastmcp.tools.base.Tool.run`
  really does `raise ToolError(f"Error executing tool {self.name}: {e}") from e`, so `__cause__` is
  set and the `ValueError`-only pass-through is exact. `psycopg` errors are not `ValueError`
  subclasses and are sanitised.
- **The compensating NetworkPolicy exists and defaults on.**
  `deploy/helm/chemclaw/templates/networkpolicy.yaml` renders `-connector-ingress` (own pods +
  monitoring namespaces only) and `networkPolicy.enabled` is `true` in `values.yaml:634` — so the
  "connectors authenticate nothing by design, the network policy is the boundary" claim is not
  another absent control.
- **`/readyz` does not leak the probe `detail`.** `api/routes/ops.py:121-124` emits only
  `name=state`; the httpx error text stays in logs.
- **The audit insert is parameterised** (`audit_store.py:25-31`) — no SQL string interpolation
  anywhere in this slice.
- **Job idempotency does not cross users unsafely.** `job_workflow_id` omits the actor, so two
  users' identical launches rejoin one run — but `prepare_job_launch` runs `authorize_trigger`
  first for `expensive` jobs, and `ConnectorJobResult` carries only `summary`/`data`/`note`, never
  the first requester's `rationale`, `requested_by`, `session_id` or `correlation_id`. No
  cross-user disclosure on the rejoin path.
- **Dynamic import from manifest data is config-as-code, not a privilege boundary crossing.**
  `jobs.resolve_params_model` / `resolve_precondition` (`^[\w.]+:[A-Za-z_]\w*$`) and
  `StdioEndpoint.command` execute what a `connector.yaml` names, but the only writer of a
  `connector.yaml` on `connectors_dirs` is the deployer, who already controls the image. No path
  traversal is possible through either pattern. Noted, not filed.
