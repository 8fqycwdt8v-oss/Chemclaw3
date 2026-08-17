# Connectors seam — design and simplification

Slice: `src/chemclaw/connectors/{registry,manifest,server,server_entry,transport,identity,caller,jobs,queues,worker,health}.py`.
All eleven files read in full. Reproductions run under `uv run` against the live venv.

---

## A connector *server* process loads the agent's authorization module and the whole LangGraph/Temporal client stack, because one bundle imports a private float out of `registry.py`

- **Severity**: high
- **Location**: `src/chemclaw/connectors/registry.py:89` (`_READ_TIMEOUT_GRACE_SECONDS`),
  consumed at `src/chemclaw/connectors/calc/remote.py:42`; the underlying cycle is
  `src/chemclaw/connectors/jobs.py:37` (`from chemclaw.agent.authz import ...`) and
  `src/chemclaw/connectors/identity.py:45` (`from chemclaw.agent.turn_flags import is_dry_run`).
- **Trigger**: start any `calc` connector server — `uvicorn chemclaw.connectors.calc.server.app:app`,
  which is what `deploy/entrypoint.sh` and `make connectors` run. No request needed; it happens at import.
- **Consequence**: the seam's stated purpose is that a bundle's dependency closure is loaded in its
  own process "and nowhere else" (`connectors/worker.py:36-38`, `connectors/bo/worker.py`). The
  reverse leak is unguarded and live: the *server* process pulls in `chemclaw.agent.authz`,
  `chemclaw.connectors.jobs` (the Temporal launcher), `chemclaw.connectors.transport` and with it
  `langgraph`, `langchain_core`, `langchain_mcp_adapters` and `temporalio` — the MCP **client** stack,
  inside the MCP **server**. Measured: 845 extra modules and ~0.7 s of import time per connector pod,
  bought by one line that wants a `5.0`.

  It also means `chemclaw.agent.authz` is imported by, and therefore live in, a process that has no
  turn, no principal and no authorization decision to make — and `authz.py:267` already documents
  that it must import the registry *lazily* "because the connector registry reaches the agent
  builder, which reaches this module". The cycle is known and worked around from one side only.

- **Evidence**:

  ```
  $ grep -n _READ_TIMEOUT_GRACE_SECONDS src/chemclaw/connectors/calc/remote.py
  42:from chemclaw.connectors.registry import _READ_TIMEOUT_GRACE_SECONDS
  127:            sse_read_timeout=timedelta(seconds=bound + _READ_TIMEOUT_GRACE_SECONDS),
  ```

  Import chain, traced by instrumenting `builtins.__import__` while importing the shipped app object:

  ```
  chemclaw.connectors.calc.server.app -> ...calc.server.tools -> ...calc -> ...calc.remote
      -> chemclaw.connectors.registry -> chemclaw.connectors.jobs -> chemclaw.agent.authz
  chemclaw.connectors.calc.server.app -> ...calc.remote
      -> chemclaw.connectors.registry -> chemclaw.connectors.transport -> langgraph
  ```

  Cost, measured by stubbing the registry with a module that carries only that float:

  ```
  WITH registry stubbed:  1473 modules, 1.27s
      langgraph NOT loaded / langchain_core NOT loaded / langchain_mcp_adapters NOT loaded
      temporalio NOT loaded / chemclaw.agent.authz NOT loaded
      chemclaw.connectors.jobs NOT loaded / chemclaw.connectors.transport NOT loaded
  WITHOUT stub (as shipped): 2318 modules, 1.98s
      langgraph loaded / langchain_core loaded / langchain_mcp_adapters loaded
      temporalio loaded / chemclaw.agent.authz loaded
      chemclaw.connectors.jobs loaded / chemclaw.connectors.transport loaded
  ```

  Per-module import weight, for scale:

  ```
  chemclaw.connectors.registry:  2094 modules, 1.60s
  chemclaw.connectors.jobs:      1891 modules, 1.26s
  chemclaw.connectors.transport: 1137 modules, 0.84s
  chemclaw.agent.authz:           816 modules, 0.50s
  chemclaw.core.http:             299 modules  (for comparison — a genuinely low-layer module)
  ```

