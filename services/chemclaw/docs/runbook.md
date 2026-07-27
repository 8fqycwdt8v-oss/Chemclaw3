# Operations runbook (admin)

How a system/admin configures and troubleshoots Chemclaw. Everything environment-dependent
comes from the one config source (`chemclaw/config.py`, every field mirrored in `.env.example`,
overridable as `CHEMCLAW_<FIELD>`); this runbook covers the four recurring admin tasks.

## Prerequisites

- Local dev stack: `make up` starts Temporal (dev server + UI) and Postgres/pgvector;
  `make down` stops it. The **Temporal Web UI is at http://localhost:8080** — the first place
  to look at a running/failed job's event history. Frontend gRPC is `localhost:7233`.
- The full gate before calling any change done: `make check` (ruff + `mypy --strict` + pytest).

## Logging & troubleshooting

- **Verbosity is one switch.** Set `CHEMCLAW_LOG_LEVEL=DEBUG` (default `INFO`) and restart the
  affected worker. `configure_logging()` runs at each worker's entrypoint; no code change.
- **What gets logged:** each worker logs its connected address/namespace/queue and registered
  workflows on startup; every agent tool call is audited (name, arguments, outcome, latency —
  `agents/audit.py`); the ELN sync logs `ingested/rejected` counts plus a WARNING per rejected
  entry, per skipped broken export file, and one aggregated WARNING naming export files that
  arrived too late to be ingested (recovery: section (v)); `DEBUG` adds calculation cache
  hit-vs-compute (the "why did this recompute?" answer).
- **Changing workflow code:** a control-flow change deployed while a run is in flight fails that run
  on replay. Follow `docs/workflow-versioning.md` (patch-gate or drain) for any release touching a
  `@workflow.defn` body.
- **A stuck/failed job:** open the Temporal UI (:8080) → the workflow → event history; cross-check
  the worker's stderr logs. A worker not picking up jobs is usually the wrong queue/namespace —
  the startup log line shows exactly what it connected to.
- **Database down:** connections fail fast with `ConnectionError: Postgres unreachable at
  <host>: <cause>` (password redacted). It is a retryable infra fault, so Temporal retries the
  activity; fix the DSN/host and it recovers.
- **OpenTelemetry (optional):** set `CHEMCLAW_OTEL_ENABLED=true` and point
  `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector. Requires the OpenTelemetry SDK + OTLP exporter
  extras installed; enabling without them raises a directive error.

## Exposing the front door (the two settings that decide whether it boots)

- **`CHEMCLAW_ENTRA_REQUIRED=true` for anything reachable beyond localhost.** With it off, every
  request runs as the shared dev principal with all authorization gates open, so the service
  **refuses to start** on a non-loopback bind (the `0.0.0.0` default) with `SECURITY:
  entra_required is False but the service binds a non-loopback interface …`. That message means the
  guard worked, not that the deployment is broken: set `CHEMCLAW_ENTRA_REQUIRED=true` (plus
  `CHEMCLAW_ENTRA_TENANT_ID` and `CHEMCLAW_ENTRA_AUDIENCE`, which are validated together at
  startup), or bind loopback for local dev. `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the conscious
  opt-out and boots with a loud warning instead.
- **`CHEMCLAW_ENTRA_CLIENT_ID` was removed.** Settings is `extra="forbid"`, so a stale export of it
  aborts startup with a validation error naming the field — unset it in any inherited environment.
- **`CHEMCLAW_NOTE_REPO_DIR` must be set on any host that submits notes — the default is always
  wrong in a deployment.** It ships as `.` (a dev convenience), which resolves to the process CWD.
  Every submission opens with `git reset --hard` + `git clean -fd`, so pointing it at the checkout
  the service itself runs from would wipe uncommitted work there — `_require_dedicated_checkout`
  refuses before any git command runs, with `note_repo_dir '.' resolves to <path> — the checkout
  this process is running from`. That error is the guard doing its job, not a broken deployment:
  point the variable at a **dedicated, writable, non-shallow clone** of the knowledge repo, used by
  nothing else (`git checkout -B note/<id>` switches the whole working tree, and
  `--force-with-lease` needs real history). The Helm chart already supplies one —
  `knowledge.noteRepoPath`, default `/var/lib/chemclaw/note-repo`, provisioned by
  `deploy/knowledge-sync.sh`. Deliberately *not* the read replica the retriever serves from.
  Leaving it unset outside Helm is the quieter failure: `knowledge-sync.sh` logs
  `CHEMCLAW_NOTE_REPO_DIR unset — no submitter clone provisioned` and skips the clone, so the
  first note submission is the thing that discovers it.
