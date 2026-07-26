# Connector Plan — one generic, standardized way to add a tool, a skill, or an agentic workflow

**Goal (user request, 2026-07-26):** make adding a capability a *declarative, standardized* act.
Every tool lives behind its own FastAPI server as MCP tools, or — when it runs long — as a Temporal
workflow, and both are registered through **one** mechanism that is still the existing Chemclaw
config substrate. The same mechanism registers the skills and the specialized agentic workflows that
belong to that capability.

**Method:** deep read of the status quo (every claim below is `file:line`-cited), an eight-question
design interview, and two API verifications against the installed `agent_framework` that the design
hinges on (§4.2, §5.1). Supersedes and completes `docs/audit/10-config-extensibility.md`, whose
seven follow-ups are all shipped (`BACKLOG.md:63-99`) — this plan is the next architectural step,
not a re-run of that one.

---

## 0. Executive summary

Chemclaw already has five extension seams, four of them good. The audit that built them optimized
for *in-tree, in-process* extension: a tool is a decorated Python function in the agent's own
process (`agents/tool_registry.py`), and MCP is a side channel for two stdio fingerprint servers.
The request inverts that default: **out-of-process is the norm, in-process is the exception.**

That inversion is worth making, and not only for tidiness:

- **Dependency isolation.** `rdkit`, `torch`, `bofire`, `tblite` are all in the front-door image
  today because tools are in-process. Each is a capability's dependency, not the chat service's.
- **Independent scale and blast radius.** A structure search and a chat turn have nothing in common
  operationally. `deploy/helm/chemclaw/templates/deployment-mcp.yaml` already anticipates exactly
  this and is *inert* today, gated on a networked transport (`values.yaml:65-75`).
- **A single registration story.** Today a capability is added in one of five different ways
  depending on what kind of thing it is. After this plan there is one: **a connector bundle.**

**The one mechanism.** A connector is a folder:

```
connectors/<name>/
  connector.yaml      # the validated manifest — the whole contract
  server/             # optional: the FastAPI+MCP app, when we own the capability
  workflows/          # optional: the Temporal workflow + worker, when it runs long
  skills/             # optional: the SKILL.md judgment that belongs to this capability
  profiles/           # optional: the agent profiles this capability enables
```

Discovered by folder (exactly as skills are), validated by a pydantic manifest (exactly as
`SkillManifest` validates `SKILL.md`), enabled by one config token, checked by
`make connector-validate` in CI. Adding a tool, a durable job, a skill or an agentic workflow is
**one folder and one config token — never an edit to orchestration code.**

**Decisions taken in the design interview** (§11 records the two places I deviate, with reasons):

| # | Decision |
|---|---|
| 1 | **Seam + domain slice.** Capability tools migrate out to connectors; the ~11 genuinely conversation-local tools stay in core *by documented rule*, not by omission. |
| 2 | **Agentic workflows: both, staged.** Declarative `AgentProfile` bundles now (Stage 2/3 of the existing seam); deterministic step templates specified as a later stage, gated on a second real use case. |
| 3 | **Durable jobs: core wrapper + connector-owned workflow.** A generic `ConnectorJobWorkflow` in core keeps idempotency, actor stamping, session push-back and the PR-gate; the connector owns the domain workflow and its worker, reached **by workflow-type string** so core imports nothing. |
| 4 | **In-tree bundle folder + config enable-token.** |
| 5 | **One FastAPI app per domain, one composite dev process.** |
| 6 | **Header contract + per-connector auth mode** (discriminated union). |
| 7 | **Degrade with a loud signal** when a connector is unreachable; `connectors_required` flips it to fail-fast. |
| 8 | **`mcp_servers` is removed**, not deprecated. Connectors are the only registration mechanism. |

---

## 1. Status quo — what exists, precisely

### 1.1 Tools: 31 in-process functions behind one decorator
`agents/tool_registry.py` is a 62-line registry: `@tool` keys a function by `fn.__name__` into
`_REGISTRY`, `registered_tools()` returns them in registration order. `build_agent` imports the 13
tool-bearing modules for their registration side effect (`agents/chemclaw_agent.py:41-54`) and
`_capability_tools()` assembles registry tools + MCP tools, then narrows by profile
(`chemclaw_agent.py:285-311`). Two function middlewares wrap **every** tool uniformly — audit
outermost so a denied call is still recorded, then `enforce_tool_authz`
(`chemclaw_agent.py:170-176`).