- **Fix**: two changes, both behaviour-preserving.
  1. Move the three timeout constants (`_CONNECT_TIMEOUT_SECONDS`, `_DEFAULT_REQUEST_TIMEOUT_SECONDS`,
     `_READ_TIMEOUT_GRACE_SECONDS`) and `request_timeout_seconds` out of `registry.py` into a leaf
     module — `chemclaw/core/http.py` already holds `is_loopback_url`, is imported by `manifest.py`,
     and costs 299 modules. Make `_READ_TIMEOUT_GRACE_SECONDS` public there, since it now has two
     legitimate readers in different packages; a private name imported across a package boundary is
     the tell that it is in the wrong module.
  2. Break the `connectors → agent` direction, which is the reason `registry` is heavy at all.
     `jobs.py` needs `authorize_trigger`/`require_actor` and `identity.py` needs `is_dry_run`; both
     are identity/entitlement primitives, not agent-builder concerns. Either move them beside
     `chemclaw.core.identity_context` (which `identity.py` already imports), or import them lazily
     inside the two call sites, mirroring what `authz.py` was already forced to do in the other
     direction. Add a test asserting `chemclaw.agent` is absent from `sys.modules` after importing
     a bundle's `server.app`, so the leak cannot come back silently.

---

## `resolve_params_model` and `resolve_precondition` are the same fourteen lines, twice

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/jobs.py:89-118` and `src/chemclaw/connectors/jobs.py:121-142`
- **Trigger**: static. Both resolve a `module:Attribute` reference off a `JobSpec` field with the same
  regex (`^[\w.]+:[A-Za-z_]\w*$`, `manifest.py:298` and `:311`), the same error class, and the same
  four failure branches.
- **Consequence**: a fix to one is a fix to one. Concretely, both catch only `ImportError` — a
  `params_model` or `precondition` whose module raises `AttributeError`/`SyntaxError` on import escapes
  as a raw traceback rather than a `ConnectorJobError`, and that has to be corrected in two places.
  The same is true for anything else the resolution should learn (a check that the reference is not a
  builtin, a better message naming the job).
- **Evidence**: unified diff of the two function bodies with docstrings and comments stripped —
  five of fourteen lines differ, and four of those five differ only in the literal `"params_model"` vs
  `"precondition"`:

  ```
  -def resolve_params_model(reference: str) -> type[BaseModel]:
  +def resolve_precondition(reference: str) -> Callable[[Any], None]:
   module_name, _, attribute = reference.partition(":")
   try:
   module = import_module(module_name)
   except ImportError as exc:
   raise ConnectorJobError(
  -f"params_model {reference!r}: cannot import {module_name!r}"
  +f"precondition {reference!r}: cannot import {module_name!r}"
   ) from exc
  -model = getattr(module, attribute, None)
  -if model is None:
  -raise ConnectorJobError(f"params_model {reference!r}: {module_name!r} has no {attribute!r}")
  -if not (isinstance(model, type) and issubclass(model, BaseModel)):
  -raise ConnectorJobError(f"params_model {reference!r} is not a pydantic model")
  -return model
  +check = getattr(module, attribute, None)
  +if check is None:
  +raise ConnectorJobError(f"precondition {reference!r}: {module_name!r} has no {attribute!r}")
  +if not callable(check):
  +raise ConnectorJobError(f"precondition {reference!r} is not callable")
  +return check
  ```

- **Fix**: one private `_resolve_reference(reference: str, *, field: str, requires: Callable[[object], bool],
  requirement: str) -> object`, with the two public functions becoming a typed one-liner each
  (they must stay public — `validate_connectors.py:40` imports `resolve_precondition`, and
  `_build_params_model` needs the `type[BaseModel]` return). Behaviour-preserving, including the exact
  message strings.

---

## The connector's fail-closed auth state is an environment-variable *name*, so setting a variable with that name authenticates every request

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/server.py:60` (`_UNRESOLVED_AUTH`), returned at `:116` and
  `:129`, consumed at `:206` (`os.environ.get(token_env, "")`)