- **Note submission is serialized per host.** Keep the background worker at one replica (see
  `deploy/helm/chemclaw/values.yaml`); the PR-gate's checkout lock is host-local, so a second
  replica needs the distributed lock still open in `BACKLOG.md`.

## Talk to the agent from a terminal (testing)

The production ingress is Teams/Copilot with Entra-ID SSO (architektur.md §7). For local
testing there is a CLI: `make chat` (or `uv run chemclaw --admin`). It needs `ANTHROPIC_API_KEY`
in the environment — the chat client preflights it and fails with a clear message otherwise.

- **Admin mode is required.** Entra auth is enforced at the *front door* (F4), and this CLI has no
  browser OIDC token to validate, so it runs only with `--admin`: it bypasses auth, advertises every skill, and stamps the audit trail with
  `CHEMCLAW_CLI_ADMIN_ACTOR` (default `admin@localhost`). Without `--admin` it refuses and exits
  non-zero — "no authentication" stays a conscious choice, not a default.
- **One-shot vs. REPL:** `uv run chemclaw --admin -m "which solvent next for …?"` asks one
  question, prints the answer to stdout, and exits (scriptable); with no `-m` it is an
  interactive chat (the thread accumulates; `exit`/Ctrl-D to quit).
- **Attribute the run:** `--actor alice@lab` overrides the audit actor. `--audit-postgres`
  persists the tool-audit trail to Postgres (default is log-only).

## (i) Add a skill

Drop a `skills/<name>/SKILL.md` (front-matter schema + template in `skills/README.md`) and
restart the agent — discovery is automatic. To add a second skills directory (e.g. team-private
skills), set `CHEMCLAW_SKILLS_DIR` to an OS-path-separator list, like `PATH`
(`skills:/opt/team-skills`).

A skill that teaches *one capability's* tools belongs in that connector's bundle instead
(`connectors/<name>/skills/<skill>/`, declared in its `connector.yaml` — see (iv)), so the judgment
ships and is reviewed with the capability it is about. One that spans several stays in `skills/`.
Either way `make skill-validate` checks its declared `tools:` against the live surface, in-process
and out, so a skill cannot outlive the tool it teaches.

## (ii) Add or repoint a database

Set `CHEMCLAW_POSTGRES_DSN` and run `make db-migrate` (applies `infra/sql/*.sql` in filename
order; each migration is idempotent, so re-running is safe). A new capability's table is a new
hand-written `infra/sql/00N_*.sql`. Note the bit-width coupling: a `bit(N)` fingerprint column
must match `CHEMCLAW_ECFP_BITS` / `CHEMCLAW_DRFP_BITS` (see `config.py`). Applied migrations are
recorded in the `schema_migrations` ledger with a checksum (D-034), so re-running is safe and an
edited already-applied file is flagged as drift rather than silently skipped.

## (iii) Add / switch an ELN source

Sources live on the generic seam (`sources/registry.py`): `eln-json` (free-text export) and
`eln-ord` (native ORD) are the ingest sources, `graph` the retrieve source. Set which are active
with `CHEMCLAW_DATA_SOURCES` (a comma list, e.g. `graph,eln-json,eln-ord`) plus each ingest
source's export directory (`CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`). The durable sync
ingests **every** active ingest source, each with its own high-water cursor (keyed by registry
name in `sync_cursors`), so sources advance independently; the memory jobs read the same active set.
A *new* ELN source is one new adapter class satisfying the `ElnAdapter` contract plus one
`DATA_SOURCES` entry in `sources/registry.py` and its key in `CHEMCLAW_DATA_SOURCES`. Validate an
export with `make eln-validate`.