The 31 tools split cleanly along a line the code does not yet name:

- **Capability** (compute/search/screen — 20 tools): `compute_xtb_energy`, `predict_pka`,
  `predict_solubility`, `calculator_trust`, `report_measurement`, `green_metrics`,
  `stoichiometry_table`, `resolve_compound`, `render_structure`, `screen_hazards`, `find_notes`,
  `expand_note`, `find_knowledge_gaps`, `gather_evidence`, `suggest_next_experiment`,
  `submit_qm_job`, `get_qm_job_status`, `request_development_report`,
  `start_optimization_campaign`, `get_durable_job_status`.
- **Conversation plumbing** (11 tools) — these read or write the *turn's own state* through
  ContextVars and cannot be moved without inventing a callback channel from a remote process back
  into a live SSE stream: `ask_clarifying_question` (emits `QuestionSignal` into the turn's stream,
  `agents/turn_signals.py`), `list_attachments`/`read_attachment` (the turn's uploads),
  `remember_preference`/`forget_preference`/`recall_preferences` (actor state),
  `watch_for`/`stop_watching`/`list_watches` (standing queries bound to the session), and the two
  PR-gate writers `propose_knowledge_note`/`record_confirmed_answer` — which must stay in core
  because the PR-gate *is* the GxP boundary (safety rubric item 3, §10).

### 1.2 MCP: two stdio servers, config-as-registry
`settings.mcp_servers` (`chemclaw/config.py:548-561`) holds two `StdioMcpServerSpec` defaults
(`mcp-molfp`, `mcp-rxnfp`) launched as subprocesses of the agent's own pod;
`_mcp_capability_tools()`/`_mcp_tool()` dispatch on the `transport` discriminator to
`MCPStdioTool`/`MCPStreamableHTTPTool` (`chemclaw_agent.py:330-364`). `allowed_tools` is the
boundary keeping each server's `index_*` write tools off the agent (D-029). The servers themselves
are thin FastMCP wrappers over plain modules (`mcp_servers/molfp/server.py`), sharing
`mcp_servers/fpstore.py`. **Both already have HTTP-ready internals; only the transport is stdio.**

### 1.3 Durable work: four bespoke adapters over hand-maintained worker lists
`agents/qm_tools.py` and `agents/durable_tools.py` are four near-identical adapters: authorize →
dry-run check → `require_actor()` → deterministic workflow id → `client.start_workflow(...)` →
`record_job_started`. Each imports its workflow class directly
(`durable_tools.py:36-38`), and every workflow must appear in a hand-maintained module list on a
worker (`workers/background_worker.py:66-107`, `workers/hpc_worker.py:31-37`). Adding a durable
capability today means: write the workflow, add it to a worker list, write a bespoke adapter tool,
and hand-roll the id derivation and status mapping again.

The cross-cutting concerns those adapters and workflows carry are the valuable part and must
survive any generalization: idempotent workflow ids (`WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY`,
D-011), `require_actor()` (F4-T3 reject-if-absent), best-effort session push-back
(`workflows/notify.py::notify_session_best_effort`), PR-gated publication
(`workflows/publish.py::publish_note_best_effort`), and `record_job_started` for the live stream.

### 1.4 Skills, sources, profiles
Skills: `FileSkillsSource(settings.skills_dirs)` → `EnabledSkillsSource` → `RoleScopedSkillsSource`
(`chemclaw_agent.py:160-166`); frontmatter validated by `SkillManifest`, which already declares
`tools`/`mcp_servers` deps checked against the live registries by `make skill-validate`
(`scripts/validate_skills.py:100`). Sources: `sources/registry.py` — the flagship
`{name: factory}` + config token + discriminated `DataSourceSpec`. Profiles: `agents/profiles.py`
Stage 1 — an `AgentProfile` override bundle with exactly one `"default"` entry, no front-door
selection.

### 1.5 The gap this plan closes
There is no way to add a capability *as a deployable unit*. Tools, their skills, their durable
workflows, their config and their agent profile are added in four unrelated places, and three of
those places are Python edits in core. A capability is a concept the codebase cannot name.

---

## 2. Target architecture

```
┌────────────────────────── chemclaw core (front door + agent) ──────────────────────────┐
│  build_agent()                                                                          │
│    tools = registered_tools()            ← in-process conversation plumbing (11)         │
│          + connector_job_tools()         ← generated from every manifest `jobs:` entry    │
│          + connector_mcp_tools()         ← one MCP tool per manifest `endpoint:`          │
│    middleware = [audit, enforce_tool_authz]   ← unchanged, wraps ALL of the above          │
│                                                                                          │
│  connectors/registry.py   discover → validate → enable → build                            │
│  workflows/connector_job.py  ConnectorJobWorkflow: idempotency · actor · push-back · PR-gate│
└──────────┬──────────────────────────────────────────┬────────────────────────────────────┘
           │ MCP streamable-HTTP                       │ Temporal child workflow (by type name)
           │ + X-Chemclaw-* identity headers           │ on the connector's own task queue
           ▼                                           ▼
┌──────────────────────────┐                ┌──────────────────────────────┐
│ connector FastAPI app    │                │ connector Temporal worker    │
│  /healthz  /mcp          │                │  its own workflows+activities │
│  read/compute tools only │                │  returns ConnectorJobResult   │
└──────────────────────────┘                └──────────────────────────────┘
```

**Three invariants that make this safe** (elaborated in §10):

1. **A connector's MCP tools are read/compute-only.** Mutation goes through a `jobs:` entry (core
   gates it) or stays a core PR-gate tool. This is today's `allowed_tools` rule (D-029) promoted to
   a contract the validator enforces.