- **Trigger**: a connector process in the unresolved state (its manifest ships beside the module but
  `connectors_dir` points elsewhere, or `discovered()` raised) **and** an environment variable literally
  named `CHEMCLAW_CONNECTOR_AUTH_UNRESOLVED` set to any non-empty value. A request presenting
  `Authorization: Bearer <that value>` is served.
- **Consequence**: `_declared_bearer_env`'s docstring promises "the connector answers 401 until an
  operator fixes the manifest". That promise rests entirely on the comment at `:56-59` — "No
  environment variable has this name" — which is an assumption about the deployment's environment,
  not a property of the code. The sentinel travels in the same `str | None` channel as a real env-var
  name and reaches `os.environ.get`, which cannot tell them apart.
- **Evidence** (`/tmp/sentinel.py`, run under `uv run`; forces the unresolved branch by making
  `discovered()` return `{}` while `_ships_a_manifest` reports True):

  ```
  connector_auth_unresolved: connector ghost ships a manifest that this process did not discover ...
  no credential           -> 401
  wrong bearer            -> 401
  bearer == $SENTINEL_VAR -> 200 {"served":true}
  ```

- **Fix**: the sentinel should never be a `str`. Change `_declared_bearer_env` to return
  `str | None | Literal[Unresolved.UNRESOLVED]` (an `enum.Enum` member, or a three-way
  `AuthRequirement` dataclass), and branch on it in `dispatch` before touching `os.environ` —
  `if requirement is UNRESOLVED: return Response(401)`. Behaviour-preserving in every state except
  the one reproduced above, which it fixes.

  **The stronger simplification is to delete the state entirely.** `_declared_bearer_env` asks the
  *core-side* registry (`discovered()`, which walks every bundle in `settings.connectors_dirs`) a
  question about *this* process's own bundle — and then needs `_ships_a_manifest` (`server.py:133-143`)
  to detect the case where that answer is untrustworthy, plus the sentinel to encode the resulting
  third state, plus a lazy-resolution latch on the middleware (`server.py:176-197`) to keep the
  registry cache from being warmed at app-build time. That is ~80 lines of code and reasoning
  (`server.py:56-197`) whose whole subject is "the registry might be pointed at the wrong tree".

  `_ships_a_manifest` already proves the answer is available directly:
  `Path(__file__).parent / name / MANIFEST_FILENAME`. Reading and validating that one file gives the
  bundle's declared auth mode with no dependence on `connectors_dirs`, no cache to warm, no lazy
  latch, and no third state — the app object being served is `chemclaw.connectors.<name>.server.app`,
  so the packaged manifest is the authoritative statement of what *this code* requires. Not
  behaviour-preserving in two cases, and both change for the better: a misconfigured `connectors_dir`
  goes from "401 everything" to "correct token enforcement", and an operator directory shadowing a
  shipped bundle's name stops being able to relax the shipped bundle's credential requirement.
  Bundles outside the package keep today's behaviour (no manifest beside the module → open), which
  the current docstring already concedes it cannot improve on.

---

## A comment two screens below `_check_classification` states the opposite of what it enforces

- **Severity**: medium
- **Location**: `src/chemclaw/connectors/manifest.py:233-235` (the `Endpoint` union comment) vs
  `src/chemclaw/connectors/manifest.py:187-219` (`_check_classification`)
- **Trigger**: a bundle author writing a `connector.yaml` and reading the union comment, which is the
  block that documents what `state_changing` means.
- **Consequence**: the comment says

  > An undeclared tool is treated as a read: core cannot infer a bundle's semantics, and guessing
  > "write" would gate every connector's whole surface the day this shipped.

  The validator refuses to load. The `_check_classification` docstring 15 lines above even argues
  against the comment by name: *"Defaulting an undeclared tool to 'read' would put the whole burden on
  a bundle author remembering."* The two statements are in the same file about the same field, and the
  one a reader hits while writing YAML is the false one — on the field that decides whether the plan
  gate refuses a tool under an unapproved plan.
