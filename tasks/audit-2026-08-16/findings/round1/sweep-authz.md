# Sweep: authorization consistency (round 1)

Scope: every gate named in the brief, read against each other, plus the three questions the brief
asked to settle by running code. Postgres and Temporal were up; every claim below has a script or a
route-table dump behind it. Scripts live under `/tmp/audit/`.

Three findings. Two of the brief's three questions produced a defect; the third (the connector
bearer middleware) produced a **negative result**, recorded at the end because "we checked and it
holds" is part of the answer.

---

## Any authenticated principal can read every other principal's durable job — inputs, results, molecule structures

- **Severity**: critical
- **Location**:
  - `/home/user/Chemclaw3/src/chemclaw/api/routes/jobs.py:22` (`list_jobs`) and `:41` (`get_job`)
  - `/home/user/Chemclaw3/src/chemclaw/agent/durable_tools.py:247` (`job_status`), `:283` (`_recorded_status`)
  - `/home/user/Chemclaw3/src/chemclaw/durable/job_record_store.py:100` (`read_job_record`)
  - `/home/user/Chemclaw3/src/chemclaw/agent/authz.py:130` (`READ_ONLY_TOOLS` contains `find_past_jobs` and `get_durable_job_status`)
- **Trigger**: `entra_required=True`, `session_store="postgres"`. Principal `alice-oid` runs a
  connector job; the `ConnectorJobWorkflow` wrapper writes its `job_records` row. An unrelated,
  role-less principal `mallory-oid` then issues `GET /jobs` followed by
  `GET /jobs/{job_id}`. No shared session, no shared role, no privileged role.