2. **Identity headers are advisory, never authorization.** Audit and authz run in core, before the
   call leaves the process. A connector may log the actor; it may never make an access decision on
   a header's word.
3. **A connector attenuates, never widens.** It cannot register a tool the agent's profile/authz
   layer does not gate, because both run over the assembled list.

---

## 3. The manifest — `connectors/<name>/connector.yaml`

```yaml
name: molfp                       # must equal the folder name (validated)
description: >-                   # ops-facing; not shown to the model
  Molecule fingerprints: ECFP4 similarity and substructure search over the indexed corpus.

endpoint:                         # optional — omit for a jobs-only connector
  transport: http                 # http | stdio  (discriminated)
  url: http://127.0.0.1:8811/mcp
  health_url: http://127.0.0.1:8811/healthz    # optional; omitted ⇒ unprobed
  request_timeout: 30
  auth:
    mode: none                    # none | bearer  (discriminated; see §11 for the Entra modes)
  tools:                          # the agent-facing allow-list. read/compute only (invariant 1)
    - similar_molecules
    - substructure_matches

jobs:                             # optional — each entry becomes ONE generated durable tool
  - name: submit_qm_job
    workflow: QMJobWorkflow       # the connector's Temporal workflow *type name* (a string)
    task_queue: hpc-jobs
    summary: Start a quantum-mechanical calculation and return its job id immediately.
    description: >-               # becomes the tool docstring the model reads
      Runs asynchronously as a durable Temporal workflow …
    params:                       # closed type set → a generated pydantic model → a real schema
      - {name: molecule_smiles, type: string, description: The molecule as a SMILES string.}
      - {name: method,          type: string, description: 'QM method, e.g. "B3LYP".'}
      - {name: basis_set,       type: string, description: 'Basis set, e.g. "def2-SVP".'}
    expensive: true               # ⇒ authorize_trigger() before any durable work
    publish_to_graph: false       # ⇒ core PR-gates a Note the result carries

skills: [reaction-search]         # SKILL.md folders under this bundle's skills/
profiles: [structure-search]      # profile yaml files under this bundle's profiles/
```

`extra="forbid"` throughout, as with every config model in the repo, so a misspelled key fails
CI instead of vanishing. `params[].type` is a closed set (`string`, `integer`, `number`, `boolean`,
`string[]`, `number[]`, `object`) — enough for any job's launch arguments, and closed so the
generated schema is always something the model can fill correctly.

**Config surface** (`ConnectorSettings`, one new mixin in `chemclaw/config.py`):

| Field | Default | Purpose |
|---|---|---|
| `connectors_dir` | `"connectors"` | pathsep list, like `skills_dir` — an operator can add a private bundle dir |
| `connectors_enabled` | `""` | pathsep list; empty = every discovered bundle (mirrors `skills_enabled`) |
| `connector_urls` | `{}` | per-connector endpoint override by name — Helm points at the in-cluster Service without editing a repo file |
| `connectors_required` | `false` | `true` ⇒ an unreachable enabled connector fails startup (GxP) instead of degrading |
| `connector_health_timeout_seconds` | `2.0` | bound on the startup probe |