- **Evidence**:

  ```
  >>> HttpEndpoint(url="http://127.0.0.1:8815/mcp", tools=["a","b"], state_changing=["a"], read_only=[])
  REFUSED: Value error, endpoint does not say whether tool(s) ['b'] change state; list each under
           `state_changing` ... or `read_only` ...
  ```

  Same file, three further references to symbols that no longer exist — a reader following any of
  them to see the dispatch site finds nothing:

  ```
  src/chemclaw/connectors/manifest.py:223: "one branch in `connectors.registry._mcp_tool`"
  src/chemclaw/connectors/registry.py:400: "The twin of `_mcp_tool`, dispatching on the same union"
  src/chemclaw/connectors/registry.py:430: "for the same reason as `_mcp_tool`"
  src/chemclaw/connectors/transport.py:87: "The LangChain twin of an unconnected `ConnectorMcpTool`"
  ```

  `grep -rn '_mcp_tool\|ConnectorMcpTool' src/ --include=*.py` returns only these four comment
  occurrences; the actual dispatch site is `registry._mcp_connection`.
- **Fix**: delete the last two sentences of the `Endpoint` comment (`manifest.py:233-235`) — the
  correct statement is already in `_check_classification`'s docstring — and repoint the four dangling
  references at `registry._mcp_connection`. Behaviour-preserving (comments only).

---

## `HttpEndpoint` and `StdioEndpoint` declare the same three fields and the same validator twice

- **Severity**: low
- **Location**: `src/chemclaw/connectors/manifest.py:124-132` and `:176-184`
- **Trigger**: static; any change to the tool-classification contract.
- **Consequence**: the classification triple (`tools`, `state_changing`, `read_only`) and its
  `_every_tool_is_classified` validator are written once per transport, with an identical one-line
  docstring on each. `_check_classification` was already extracted, so what is left duplicated is
  exactly the wiring that decides whether it runs — which is the part that fails silently if a third
  transport variant is added and forgets it. The `Endpoint` union comment already says a new transport
  should be "one variant plus one branch"; today it is one variant plus one branch plus four fields
  plus a validator.
- **Evidence**: the two blocks are byte-identical apart from indentation:

  ```
  tools: list[str] = Field(default_factory=list)
  state_changing: list[str] = Field(default_factory=list)
  read_only: list[str] = Field(default_factory=list)

  @model_validator(mode="after")
  def _every_tool_is_classified(self) -> Self:
      """Reject an endpoint that does not classify each of its tools exactly once."""
      _check_classification(self.tools, self.state_changing, self.read_only)
      return self
  ```

- **Fix**: a `_ClassifiedEndpoint(BaseModel)` base carrying the three fields, the model config and the
  validator; `HttpEndpoint`/`StdioEndpoint` inherit it and keep only their own `transport` literal and
  transport-specific fields. Behaviour-preserving — the discriminated union still discriminates on the
  subclass-declared `transport` literal, and nothing in the tree serializes an `Endpoint` (`grep -rn
  'endpoint.model_dump\|manifest.model_dump' src/` is empty), so the field-order change in
  `model_dump`/JSON schema is unobservable.

---

## `find_job` re-implements `job_names()` inline, in the same file, seven lines apart

- **Severity**: low
- **Location**: `src/chemclaw/connectors/registry.py:634` vs `:598`
- **Trigger**: a template step or CLI naming a job that no enabled bundle declares
  (`durable/template_activities.py:179`, `cli/live_jobs.py:177`).
- **Consequence**: the "here are the valid ones" half of the error message is a second copy of
  `job_names()`. If the notion of "declared job name" ever changes (a namespaced name, a filter on
  disabled jobs), the error message keeps reporting the old one — the classic
  message-drifts-from-reality failure, in the one message a confused operator reads.
- **Evidence**: the two lines are character-identical after the assignment target:

  ```
  registry.py:598:    return sorted(job.name for manifest in enabled() for job in manifest.jobs)
  registry.py:634:    valid = sorted(job.name for manifest in enabled() for job in manifest.jobs)
  ```