- **Consequence**: full cross-tenant read of durable job output. `GET /jobs` is an unfiltered
  enumeration of every job id in the deployment together with each run's free-text `rationale` and
  `summary`; `GET /jobs/{job_id}` then returns that run's entire `result` object. The record is
  designed to be self-contained ("reading a row back reconstructs the run without Temporal, without
  the launching conversation" — `durable/job_record.py:39`), so `result` is the whole scientific
  payload: for a BO campaign every candidate it evaluated, for a QM run the converged geometry and
  energies. `requested_by` is stored on the row and is never consulted on any read path.

  The same read is available to *the model* through two ungated tools: `find_past_jobs` returns the
  same listing, and `get_durable_job_status` returns the same result. Both sit in
  `READ_ONLY_TOOLS`, neither is in `DEFAULT_WRITE_TOOL_GATES`, and under the shipped
  `tool_authz_default="allow"` `authorize_tool` lets both through for any account. `find_past_jobs`
  docstring explicitly instructs the model to chain them: *"Take the `job_id` of a promising hit to
  `get_durable_job_status` for that run's full result."*

  This also **invalidates the stated defence of the report path**. `durable_tools._report_id` reasons
  at length that a report's workflow id must include the actor and roles because *"`job_status()` …
  applies no actor check, so an id two principals can both derive is an id either can collect"*
  (`durable_tools.py:118-140`). That mitigation is id-unguessability. For connector jobs the id is
  simply published by `GET /jobs` / `find_past_jobs`, so the premise the mitigation rests on does not
  hold for the class of job that actually writes records.

- **Evidence**:

  Route table (`/tmp/audit/routes.py`) — every route authenticates, and these two carry no
  resource-level gate at all:

  ```
  OK  ['GET'] /jobs           deps=['require_principal']
  OK  ['GET'] /jobs/{job_id}  deps=['require_principal']
  ```

  Live reproduction against Postgres (`/tmp/audit/repro_jobs.py`), with
  `settings.entra_required = True` and `require_principal` overridden to a role-less
  `Principal(oid="mallory-oid")`:

  ```
  GET /jobs -> 200
  [ { "job_id": "qm-compute_dft_energy-AUDITALICE",
      "connector": "qm", "job": "compute_dft_energy",
      "rationale": "Project VULCAN: screen the undisclosed lead series before the patent filing",
      "summary": "lead 42 barrier 18.3 kcal/mol", ... } ]
  GET /jobs/{id} -> 200
  { "job_id": "qm-compute_dft_energy-AUDITALICE", "status": "completed",
    "summary": "lead 42 barrier 18.3 kcal/mol",
    "result": { "geometry": "SECRET 3D COORDS OF UNDISCLOSED LEAD",
                "candidates": ["CC-lead-42", "CC-lead-43"],
                "energy_hartree": -1234.5678 },
    "rationale": "Project VULCAN: screen the undisclosed lead series before the patent filing" }
  ```

  The row was written with `requested_by="alice-oid"`, `session_id="sess-alice"`.

  Same read through the agent's own tools (`/tmp/audit/repro_agent_jobs.py`), ambient identity
  `mallory-oid` with role `chemist`:

  ```
  authorize_tool(find_past_jobs): ALLOWED
  authorize_tool(get_durable_job_status): ALLOWED
  find_past_jobs hit: qm-compute_dft_energy-AUDITALICE | Project VULCAN: screen the undisclosed lead series...
     requested_by = alice-oid  session = sess-alice
     result       = {'geometry': 'SECRET 3D COORDS OF UNDISCLOSED LEAD', ...}
     payload      = {'smiles': 'CC(=O)Oc1ccccc1C(=O)O-SECRET-LEAD-42', 'functional': 'wB97X-D'}
  ```

  The `deps.py` module docstring calls `GET /jobs/{job_id}` "authenticated, deliberately unscoped:
  the route needs a caller to exist, but nothing about the answer depends on *which* caller it is."
  That sentence is true of `GET /profiles` and `GET /schedules`. It is false here — the answer is
  another named principal's data, and the row carries their name.

  `jobs.py:29-37` argues the *listing* is unscoped by design, citing `find_past_jobs`'s
  cross-project-learning position and noting "nothing here pretends the row is private". That
  argument covers `rationale`/`summary` (a search index over what has been tried). It is silently
  extended to `GET /jobs/{job_id}`, whose docstring makes no unscoped claim at all and whose payload
  is a different object: the full result blob, deliberately excluded from `JobRecordSummary` for
  context reasons and thereby never re-argued for access.

- **Fix**: separate the two questions the listing and the detail read ask.
  1. Keep `GET /jobs` / `find_past_jobs` as the cross-project *discovery* surface, but return only
     the fields the D-004/KM-9 argument covers — it already does (`JobRecordSummary`), so no change.
  2. Scope the detail read. Add an owner check in `job_status`'s record path and in the route:
     return the full `result` only when `record.requested_by == get_current_actor()` or the caller
     holds `entra_privileged_role_set`; otherwise answer with the summary-level projection and a
     403/`"this run belongs to another requester"`. `requested_by` is already on every row, so this
     is a predicate, not a schema change.
  3. `job_status` is shared with the live-Temporal path, which has no record to read
     `requested_by` from. Put the check where the record is decoded (`_recorded_status`) *and* pass
     the launching actor into `ConnectorJobResult` so the live path can apply the identical rule —
     otherwise the gate exists only after Temporal history expires, which is the worst possible
     shape.
  4. Because `job_workflow_id` deliberately excludes the requester (D-011 de-duplication), one run
     can legitimately have several requesters. Store them: a `job_record_requesters` join table (or
     a `requested_by text[]` column) makes "did this principal ask for this run" answerable without
     giving up the shared-run property.

---

## `entra_expensive_actions` is inert: an operator-gated connector job launches for any authenticated user

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/jobs.py:302-303` (`prepare_job_launch`)
  — `if job.expensive: authorize_trigger(job.name)`. Compare
  `/home/user/Chemclaw3/src/chemclaw/agent/authz.py:246` (`expensive_actions()`) and
  `/home/user/Chemclaw3/src/chemclaw/core/config/entra.py:38-45`.
- **Trigger**: `entra_required=True`, `entra_privileged_roles="chem-admin"`,
  `entra_expensive_actions="compute_reaction_energy"` — i.e. exactly the configuration the settings
  comment describes ("An action named in `entra_expensive_actions` … may run only for a user holding
  at least one role in `entra_privileged_roles`"). A user holding only `chemist` calls the
  `compute_reaction_energy` job tool.
- **Consequence**: the gate never runs. `prepare_job_launch` short-circuits on the *manifest's*
  `expensive:` flag before it ever asks `authorize_trigger`, and `compute_reaction_energy` declares
  `expensive: false`. `authorize_trigger` is the only consumer of `entra_expensive_action_set`, so
  the operator's list is enforced for **no connector job it does not already cover via the manifest**
  — which is the entire reason the knob exists (`authz.py:266`: "`entra_expensive_actions` remains,
  and remains a union — it is how an operator gates something the manifests do not call expensive").
  No other gate compensates: under the default `tool_authz_default="allow"` the job's tool name is
  not in `tool_role_gates` and not in `DEFAULT_WRITE_TOOL_GATES`, so `authorize_tool` allows it too.

  The same hole is on the template path, which shares `prepare_job_launch`
  (`durable/template_activities.py:107`), so a template step naming that job is equally ungated.

  The config validator reinforces the false belief: `entra.py:166` *specifically* validates that
  `entra_expensive_actions` is accompanied by `entra_privileged_roles`, i.e. it treats the knob as
  live and refuses a configuration that would "refuse it to every user" — for a knob that refuses it
  to no one.

- **Evidence** (`/tmp/audit/repro_expensive.py`, ambient identity `bob-oid` with role `chemist`):

  ```
  expensive_actions() = ['compute_dft_energy', 'compute_interaction_energy',
                         'compute_reaction_energy', 'request_development_report',
                         'sample_conformers', 'start_optimization_campaign']
  authorize_trigger(compute_reaction_energy): REFUSED — user bob-oid lacks a privileged role for compute_reaction_energy
  prepare_job_launch(compute_reaction_energy): LAUNCH ALLOWED, payload keys: ['kind', 'level', 'products', 'reactants']
  ```

  The action is in the gate's set; the gate agrees it should be refused; the only production caller
  never asks it.

- **Fix**: delete the `if job.expensive:` condition and call `authorize_trigger(job.name)`
  unconditionally in `prepare_job_launch`. `authorize_trigger` already returns immediately for an
  action outside `expensive_actions()` (`authz.py:370`), so the condition is a duplicate of a check
  the callee makes — and a *narrower* duplicate, which is exactly how it diverged. The manifest flag
  keeps its meaning through `expensive_actions()`'s derivation; the operator's list regains its.
  Add a test that names a non-`expensive` job in `entra_expensive_actions` and asserts
  `prepare_job_launch` raises.

---

## `tool_role_gates` keys are validated nowhere, while the sibling `skill_role_gates` keys are — a typo silently ungates a tool

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/agent/authz.py:329` (`authorize_tool`,
  `settings.tool_role_gates.get(tool)`); contrast
  `/home/user/Chemclaw3/src/chemclaw/cli/validate_skills.py:175-192`
  (`skill_role_gates` keys are checked against the discovered skill set and `make skill-validate`
  fails on an unknown one).
- **Trigger**: an operator writes
  `CHEMCLAW_TOOL_ROLE_GATES='{"start_optimisation_campaign": ["chem-admin"]}'` (British spelling; the
  tool is `start_optimization_campaign`), or leaves a trailing space in a key. `make check` is green,
  `make connector-validate` is green, every validator is green.
- **Consequence**: the intended gate does not exist. Under the shipped `tool_authz_default="allow"`
  an unmatched key means the tool falls through to the default and is callable by every
  authenticated user — the failure direction is *open*. The same class of typo in the skill map is a
  hard validation failure with a message that names the consequence out loud ("so it gates nothing
  and the skill stays visible to every caller"). Two sibling role maps, the same failure mode, one
  guarded and one not.

  `authz.py:79-82` already records the general principle for the neighbouring constant — "A
  deny-list entry for a name nothing serves reads as a control and is not one, and nothing validates
  these names against the live tool surface" — but only as a note about
  `DEFAULT_WRITE_TOOL_GATES`; the operator-facing map next to it has the same property and no note.

- **Evidence** (`/tmp/audit/repro_gate_typo.py`, `entra_required=True`,
  `tool_authz_default="allow"`, identity `bob-oid` holding only `chemist`):

  ```
  correct key              authorize_tool(start_optimization_campaign) -> REFUSED
  correct key              authorize_tool(compute_xtb_energy)          -> ALLOWED
  British-spelling typo    authorize_tool(start_optimization_campaign) -> ALLOWED
  trailing space           authorize_tool(start_optimization_campaign) -> ALLOWED

  skill-validate with a typo'd skill_role_gates key -> exit 1
      "skill_role_gates names unknown skill 'no-such-skill', so it gates nothing and the skill
       stays visible to every caller; discovered: [...]"
  ```

- **Fix**: give `tool_role_gates` the check its sibling has. The live tool surface is already
  computable without a model or a connector process — `core.tool_registry.registered_tool_names()`
  ∪ every enabled manifest's `endpoint.tools` ∪ every declared `jobs:` name ∪
  `templates.registry.template_tool_names()` (the same union `authz.side_effecting_tools()` builds
  from three of those four). Add the assertion to `make connector-validate` (it already loads all
  four registries) and to `tests/test_authz.py` so the shipped chart's own map is checked in CI.
  Same treatment for `DEFAULT_WRITE_TOOL_GATES`, which is a hand-kept list of four names and has
  never been checked against that union either.

---

## Negative result: the connector bearer middleware does fail closed (question (a))

Recorded because the brief asked for it and the answer is "the control holds", which is worth as
much as a finding.

`connectors/server.py::BearerAuthMiddleware.dispatch` has exactly three paths to `call_next`
without a credential check, and all three were exercised (`/tmp/audit/repro_bearer.py`,
`/tmp/audit/repro_bearer2.py`, driven over the real streamable-HTTP transport with a real
`FastMCP` app):

| path to `call_next` | reached when | measured |
| --- | --- | --- |
| `request.url.path in ("/healthz", "/metrics")` | probe/scrape | 200, by design |
| `token_env is None` — manifest says `mode: none` | `bo`, `calc`, `molfp`, `rxnfp` | anonymous `/mcp` accepted |
| `token_env is None` — no manifest for this name ships beside the module | synthetic apps (tests) | anonymous `/mcp` accepted |

Everything else refuses. Specifically, for the two shipped bearer bundles:

```
chem, bearer, token env UNSET:            declared='CHEMCLAW_CHEM_TOKEN'   anon /mcp -> 401
chem, bearer, token env = "" (empty):     declared='CHEMCLAW_CHEM_TOKEN'   anon /mcp -> 401
safety, bearer, token env UNSET:          declared='CHEMCLAW_SAFETY_TOKEN' anon /mcp -> 401
chem, registry pointed at an empty dir:   declared='...AUTH_UNRESOLVED'    anon /mcp -> 401
chem, a prepended dir with broken YAML:   declared='...AUTH_UNRESOLVED'    anon /mcp -> 401
chem, token set, no header / wrong token: 401 / 401       (correct token: request proceeds)
```

So the two fail-closed claims in `_declared_bearer_env`'s docstring — "unreadable manifests" and
"ships a manifest that this process did not discover" — are both true as written, and a bearer-mode
manifest with an unset or empty token env answers 401 rather than comparing against `""`.

Two facts the question is adjacent to, stated because they are properties of the deployment rather
than of this middleware, and neither is a defect in it:

- **Every connector this repository actually serves is `mode: none`.** `bo`, `calc`, `molfp` and
  `rxnfp` ship a `server/` here and all four declare `auth: mode: none`; `chem` and `safety` are the
  only bearer bundles and their servers live in `Chemclaw3-mcp`. So in this repo the middleware is a
  pass-through on every process it actually runs in, and the only ingress control on `/mcp` is the
  `chemclaw-connector-ingress` NetworkPolicy. That policy exists
  (`deploy/helm/chemclaw/templates/networkpolicy.yaml:132`), selects every enabled connector
  component, and admits chemclaw's own pods plus `networkPolicy.monitoringNamespaces` — the chart
  says out loud that the monitoring grant is not metrics-only because the port also carries `/mcp`.
  Consistent, and argued.
- `HttpEndpoint._a_networked_endpoint_carries_a_credential` validates the *declared* URL, which is
  loopback for all four, so it never fires for them even though the chart moves them to in-cluster
  Services. The docstring says this is deliberate and gives the reason. Noted, not filed.

---

## Answer to question (c): what `entra_required=False` turns off, and whether production can reach it

Measured (`/tmp/audit/repro_dev_mode.py`), `entra_privileged_roles="chem-admin"`, identity present
but role-less:

```
### entra_required = True
    authorize_tool(propose_knowledge_note):   REFUSED
    authorize_trigger(compute_dft_energy):    REFUSED
    _owner_authorizes(None, stranger):        False
    _owner_authorizes('alice', stranger):     False
    _is_reviewer(no roles):                   False
### entra_required = False
    authorize_tool(propose_knowledge_note):   ALLOWED
    authorize_trigger(compute_dft_energy):    ALLOWED
    _owner_authorizes(None, stranger):        True
    _owner_authorizes('alice', stranger):     False    <-- still enforced
    _is_reviewer(no roles):                   True     <-- everyone is a reviewer
```

So the flag turns off, in one place each: token validation entirely (`require_principal` returns the
fixed `dev-user` principal without reading the `Authorization` header), `authorize_tool`,
`authorize_trigger`, the owner check *for owner-less rows only*, and `_is_reviewer` — which means
every caller may decide any note proposal (`POST /proposals/{id}/decision`) and cancel any durable
job (`DELETE /jobs/{id}`). What it does **not** turn off: a *recorded* owner still authorizes
(`_owner_authorizes('alice', stranger)` is False in both modes — the SEC-3 fix is real), the skill
role gate (`RoleScopedSkills` reads ambient roles with no dev bypass, so a gated skill is hidden in
dev — fails closed), and the document-share entitlement (`ShareDocumentRetriever._entitled` likewise
— dev principal has no roles, so a gated share returns nothing).

**Can production reach that branch?** Not by omission. `api/middleware._refuse_unauthenticated_exposure`
runs at `create_app` and was exercised directly:

```
boot host=0.0.0.0   allow_insecure=False : REFUSED  (RuntimeError, app does not start)
boot host=0.0.0.0   allow_insecure=True  : BOOTS    (with the SECURITY warning logged)
boot host=127.0.0.1 allow_insecure=False : BOOTS
```

and `deploy/helm/chemclaw/values.yaml:382` sets `CHEMCLAW_ENTRA_REQUIRED: "true"` in the shared
ConfigMap, which every deployment template consumes through one `chemclaw.envFrom` helper
(service, workers, connectors, migrate job, schedules job) — checked; no component gets a different
env. The only route to an exposed dev-mode front door is an operator explicitly setting
`CHEMCLAW_SERVICE_ALLOW_INSECURE=true`. I could not construct a misconfiguration that reaches it
accidentally.

One residual, not filed as a finding because it is documented and bounded: the guard reads
`settings.service_host` (the *configured* bind), so a deployment that binds via a command-line
`--host 0.0.0.0` while leaving `CHEMCLAW_SERVICE_HOST` at a loopback value would boot unguarded.
`deploy/entrypoint.sh` uses the setting, so the shipped path is consistent.

---

## What else was checked and found sound

- **Route coverage.** Every `APIRoute` on the app was walked programmatically and its full
  dependency tree resolved (`/tmp/audit/routes.py`). All 22 non-probe routes carry
  `require_principal`; `/healthz`, `/readyz`, `/metrics` deliberately do not. The only other mount is
  `StaticFiles` for the dev chat page. Unscoped-but-authenticated routes are `/schedules`,
  `/profiles`, `/notes/{id}`, `/jobs`, `/jobs/{job_id}` — the first three carry no per-principal
  data; the last two are the finding above.
- **Session/hold/proposal gates.** `_owner_authorizes` / `_refuse_unless_owner` are applied on every
  `{session_id}` route including the SSE stream, the tool-result blob read and the plan decision;
  `owned_approval` is a route dependency on both approval-detail routes; `_visible_proposal`'s
  divergence from the shared owner rule is correct (reviewer sees any, dev-open comes from
  `_is_reviewer` not `_owner_authorizes`) and the decision route's 403→422→404 ordering is preserved
  by calling it in the body rather than as a dependency, exactly as its docstring claims.
- **Ambient identity.** `run_turn` binds actor *and* roles from the validated `Principal`
  (`api/routes/turns.py:165-166`), and it is the only turn entry point. Temporal-side re-binding
  (`report_workflow`, `memory_jobs`, `template_activities`) restores the recorded requester; the
  report workflow carries `requested_roles` into the retrieval activity, so the entitlement-gated
  share is evaluated against the requester's own roles rather than the worker's.
- **Subagents.** `_subagents` compiles the helper through `build_langgraph_agent`, so it inherits
  the full middleware chain (audit → authz → dry-run → repeat → plan gate) rather than deepagents'
  bare `SubAgent` dict. `governed_roster` refuses a spec upstream would have assembled.
- **Skill gate.** The narrowing is enforced at the *backend* (`NarrowedSkillsBackend`), covering
  `ls`/`read`/`glob`/`grep` and refusing the write half, so a role-gated skill is not one guessed
  path away. This is the one place in the tree where a listing filter would have been insufficient
  and it was not used.
- **Actor-scoped stores.** Durable memories are namespaced by an actor digest
  (`agent/scratchpad.memory_namespace`); preferences and subscriptions go through `require_actor()`;
  attachments and tool-result blobs are session-scoped behind `resolve_session`. No cross-tenant read
  found in any of them.
- **Metrics.** No exported series carries a principal label, so the unauthenticated `/metrics`
  endpoint discloses no identity (checked against every `chemclaw_*` name emitted in `src/`).
- **Connector identity headers.** `X-Chemclaw-*` are bound for provenance only; the only reader is
  `bo/server/tools.py`'s `caller_provenance`. Nothing branches on them for access.
- **`/events/knowledge-merged`.** The authorization-shaped half (closing proposals) requires a valid
  HMAC over the raw body in addition to an authenticated principal; the idempotent reindex half does
  not. Correct split.
- **MCP tool naming.** `load_mcp_tools` is called without `tool_name_prefix`, so connector tool names
  reach `authorize_tool` unprefixed and `tool_role_gates` keys addressed by bare tool name do match
  (verified against the installed adapter's signature). No silent mismatch there.