`mcp_servers`, `McpServerSpec`, `StdioMcpServerSpec`, `HttpMcpServerSpec` are **deleted** (decision 8).

---

## 4. Identity, auth and transport

### 4.1 The header contract
Every HTTP connector call carries the turn's ambient context, read at call time from the same
ContextVars audit and authz read:

| Header | Source | Note |
|---|---|---|
| `X-Chemclaw-Actor` | `identity_context.get_current_actor()` | the Entra `oid`; absent off the authenticated path |
| `X-Chemclaw-Roles` | `identity_context.get_current_roles()` | space-delimited, sorted |
| `X-Chemclaw-Session` | `session_context.get_current_session_id()` | for correlating a connector's logs to a chat |
| `X-Chemclaw-Dry-Run` | `dialogue_tools.is_dry_run()` | defense in depth; core already refuses mutating paths |

This mirrors the ADR'd HPC identity bridge (`agents/identity/hpc_bridge.py`, §7.2): the downstream
runs under *our* service identity while the requesting user's oid travels with the request and is
logged, so the audit trail can always answer "which real user drove this".

**stdio connectors get no headers** — a subprocess of our own pod under our own identity is
already inside the trust boundary, and there is no request to attach them to.

### 4.2 Verified mechanism (this is why the design works)
`MCPStreamableHTTPTool` accepts `header_provider: Callable[[dict], dict[str, str]]`, invoked
**per `call_tool`** and injected via a ContextVar-backed httpx request hook
(`agent_framework/_mcp.py:3085-3110`). So per-turn identity needs *no* per-turn tool construction
and has *no* mutation race between concurrent turns — the provider simply reads the ambient
ContextVars at call time.

MAF's own `security.py:3425-3431` documents the trap: `header_provider` headers are **not** present
during `session.initialize()`, so auth passed that way 401s at connect. Therefore **auth goes on the
`httpx.AsyncClient` (`auth=`), identity goes on `header_provider`.** An `httpx.Auth` applies to every
request including initialize, and can refresh a token without rebuilding anything.

### 4.3 Auth modes
A discriminated union on `mode`:

- `none` — for stdio, and for loopback HTTP in dev. Validated: refused for a non-loopback URL
  unless `service_allow_insecure`, reusing the existing loopback rule (`service/app.py:62`).
- `bearer` — `token_env: CHEMCLAW_CONNECTOR_<X>_TOKEN`, read at call time (not import time) so a
  rotated secret is picked up, wrapped in an `httpx.Auth`. This is the three-secret in-cluster model.

Adding a mode is one variant plus one branch in `_auth_for`. §11 explains why the two Entra modes
are documented-but-unbuilt rather than stubbed.

---

## 5. Durable jobs — generic launch, connector-owned execution

### 5.1 The generated tool
For each `jobs:` entry, `connectors/jobs.py` builds:

- a params model via `pydantic.create_model` from the declared `params` (typed, so MAF derives a
  proper JSON schema — verified: MAF derives schemas from the signature, and a single
  pydantic-model parameter is already the in-repo idiom, `start_optimization_campaign(spec: CampaignSpec)`);
- an async function with `__name__ = <job name>` and a docstring assembled from `summary` +
  `description` + per-param descriptions, registered through the **existing** `register_tool` — so
  audit, authz, profile narrowing and the prose-contract validator all apply with zero changes;
- a body that is exactly the four bespoke adapters' shared shape, written once:

```python
authorize_trigger(name) if expensive
if is_dry_run(): return dry_run_notice(...)
actor = require_actor()
workflow_id = f"{connector}-{job}-{stable_hash([...params])}"      # D-011 idempotency
handle = await client.start_workflow(
    "ConnectorJobWorkflow", ConnectorJobInput(...),
    id=workflow_id, task_queue=settings.background_task_queue,
    id_reuse_policy=ALLOW_DUPLICATE_FAILED_ONLY)
record_job_started(handle.id, job)
return handle.id
```

`get_durable_job_status` already handles any workflow id generically
(`durable_tools.py::_status_of`) and becomes the one status tool for every connector job.