- **Fix**: `valid = job_names()`. Behaviour-preserving and identical output.

---

## `open_connector_specs` is pure transport but lives in `registry.py`, which is why the front door imports the registry at all

- **Severity**: low
- **Location**: `src/chemclaw/connectors/registry.py:475-532`
- **Trigger**: static.
- **Consequence**: the module split the package README describes is
  *`registry.py` = discovery/enablement*, *`transport.py` = the per-turn MCP session*
  (`connectors/__init__.py`). `open_connector_specs` reads no registry state — its body uses only
  `HeldConnectorSession`, the caller's `AsyncExitStack`, `logger` and `record_metric` — yet it is the
  registry's largest function and the only thing `api/runner.py` imports from the registry
  (`runner.py:59` is that file's sole `from chemclaw.connectors` line). So the front door's turn
  runner takes a dependency on manifest discovery to get a function that only opens sessions, and
  `registry.py` reaches 669 lines by holding roughly half a transport module (the three timeout
  constants, `_endpoint_url`, `request_timeout_seconds`, `_session_kwargs`, `connector_http_client`,
  `_connector_client_factory`, `_mcp_connection`, `open_connector_specs`, `health_url`).
- **Evidence**: `open_connector_specs`'s body references no name defined in `registry.py`;
  `grep -n "from chemclaw.connectors" src/chemclaw/api/runner.py` returns exactly line 59.
- **Fix**: move `open_connector_specs` to `transport.py` (which already owns `HeldConnectorSession`
  and `absorb_connect_failure`, and creates no import cycle — `registry` already imports `transport`,
  not the reverse), and move the client/timeout group with it or into `core/http.py` per the first
  finding. `api/runner.py` then imports transport only. Behaviour-preserving; a pure relocation plus
  import redirects. While there, `_connector_client_factory` (`registry.py:445-458`) has a single
  caller and is a two-line closure under a twelve-line docstring — inline it into `_mcp_connection` as
  `httpx_client_factory=lambda **_: connector_http_client(name, endpoint)` with the docstring's
  content as a comment, per the repo's own Rule of Three.

---

## `connectors/worker.py` does process setup inside the coroutine, after the bundle imports — the exact arrangement `server_entry.py` exists to forbid

- **Severity**: low
- **Location**: `src/chemclaw/connectors/worker.py:40-41` (inside `run_bundle_worker`) and `:65-67`
  (`main` = `asyncio.run(...)`), against `src/chemclaw/connectors/server_entry.py:1-29`
- **Trigger**: `python -m chemclaw.connectors.bo.worker` (or `calc`, `qm`).
- **Consequence**: `server_entry.py`'s docstring states as fact that *"each Temporal worker has
  `connectors/worker.py` … Each of them calls `configure_logging()` and `configure_telemetry()`
  **there**, because those are process setup and belong at a process boundary"*, and goes on to
  explain that its own `main` passes uvicorn an import *string* specifically so the app is built
  after logging is configured. `connectors/worker.py` does the opposite: each bundle's `worker.py`
  imports `activities` and `workflows` at module scope for their registration side effect, and only
  then calls `main`, which enters `asyncio.run` before `configure_logging()`/`configure_telemetry()`
  run. During the whole bundle import there are zero root handlers, therefore no `SecretRedactingFilter`,
  no `ContextFilter`, and no no-op meter provider — the three consequences `server_entry.py` lists
  by name.
- **Evidence** (reproducing `bo/worker.py`'s module body order and spying on `logging.Logger.handle`):

  ```
  records emitted during bundle import, BEFORE configure_logging(): 0
  root handlers at that point: []
  redaction/context filters installed at that point: []
  ```

  So the window is real and unguarded, but no *shipped* bundle currently logs or creates an
  instrument inside it. This is a latent asymmetry rather than a live leak today, and the honest
  statement is that it becomes live the first time a bundle module logs at import — which is not a
  rule anything enforces.
- **Fix**: move `configure_logging()`/`configure_telemetry()` from `run_bundle_worker` into
  `worker.main`, before `asyncio.run`, and have each bundle's `worker.py` call `main("<name>")`
  with the registration imports moved *inside* `run_bundle_worker` (via `importlib.import_module`
  on `chemclaw.connectors.<name>.{activities,workflows}`, which also deletes the three
  `# noqa: F401 — registration side effect` lines per bundle). That makes the setup a genuine process
  boundary and matches `server_entry.py`'s own argument. Behaviour-preserving for the shipped bundles
  (measured: 0 records in the window).

---

## What I checked and did not report

- **Dead code.** Every public symbol in the slice has a live non-test caller:
  `server_tools_module` (`cli/validate_templates.py:109`, `cli/validate_connectors.py:150`),
  `declared_note_types`/`declared_relations` (`kg/note.py:251`, `kg/relations.py:60`),
  `caller_provenance` (`connectors/bo/server/tools.py:393`), `SERVED_BY` (`agent/audit.py:242`),
  `find_job` (`durable/template_activities.py:179`, `cli/live_jobs.py:177`),
  `job_workflow_id` (`cli/live_jobs.py:180`), `prepare_job_launch` (`durable/template_activities.py:186`),
  `endpoint_tool_names`/`connector_tool_names` (`agent/chemclaw_agent.py:247`, `:415`),
  `state_changing_tool_names` (`agent/authz.py:187`), `job_names` (`cli/live_probes.py:128`),
  `skills_dirs`/`profiles_dirs` (`agent/langgraph_agent.py:537`, `agent/profile_discovery.py:80`),
  `bundle_queue` (`durable/template_activities.py:194`, `connectors/bo/workflows.py:85`),
  `probe_connectors`/`check_connectors_at_startup` (`api/app.py:114`, `:155`),
  `ConnectorsUnavailable`, `MissingConnectorCredential` (raised in-module, caught by class in the
  front door / documented in `values.yaml:526`). `queues.py` is an 18-line module for a one-line
  function and that is correct — six independent spellings of `connector-<name>` would be a job in a
  queue nobody polls.
  Four symbols (`turn_headers`, `absorb_connect_failure`, `request_timeout_seconds`,
  `connector_http_client`) are public with only in-module callers plus tests. Each carries a docstring
  saying so, and for the latter two the reason is sound (a test asserting the connect/read timeout
  relationship must exercise the same function a deployment does). Not reported.
- **Dynamic registration.** `JobSpec.workflow` is a Temporal type *name* resolved across the queue;
  `params_model`/`precondition` are dotted references resolved by `importlib`; `Endpoint`/`ConnectorAuth`
  are pydantic discriminated unions; bundle `activities`/`workflows` modules register by import side
  effect through `durable.registry`. Nothing in the slice was called dead on a grep alone.
- **Claims verified rather than trusted.** `ConnectorError` and `ConnectorJobError` really are both in
  `durable/publish.py::_BAD_DATA_TYPES` (lines 60 and 57), as their docstrings assert.
  `registry.health_url`'s suffix re-rooting produces the documented result for both shipped shapes
  (per-Service `…:8814/mcp` → `…:8814/healthz`, and the dev composite's `…:8810/chem/mcp` →
  `…:8810/chem/healthz`), including the degenerate cases where one URL is a prefix of the other.
  `_bind_caller_per_tool_call` really is applied after `_sanitize_tool_errors` and therefore really is
  the outer wrapper, as `server.py:411` claims.
- **Branchiness.** `ruff --select C901 --max-complexity=5` over the slice flags only
  `build_job_tool`/`launch` (8-9) and `connector_app` (6). `launch`'s complexity is four distinct
  exception outcomes plus the inline-wait branch, each with a distinct message the model reads; the
  duplication that *was* there (`_await_briefly`'s failure framing at two call sites) has already been
  extracted. Not worth splitting.
- **Module-global state.** `discovered()`'s `@cache` and `jobs._PARAMS_MODELS` are both process-lifetime
  caches over data that is fixed at import, with documented clearing seams; `_PARAMS_MODELS` is bounded
  by the number of distinct job definitions on disk. `caller.py`'s three `ContextVar`s are correctly
  task-local. Nothing here should be local that is global.