## (iv) Add a capability — a tool, a durable job, and their skills (a **connector**)

A capability is a **connector bundle**: one folder declaring everything it contributes. There is no
second mechanism — `CHEMCLAW_MCP_SERVERS` is gone (D-110).

```
connectors/<name>/
  connector.yaml      # the manifest — the whole contract
  server/app.py       # optional: the FastAPI+MCP app, when we own the capability
  workflows.py        # optional: its Temporal workflow, when the work runs long
  skills/             # optional: the SKILL.md judgment that belongs to this capability
  profiles/           # optional: the agent profiles it enables
```

**To add one:**

1. Create the folder with a `connector.yaml`. For an MCP capability, declare an `endpoint:` and the
   `tools:` the agent may call — read/compute only; `make connector-validate` refuses a mutating
   name, because mutation belongs on the job path or on a core PR-gate tool.
2. For a long-running capability, declare a `jobs:` entry naming the Temporal **workflow type** and
   **task queue** its own worker serves. Its workflow returns a `ConnectorJobResult`
   (`summary`, `data`, optional `Note`); core's `ConnectorJobWorkflow` supplies the idempotent job
   id, the actor attribution, the PR-gate publish and the session push-back. A job declares its
   arguments inline (`params:`) or by reference (`params_model: module:Model`) when the input is a
   structured domain object. Mark it `expensive: true` to require a privileged role before any
   durable work starts.
   *If the same request is sometimes fast and sometimes slow* — a reaction energy over two small
   species versus eight with Hessians — add `inline_wait_seconds: <n>`. The launcher then waits up
   to that long and returns the result if it lands, or the job id if it does not, so one tool serves
   both cases and the model never has to guess a cost. Keep `n` comfortably under
   `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS`: the wait is spent inside a turn. Cancelling the turn does
   not cancel the run — it completes, caches and pushes back regardless. `connectors/calc` is the
   worked example (five jobs, one workflow, one queue, its own worker).
3. Run `make connector-validate`. It checks the manifest, that declared skills/profiles exist (and
   that no undeclared ones are hiding in the bundle), the read-only tool surface, and that every
   job can actually be built.
4. Nothing to enable: an empty `CHEMCLAW_CONNECTORS_ENABLED` runs every discovered bundle. Set a
   pathsep list to narrow (and to fix the order — tool order is part of the prompt); an unknown name
   there is a startup error, not a silently missing capability.

**Running them.** `make connectors` serves every local bundle in one process and prints the
`CHEMCLAW_CONNECTOR_URLS` to point the front door at. In a cluster, each bundle is its own
Deployment + Service (`.Values.connectors.<name>.enabled`), and the chart *computes*
`CHEMCLAW_CONNECTOR_URLS` from that same block, so addresses cannot drift from the pods that exist.

**Configuration.** `CHEMCLAW_CONNECTORS_DIR` (pathsep, like `PATH` — prepend a private bundle dir to
override a shipped one), `CHEMCLAW_CONNECTORS_ENABLED`, `CHEMCLAW_CONNECTOR_URLS`,
`CHEMCLAW_CONNECTORS_REQUIRED`, `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS`,
`CHEMCLAW_CONNECTOR_JOB_TIMEOUT_SECONDS`. A connector's request timeout and auth mode are per-manifest
(`endpoint.request_timeout`, `endpoint.auth`); the `bearer` mode names an env var, so no credential is
ever written into a bundle.

**Troubleshooting.** `GET /readyz` reports each enabled connector as `healthy`, `unreachable` or
`unprobed` (no `health_url` declared — honest for a third-party server), and
`chemclaw_connectors_unhealthy` on `/metrics` counts the unreachable ones. An unreachable connector
costs its tools for that turn, not the turn itself; set `CHEMCLAW_CONNECTORS_REQUIRED=true` to fail
startup instead. Verify a bundle standalone with `uvicorn connectors.<name>.server.app:app` and check
`/healthz`; tool *discovery* needs no database, but *invoking* a search does.