### 5.2 `ConnectorJobWorkflow` (core, `workflows/connector_job.py`)
```python
@workflow.defn
class ConnectorJobWorkflow:
    @workflow.run
    async def run(self, job: ConnectorJobInput) -> ConnectorJobResult:
        result = await workflow.execute_child_workflow(
            job.workflow,                      # a *string* type name — core imports nothing
            job.payload,
            id=f"{job.workflow_id}-child",
            task_queue=job.task_queue,
            retry_policy=BAD_DATA_RETRY,
        )
        if job.publish_to_graph and result.note is not None:
            await publish_note_best_effort(publish_memory_note_activity, [result.note])
        if job.session_id:
            await notify_session_best_effort(job.session_id, "job_completed",
                                            {"job_id": ..., "summary": result.summary, **result.data})
        return result
```

`publish_memory_note_activity(note: Note) -> str` already exists as the generic PR-gate publish
(`workflows/memory_jobs.py:80-83`), so the graph path is reused verbatim, not re-invented.

**The result envelope is the contract:** a connector workflow returns
`ConnectorJobResult(summary: str, data: dict, note: Note | None)`. `Note` is the existing frozen
model with its slug validators, so a connector cannot smuggle a malformed note past the PR-gate.

### 5.3 Why this is genuinely decoupling
The child is addressed by **type name + task queue**, both from the manifest. Moving a workflow
from core's worker to a connector's own worker is then *a one-line manifest change* — which is
exactly the property that makes the seam real rather than nominal. Existing in-flight histories are
untouched: the four current bespoke tools keep their workflow ids (§9 Stage B pins this with a test).

---

## 6. Skills and profiles from a bundle

- **Skills:** `settings.skills_dirs` gains every enabled bundle's `skills/` dir, so
  `FileSkillsSource` discovers them with zero new machinery, and `EnabledSkillsSource` /
  `RoleScopedSkillsSource` still gate them. `make skill-validate` gains one check: a bundled skill
  must be declared in its manifest's `skills:` list (so a stray folder is a CI failure, not a
  silently-shipped skill).
- **Profiles:** `agents/profiles.py` gains a loader for `connectors/<name>/profiles/<p>.yaml`
  validated by the existing `AgentProfile` model. This is the audit's deferred "Stage 3
  filesystem-discovered profiles", and the trigger it waited for — content authoring pressure from
  bundles — has now arrived.
- **Front-door selection (profile Stage 2):** `profile` on `POST /sessions`, carried through
  `_LiveSessions`, one cached agent per profile in `app.state.agents`. Small, and it is what makes
  "configure a new agentic workflow" reachable by a user rather than only by a redeploy.

---

## 7. Agentic workflows, staged (decision 2)

**Stage now — declarative profile bundles.** A profile is instructions + tool subset + MCP subset +
skill subset + harness mode/autonomy, authored as YAML in a bundle, selectable per session. No new
execution engine: the LLM still chooses tool order, and `docs/harness-konzept.md §11`'s deliberate
decision not to build MAF graph-workflows stands.

**Later stage — deterministic step templates.** `connectors/<name>/workflows/<t>.yaml` declaring an
ordered list of steps (tool call / skill load / sub-agent turn) executed by a core
`TemplateWorkflow` on Temporal. This is a real engine with real obligations — replay determinism,
versioning (`docs/workflow-versioning.md`), a resume story, and per-step authz. It is **specified
and not built**: the Rule-of-Three trigger is a second real use case that a profile provably cannot
express. Recorded in `BACKLOG.md` with that trigger, so it is a decision rather than an omission.

---

## 8. Migration map

| Connector | Tools | Notes |
|---|---|---|
| `molfp` | `similar_molecules`, `substructure_matches` | re-host the existing FastMCP server behind FastAPI/HTTP; keeps `index_molecule` off the allow-list |
| `rxnfp` | `similar_reactions` | same |
| `calc` | `compute_xtb_energy`, `predict_pka`, `predict_solubility`, `calculator_trust`, `report_measurement`, `green_metrics` | takes `tblite`/`torch` out of the front-door image |
| `chem` | `resolve_compound`, `render_structure`, `stoichiometry_table` | takes `rdkit` out |
| `safety` | `screen_hazards` | its own bundle: separately governed, separately auditable |
| `kg` | `find_notes`, `expand_note`, `find_knowledge_gaps`, `gather_evidence` | deepest coupling (knowledge repo + vector index); last to move |
| `bo` | job `start_optimization_campaign`, tool `suggest_next_experiment` | owns `bofire` + its own worker — the reference connector-owned durable workflow |
| `qm` | jobs `submit_qm_job`; `request_development_report` stays a core job initially | workflow stays on `hpc-jobs` until the HPC bridge moves with it |
| **core (stays)** | the 11 conversation-plumbing tools + `get_durable_job_status` | documented rule, §1.1 |

---

## 9. Staged plan with acceptance gates

Each stage is independently shippable and green under `make lint type test`.

### Stage A — the seam (core)
1. `connectors/manifest.py` — `ConnectorManifest` + `EndpointSpec` (stdio|http union) +
   `ConnectorAuth` (none|bearer union) + `JobSpec` + `JobParam`; every model `extra="forbid"`.
2. `connectors/registry.py` — discover folders across `connectors_dirs`, parse+validate, filter by
   `connectors_enabled`, apply `connector_urls` overrides, build MCP tools and job tools; raise with
   the valid keys on an unknown enabled name (the `sources/registry.py` idiom).
3. `connectors/identity.py` — the header provider + `httpx.Auth` per auth mode.
4. `connectors/jobs.py` — the generated durable tool factory (§5.1).
5. `workflows/connector_job.py` — `ConnectorJobWorkflow` + `ConnectorJobInput/Result`; registered on
   the background worker.
6. `chemclaw/config.py` — `ConnectorSettings` mixin; **delete** `mcp_servers` and its three models.
7. `agents/chemclaw_agent.py` — `_capability_tools()` reads connector MCP + job tools; delete
   `_mcp_tool`/`_mcp_capability_tools`.
8. `connectors/health.py` + `/readyz` detail + a `chemclaw_connectors_unhealthy` gauge on `/metrics`;
   `connectors_required` fail-fast.
9. `scripts/validate_connectors.py` + `make connector-validate`; teach `validate_skills` and
   `validate_prose_contract` to read connector tool names instead of `settings.mcp_servers`.
10. `.env.example`, Helm values/templates, `docs/runbook.md`, ADRs in `DECISIONS.md`.

**Gate A:** a bundle with only a manifest registers its tools; audit+authz demonstrably wrap a
connector-sourced tool *and* a generated job tool (the Spike-1 property, now for real); an unknown
enabled connector fails loud; a non-loopback `auth: none` is refused; `make lint type test` green.

### Stage B — the reference bundles + the durable path proven end to end
1. `connectors/molfp/`, `connectors/rxnfp/`: manifests + `server/app.py` (FastAPI mounting
   `FastMCP.streamable_http_app()` at `/mcp`, plus `/healthz`), keeping the existing modules as the
   capability. `mcp_servers/` becomes their implementation, unmoved.
2. `scripts/connectors_dev.py` + `make connectors` — one process mounting every enabled local
   bundle's app on one port (decision 5's dev ergonomics).
3. The durable path is proven against a real `WorkflowEnvironment` with a **fixture connector**
   (`tests/fixtures/connectors/`): a manifest job whose connector-owned workflow returns a
   `ConnectorJobResult`, asserting the whole chain — idempotent id, actor attribution, child on its
   own task queue, PR-gated note, session push-back.

**The four existing bespoke adapters are deliberately NOT migrated in this stage.** `submit_qm_job`,
`request_development_report` and `start_optimization_campaign` wrap workflows that return typed
domain results their callers consume (`QMJobResult` feeds the calibration ledger and the note
mapper), not the `ConnectorJobResult` envelope. Converting them now would mean either changing three
tested durable workflows' return types — orphaning in-flight histories for no functional gain — or
stacking a third wrapper layer around each. Both are worse than waiting: in Stage C their code moves
into its bundle and the moved workflow returns the envelope *directly*, which is one change instead
of two. Until then core durable capabilities and connector durable capabilities coexist by design,
and the generic path is what every *new* durable capability uses. `agents/qm_tools.py` and
`agents/durable_tools.py` keep a pointer to this decision so the debt is visible where the code is.

**Gate B:** the agent reaches fingerprint search over HTTP with identity headers observed by a test
server; a fixture connector's durable job completes, pushes back to its session, and PR-gates its
note under a real `WorkflowEnvironment`.