**What ships today.** Six bundles: `molfp` and `rxnfp` (fingerprint search), `safety` (the hazard
screen), `chem` (bench chemistry over RDKit), `calc` (the fast calculators and the calibration
ledger), and `bo` (Bayesian optimization — the one that also owns durable work, so it runs a second
Deployment for its own Temporal worker; set `worker: true` on a bundle in the chart to get one). 
**What stays in core is a rule, not an omission** (D-115), and `tests/test_tool_registry.py` pins the
set so adding to it is a reviewed edit:

- **Conversation plumbing** — anything reading or writing the turn's own state
  (`ask_clarifying_question`, attachments, preferences, watches). Another process does not have the
  turn.
- **The two PR-gate writers** (`propose_knowledge_note`, `record_confirmed_answer`) — the GxP
  boundary. A connector reaches the gate only by returning a note in a job envelope, for core to
  publish.
- **The knowledge-graph reads** (`find_notes`, `expand_note`, `find_knowledge_gaps`, and the
  `gather_evidence` sweep over them). The graph is core's *data layer*, not a capability: thirteen
  core modules import `kg`, so a bundle would move three thin tools and leave every one of those
  imports behind — a zero dependency win and a second read path to one note tree. Re-indexing stays
  in core with it.
- **`submit_qm_job`** and the report — the first needs the HPC identity bridge, the second's closure
  (retrievers, embedding index) is what core keeps for `gather_evidence` anyway. Both still return
  results the generic status tools read.

## (iv-b) Add a specialized agent (a **profile**)

A profile is a named override bundle over the one agent: its instructions, the tools it may use, and
whether the plan/execute harness runs. It only ever *narrows* — the audit trail, the per-tool
authorization gate and the skill role gates all run after it — so a profile gives a caller a smaller,
sharper agent, never a wider one.

**To add one:** drop `profiles/<name>.yaml`. The filename is the profile name (a `name:` key inside
is refused, so the two cannot disagree). A profile about a single capability goes in that connector's
bundle instead (`connectors/<name>/profiles/<p>.yaml`, declared in its manifest) so it ships and is
reviewed with the capability.

```yaml
instructions: >-
  You are Chemclaw in property-lookup mode. …
tool_names:            # spans both halves of the surface: in-process tools AND connector tools
  - predict_pka        # a `calc` connector tool — `calc` is attached with its allow-list cut to these
  - ask_clarifying_question
harness_enabled: true  # optional; omit any field to inherit the global default
```

`tool_names` narrows the in-process tools *and* each connector's agent-facing allow-list, dropping
connectors left with nothing; `mcp_server_names` is the coarser dial that selects whole connectors. A
name nothing provides is a startup error, not a silently smaller agent. See
`profiles/property-lookup.yaml` for a worked example.

**To use one:** `POST /sessions {"profile": "property-lookup"}`. The profile is fixed for the
session's life — a conversation whose tools changed underneath it would have a thread that no longer
matches its own history — and an unknown name is a 400 at session creation. One agent is built and
cached per profile.

**Known limit:** a session rehydrated after a pod restart comes back on the *default* profile. The
owner row records who owns a session, not which agent it was talking to; the conversation resumes
with the full tool surface rather than a narrowed one. Persisting the profile is the fix if a
deployment needs the narrowing to survive a restart.

## (iv-c) Add a fixed procedure (a **template**)

Reach for this only when the *order* must not vary. A profile is the first answer — it configures an
agent and lets the model choose the sequence, which is what you want while a procedure is still being
figured out. A template pins the sequence and runs it as a durable Temporal job: use it for a
validated protocol, a standard screening sweep, a report that must always gather the same evidence in
the same order. `templates/README.md` has the full comparison and the field reference.

**To add one:** drop `templates/<name>.yaml`. The filename is the name here too.

```yaml
summary: Screen a molecule for hazards and write a briefing.
inputs:
  - {name: smiles, type: string, description: The molecule to screen.}
steps:
  - id: hazards                      # unique; how later steps refer to this one
    kind: tool                       # or `job` (await a connector's durable job) or `agent`
    tool: screen_hazards
    arguments: {smiles: ["${inputs.smiles}"]}
  - id: brief
    kind: agent                      # a model turn — fixed sequence, free reasoning inside a step
    prompt: "Summarize for a chemist: ${steps.hazards.result}"
```