### Stage C — domain connectors
`calc`, `chem`, `safety`, then `kg`, one bundle at a time; `bo` moves its workflow to its own worker
and task queue, proving §5.3. Helm gains one Deployment+Service per enabled connector (generalizing
the inert `deployment-mcp.yaml`) and a NetworkPolicy allowing only front-door→connector.

**Gate C:** the front-door image no longer needs `tblite`/`bofire`; each connector's tests run
against its own app; end-to-end chat unchanged.

### Stage D — agentic workflow configuration
Profiles from bundles + `POST /sessions {profile}` + optional profile-name RBAC gate.

**Gate D:** two profiles selectable per session; the attenuate-not-authorize invariant re-proven
across the connector surface.

### Stage E — step templates (specified, gated)
Not built. Trigger recorded in `BACKLOG.md`.

---

## 10. The safety rubric — how each item survives

| Invariant | How it holds |
|---|---|
| **GxP audit** | `make_audit_middleware` wraps the *assembled* tool list. Connector MCP tools and generated job tools are in that list, so both are audited identically — including a denied call, recorded before the exception surfaces. |
| **Per-tool authz** | `enforce_tool_authz` runs over the same assembled list. A generated job tool is keyed by its manifest `name`, so `tool_role_gates` and `DEFAULT_WRITE_TOOL_GATES` address it exactly as they address `submit_qm_job` today. |
| **PR-gate** | The two PR-gate writer tools stay in core. A connector job's note is published by *core's* workflow through the existing `propose_note` path, and `Note`'s validators run on the way in — a connector cannot write to the graph directly. |
| **`require_actor`** | In the generated job body, before any durable work — the same reject-if-absent placement the four bespoke adapters use. |
| **Dry-run** | Enforced in core for every job tool. Connector MCP tools are read/compute-only by contract (invariant 1, validated), so there is nothing for a dry run to suppress; the header is defense in depth. |
| **Fail-fast validation** | Manifest models are `extra="forbid"` pydantic; `make connector-validate` is a CI gate mirroring `make skill-validate`; an unknown enabled connector, a job with a duplicate tool name, a non-loopback `auth: none`, or a `tools:` entry naming a mutating tool all fail before serving. |
| **Attenuation** | Profiles narrow the assembled list; authz runs after. A connector can add capability to the *offered* set, never to the *permitted* set. |

**New risk this plan introduces, stated plainly:** a connector is a network dependency in the tool
path. Mitigations: the startup probe + unhealthy gauge (decision 7), per-connector
`request_timeout`, `connectors_required` for deployments that prefer death to degradation, and a
NetworkPolicy that keeps connectors reachable only from the front door.

---

## 11. Deviations from the interview answers, and why

1. **Entra auth modes are documented, not built.** The chosen option listed
   `entra_client_credentials` and `entra_obo`. OBO requires the user's *raw* access token, and
   `service/auth.py::Principal` deliberately carries only `oid`/`upn`/`roles` — adding the raw token
   to the ambient turn state is a security-relevant change that no current caller needs, and
   `BACKLOG.md:501` already blocks live Entra edges on a real tenant. Building either mode now would
   ship an unverifiable code path against a guessed token shape — precisely the "recorded-response
   tests assert one's own assumptions" trap DA-10 called out. So: `none` + `bearer` are built and
   tested; the union documents that a mode is one variant plus one branch; and the manifest is
   forward-compatible because `mode` is a discriminator.
2. **`suggest_next_experiment` stays inline.** It answers in-turn today and is not a durable job; it
   moves to the `bo` connector as an MCP tool in Stage C, not as a `jobs:` entry.

---

## 12. Open questions (answer before the stage that needs it)

- **Stage C / `kg`:** does the `kg` connector mount the knowledge repo read-only and share the
  vector index, or does it own re-indexing? Affects whether `NoteReindexWorkflow` moves with it.
- **Stage C / Helm:** one image with a component switch (today's `CHEMCLAW_COMPONENT` pattern) or
  per-connector images? One image is simpler and keeps the single-image promise of F6; per-connector
  images are what actually buys the dependency isolation. Probably: one image now, split when a
  connector's dependency set justifies it.
- **Stage D:** should a profile name be RBAC-gated like a skill (`skill_role_gates`)? Only if a
  profile can ever be more permissive than the caller — it cannot (attenuation), so this is a
  usability question, not a security one.