Substitution is `${inputs.<name>}` and `${steps.<id>.result}` and nothing else — no conditionals or
loops by design. A whole-string reference keeps the value's type; one inside a longer string
interpolates JSON text. Forward references, unknown inputs and duplicate step ids are refused at load.

Run `make template-validate` (CI does): it checks that every step names a tool, job or profile that
actually exists, so a pinned procedure cannot fail on step four in production.

**To use one:** the template becomes a generated `run_<name>` tool the model can call like any
durable job — same authorization gate, same audit trail, same dry-run behaviour. It returns a job id;
poll with `get_durable_job_status`. Re-running with identical inputs returns the existing id rather
than paying twice.

**Editing one is safe.** A run pins the resolved template into its workflow input, so an edit cannot
change a run already in flight and there is no migration; the change applies to later runs only.

`CHEMCLAW_TEMPLATES_ENABLED` narrows which discovered templates are advertised (empty = all);
`CHEMCLAW_TEMPLATE_STEP_TIMEOUT_SECONDS` bounds one step.

## (v) Re-ingest a rejected ELN entry (after fixing the source record)

The durable sync rejects an entry that fails validation (bad structure, mass-balance
mismatch) and **advances past it** — a rejection is deterministic bad data, so re-fetching
it unchanged would only re-reject it. Each rejection is reported in the run's
`IngestSummary.rejected` (visible in the Temporal workflow result) and logged as a `WARNING`
carrying the entry id, the reason, **and the entry's timestamp**.

To re-ingest one after correcting its source record upstream: start the `ElnSyncWorkflow`
with `since` set to just before that entry's timestamp (from the rejection log/summary). The
sync re-fetches from there and re-ingests everything after it; ingestion is idempotent
(id-keyed fingerprint upserts + a stable note branch), so the already-ingested entries in
that window are harmless no-ops and only the corrected entry newly succeeds. There is no
automatic re-drive by design (KISS) — re-ingestion is a deliberate, admin-triggered action.

The **same procedure backfills a late-arriving export**: a file dropped into the export directory
after the sync's overlap window, carrying an older payload timestamp, is filtered out permanently
and reported as `… export file(s) arrived after the sync cursor but carry an older timestamp …`.
Start the sync with `since` set before those entries' timestamps to pull them in.

## (vi) Change a fingerprint definition (ECFP radius/bits or DRFP bits)

`CHEMCLAW_ECFP_RADIUS`/`_ECFP_BITS`/`_DRFP_BITS` define the fingerprints. A **width** change
(`*_BITS`) also needs a matching `bit(N)` schema change (`infra/sql/002,003`) or inserts fail
loudly. Every fingerprint row records the *definition* it was indexed under, and similarity
search returns only rows matching the store's current definition — so after any definition
change, previously-indexed rows fall out of search (safe: no wrong scores, just missing hits)
until you **re-index** them (re-run the ELN sync / re-add molecules). If search comes back
empty after a config change, that is the tell: the index predates the new definition and needs
rebuilding.

## (vii) Read eval-drift alerts

The scheduled `EvalDriftWorkflow` re-scores the committed eval case-set and raises one alert per
metric that moved past the relative noise band (`CHEMCLAW_EVAL_DRIFT_EPSILON`). Two surfaces, both
intentional:

- **The background worker's log** is where you meet a regression: `eval drift: metric 'f1' scored
  … vs baseline … (delta …)`, or `… disappeared from the run …` when a metric stopped being scored
  at all (that is *not* a score of 0.0 — usually a removed or erroring case).
- **The durable record** is the `system-eval-drift` channel in `session_events`; delivery is
  must-deliver, so a failed write fails the workflow run rather than dropping the alert. **No UI
  consumes this channel by design** — read the backlog with
  `SELECT created_at, payload FROM session_events WHERE session_id = 'system-eval-drift' ORDER BY
  created_at DESC LIMIT 20;`.

Over the committed (deterministic) case-set this is a *deployment-consistency tripwire*: it fires
when the deployed code, cases, and `evals/baseline.json` are inconsistent. After a deliberate
metric change, refresh the committed baseline — otherwise every scheduled run re-alerts.
