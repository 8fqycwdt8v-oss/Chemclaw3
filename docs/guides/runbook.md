# Operations runbook (admin)

How a system/admin configures and troubleshoots Chemclaw. Everything environment-dependent
comes from the one config source (`src/chemclaw/core/config/`, every field mirrored in `.env.example`,
overridable as `CHEMCLAW_<FIELD>`); this runbook covers the four recurring admin tasks.

## Prerequisites

- Local dev stack: `make up` starts Temporal (dev server + UI) and Postgres/pgvector;
  `make down` stops it. The **Temporal Web UI is at http://localhost:8081** — the first place
  to look at a running/failed job's event history. Frontend gRPC is `localhost:7233`.
- The full gate before calling any change done: `make check` (ruff + `mypy --strict` + pytest).

## Logging & troubleshooting

- **Verbosity is one switch.** Set `CHEMCLAW_LOG_LEVEL=DEBUG` (default `INFO`) and restart the
  affected worker. `configure_logging()` runs at each worker's entrypoint; no code change.
- **What gets logged:** each worker logs its connected address/namespace/queue and registered
  workflows on startup; every agent tool call is audited (name, arguments, outcome, latency —
  `src/chemclaw/agent/audit.py`); the ELN sync logs `ingested/rejected` counts plus a WARNING per rejected
  entry, per skipped broken export file, and one aggregated WARNING naming export files that
  arrived too late to be ingested (recovery: section (v)); `DEBUG` adds calculation cache
  hit-vs-compute (the "why did this recompute?" answer).
- **Changing workflow code:** a control-flow change deployed while a run is in flight fails that run
  on replay. Follow `docs/guides/workflow-versioning.md` (patch-gate or drain) for any release touching a
  `@workflow.defn` body.
- **A stuck/failed job:** open the Temporal UI (:8081) → the workflow → event history; cross-check
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
  Every submission creates `note/<id>` in that clone and force-pushes it to the clone's origin, so
  pointing it at the checkout the service itself runs from would publish agent-authored notes into
  the source repository — `_require_dedicated_checkout` refuses before any git command runs, with
  `note_repo_dir '.' resolves to <path> — the checkout this process is running from`. That error is
  the guard doing its job, not a broken deployment: point the variable at a **dedicated, writable,
  non-shallow clone** of the knowledge repo, used by nothing else (`--force-with-lease` needs real
  history, and so does the worktree each submission branches from). The Helm chart already supplies one —
  `knowledge.noteRepoPath`, default `/var/lib/chemclaw/note-repo`, provisioned by
  `deploy/knowledge-sync.sh`. It is also the tree the retriever serves from, because it has to be:
  `settings.knowledge_path` is `note_repo_dir` joined with `knowledge_dir` and there is no second
  resolution, so the sync publishes into that subdirectory (taking the submitter's checkout lock
  while it does) rather than to a path of its own. Since D-2026-08-05 that working tree is a
  *reader* surface only: a submission happens in a private worktree under `.git/` and never
  switches it, and the sync is the one thing that writes it. The *shallow* replica at
  `knowledge.sync.checkoutPath` is what it publishes from, never what anything reads.
  Leaving it unset outside Helm is the quieter failure: `knowledge-sync.sh` logs
  `CHEMCLAW_NOTE_REPO_DIR unset — no submitter clone provisioned` and skips the clone, so the
  first note submission is the thing that discovers it.
- **Note submission is serialized per host.** Keep the background worker at one replica (see
  `deploy/helm/chemclaw/values.yaml`); the PR-gate's checkout lock is host-local, so a second
  replica needs the distributed lock still open in `docs/planning/BACKLOG.md`. Per-submission
  worktrees did not change this — they buy isolation from *readers*, not throughput — and the lock
  matters more than it did: each submission sweeps leftover worktrees under the shared clone, which
  is safe only because the lock guarantees no sibling submission owns one. On a filesystem where
  `flock` is not honoured (some NFS/ReadWriteMany setups) that assumption fails, and the blast
  radius is a live worktree deleted mid-submission rather than two interleaved branches.

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

## Live-test the whole stack (Temporal + workers + Postgres, then the model)

The lane that proves the durable half actually works. Everything before this section tests one
layer; this runs the path a durable capability really takes — agent tool → `ConnectorJobWorkflow`
on `background-jobs` → the bundle's workflow on `connector-<name>` → the calculation cache →
`job_records` → the audit chain — against a real broker and a real database.

It exists because that path had never been run end to end. The Temporal tests use the
time-skipping test server with no model and no database; the live probe run
(`docs/archive/live-grounded-2026-08-03.md`) had **no Temporal worker at all**, and its probe files
said so in their own headers. See `docs/decisions/D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md`.

```sh
make live-infra     # Postgres/pgvector + Temporal — uses docker-compose when a daemon is
                    # reachable, otherwise builds and starts them natively (infra/live/)
make db-migrate     # apply infra/sql
make live-up        # connectors (:8810), the four Temporal workers, the front door (:8000)
make live-status    # what is running
make live-jobs      # STAGE A: a real durable job, no model needed
make live-probes    # STAGE B: the probe corpus through the front door (needs ANTHROPIC_API_KEY)
make live-down && make live-infra-down
```

**Stage A (`make live-jobs`) needs no model credential and is the load-bearing one.** It launches
`compute_reaction_energy` through the *real* generated job tool and then asks the live system six
questions that have mechanical answers — the workflow's terminal state from Temporal, the cache row
and the `job_records` row from Postgres, whether a duplicate launch rejoins rather than recomputes,
whether a job whose worker is wedged comes back *pending* rather than hanging or crashing, and
whether the audit chain still verifies. Nothing is scored from prose. The report lands in
`tasks/live-test/transcripts/durable-smoke.md`.

**Two prerequisites the corpus layer needs, or the probes measure an empty database.** `make
reindex` fills `note_index`; the fingerprint tables are filled only as a side effect of the ELN
sync, so start `ElnSyncWorkflow` on `background-jobs` once. The PR-gate needs a *dedicated* clone —
`bootstrap.sh` creates `.live/knowledge-repo` and `processes.sh` points `CHEMCLAW_NOTE_REPO_DIR` at
it, because `note_repo_dir` defaults to the working checkout and every submission force-pushes a
note branch to that clone's origin, so the gate refuses it (G4) and the whole
knowledge-contribution half of a run silently disappears.

**`make live-storm` is the third stage, and it needs no model at all.** Point the lane at the mock
(`CHEMCLAW_LLM_PROVIDER=openai_compatible`, `CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1`,
`CHEMCLAW_LLM_MODEL=mock`) and `make live-up` starts `chemclaw.cli.mock_llm` alongside everything
else. The storm then drives load, adversarial model behaviour and the front door's own limits with
zero LLM calls — the mock reports how many requests it served, which is how the run *proves* that
rather than asserting it. It is an HTTP mock of the **Responses** API, not chat-completions, and
not an injected chat client: the streaming assembler, the middleware stack, budget admission, the
audit sink and the session store all sit between the socket and the agent.

**`make live-soak` repeats the storm for as long as you leave it and fits what drifts.** It asks the
one question no single run can — does anything grow that should not — so it is checkpointed per
round to a JSON-lines record under `.live/` and re-running it *resumes*: on a host whose container is
reclaimed on a timer, a reclaim costs one round rather than the run. `make live-soak-report` fits
every series.
It deliberately runs families `BCDFGH` rather than all eight, because family A restarts the front
door at each admission cap and family E SIGKILLs a worker, and the RSS of a process that has just
been replaced is not a series. Ask `make live-storm` whether the system survives being disturbed;
ask this one what drifts when it is not.

**Stage B (`make live-probes`) adds the model.** With the workers up, the `du-*` probes in
`data/evals/probes/durable.yaml` exercise durable work for the first time, and every workflow id a
probe launches is resolved against Temporal rather than taken from the turn's account of it — a job
tool returns an id the moment the launch is *accepted*, so "I started a job" can be true about work
that never ran. Pass `ARGS='--only du-01 --no-judge'` to narrow a run.

Notes on the stack itself:

- **The front door will not boot without a model credential.** It builds the agent during startup,
  so `ANTHROPIC_API_KEY` (or `CHEMCLAW_LLM_PROVIDER=openai_compatible` plus a base URL) is required
  for Stage B. `make live-up` skips it and says so when neither is set; the workers still come up,
  which is why Stage A is independent of it.
- **The lane pins `CHEMCLAW_SERVICE_HOST=127.0.0.1`.** With `entra_required=false` the front door
  refuses a non-loopback bind (SEC-2) and the default is `0.0.0.0`, so without this it would
  correctly fail to start.
- **Each worker gets its own probe port** (9000-9003). `worker_http` otherwise has them all
  contend for 9000; setting the port to 0 would silence the readiness signal this lane polls.
- **Without a Docker daemon** the bootstrap builds pgvector and the Temporal CLI from git clones.
  That is not a preference: `temporal.download` and `codeload.github.com` archives are both denied
  by a filtering egress proxy, while git-over-HTTPS and the Go module proxy are not. PostgreSQL
  server headers are the one prerequisite it cannot install for you
  (`apt-get install postgresql-server-dev-16`).

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
must match `CHEMCLAW_ECFP_BITS` / `CHEMCLAW_DRFP_BITS` (see `core/config/fingerprints.py`).
Applied migrations are recorded in the `schema_migrations` ledger with a checksum (D-034), so
re-running is safe and an edited already-applied file is flagged as drift rather than silently
skipped.

**Always follow `make db-migrate` with `make db-grants`** (the Helm hook Job runs both, in that
order, so this only concerns migrating by hand). The grants are *not* in the tracked migration set
and are re-applied on every deploy on purpose: a table added by a new migration ships with no grant
until they run, so a split-principal deployment breaks on first use of it with
`InsufficientPrivilege`. They no-op where no `chemclaw_app` role exists.

### Splitting the database principal (optional, GxP)

By default one credential does everything, and `infra/sql/006` describes `audit_events` as
"append-only by contract" — which the hash chain and the signed anchors *detect* violations of, and
which nothing prevented. To make it append-only in fact
(D-2026-08-05-append-only-by-grant-not-by-contract):

1. Create a login role the application runs as, owning nothing:
   `CREATE ROLE chemclaw_app LOGIN PASSWORD '…';`
2. Point `CHEMCLAW_POSTGRES_DSN` at it, and put the schema owner's DSN in
   `CHEMCLAW_POSTGRES_MIGRATION_DSN` — in the chart, `secrets.migrationKeys`, which is mounted on
   the migration hook Job and on nothing else.
3. `make db-migrate && make db-grants`.

Verify it took: as `chemclaw_app`, `INSERT INTO audit_events …` succeeds and
`DELETE FROM audit_events` fails with `InsufficientPrivilege`. The owner credential can still
rewrite the trail — this narrows who holds that power and for how long, it does not remove it — so
the chain and the anchors remain the evidence. The role also needs no `CREATE EXTENSION` right;
that stays with the migrator, which is where `vector` already required superuser on most managed
Postgres.

**`job_records` is the one table a chemist's answers now depend on** (023, D-157): every finished
connector job writes what it ran, on what arguments, its whole result, and the reason it was
started. It is what `get_durable_job_status` reads for a job Temporal has forgotten and what
`find_past_jobs` searches, so a deployment that skips this migration — or that runs with
`CHEMCLAW_SESSION_STORE=memory`, which selects the null sink — silently loses every finished run
once its workflow history expires. Nothing prunes it (`durable/retention.py` says why).

**`observations` is opt-in and stays empty until you say so** (025, D-161). It holds the ungated
tier: cross-project patterns the agent noticed, in Postgres rather than the graph because they are
explicitly not truth. `CHEMCLAW_OBSERVATIONS_ENABLED=true` is what registers its Schedule and what
lets `recall_observations` return anything; without it the table is created and never written.
Turning it on is a deliberate choice — it is the only knowledge surface the agent can read that no
human signed off. Watch two numbers once it runs: the retirement rate (close to the mining rate
means the miners are producing noise) and the promotion rate (zero over a quarter means the tier is
a write-only log and should be removed, not defended).

## (iii) Add / switch a data source (an ELN, a warehouse, a retrieval index)

A source is a folder with a `datasource.yaml`, exactly as a capability is a folder with a
`connector.yaml` (D-120). The shipped ones are under `sources/`: `eln-json` (free-text export) and
`eln-ord` (native ORD) are ingest-only, `graph` (plus the off-by-default `vector`/`lexical`) is
retrieve-only. Set which are active with `CHEMCLAW_DATA_SOURCES` (a comma list, e.g.
`graph,eln-json,eln-ord`). The durable sync ingests **every** active ingest source, each with its
own high-water cursor keyed by source name in `sync_cursors`, so sources advance independently; the
memory jobs read the same active set.

**A new source is a folder and a config token — no core edit.** Write the adapter (satisfying
`ElnAdapter` for ingest or `SourceRetriever` for retrieval), declare it, enable it. See
`src/chemclaw/ingest/sources/README.md` for the manifest fields; `make datasource-validate` checks that every declared
half resolves and that its `config:` binds to the callable's signature.

**A second instance of an existing adapter needs no code at all** — for example a staging ELN drop
beside the production one. Mount a directory of manifests and put it first in
`CHEMCLAW_DATA_SOURCES_DIR` (OS-pathsep list, earlier wins), so a deployment can also override a
shipped source without rebuilding the image:

```yaml
# /etc/chemclaw/sources/eln-json-staging/datasource.yaml
name: eln-json-staging
description: The staging ELN drop.
ingest: eln.json_adapter:JsonExportAdapter
config:
  export_dir: /mnt/eln/staging
```

The bare `eln-json`/`eln-ord` sources carry no `config:`, so they fall back to
`CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`. Validate an export with `make eln-validate`.

**Two sources describe themselves in a `binding:` rather than in code**, because the shape they read
exists before this system does and differs at every site — a warehouse's tables
(`docs/guides/warehouse-eln-concept.md`) and a mounted file share's directory tree
(`docs/guides/sharedrive-concept.md`). For those, `make datasource-validate` only checks that the
half resolves and the kwargs bind; `python -m chemclaw.cli.validate_datasources --construct` is what
actually parses the binding, and it is the check to run after mounting your own manifest directory.

**A mounted SMB/CIFS share** is the one source that is not reached over the network at all: enable
`documentShare` in `values.yaml` so the volume lands read-only on the background worker, point the
binding's `mount:` at the same path, and name the source in `CHEMCLAW_DATA_SOURCES`. Run
`make share-estimate SHARE=<source>` against the real mount **before** enabling it — it walks the
share, reads nothing, and reports what would be indexed and what cannot be read. The full concept,
including how the share's AD group becomes an entitlement, is in `docs/guides/sharedrive-concept.md`.

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
2. For a long-running capability, declare a `jobs:` entry naming the Temporal **workflow type**. The
   queue is *not* declared — it is `connector-<name>`, derived at dispatch, because a bundle's worker
   serves only what the bundle's own modules registered (D-150). Its workflow returns a
   `ConnectorJobResult`
   (`summary`, `data`, optional `Note`); core's `ConnectorJobWorkflow` supplies the idempotent job
   id, the actor attribution, the PR-gate publish and the session push-back. A job declares its
   arguments inline (`params:`) or by reference (`params_model: module:Model`) when the input is a
   structured domain object. Mark it `expensive: true` to require a privileged role
   (`CHEMCLAW_ENTRA_PRIVILEGED_ROLES`) before any durable work starts — the declaration is what the
   trigger gate reads, so no matching `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` entry is needed. Under
   `entra_required` a deployment that declares **no** privileged role refuses every expensive job
   rather than allowing it — so an exposed deployment that wants these jobs usable must set
   `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` — and that setting **alone** is the whole remedy.
   `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` remains only for gating something no manifest declares
   expensive, and config validation requires it beside a role in one direction only: actions named
   with no role are rejected (nobody could pass that gate), roles named with no actions are the
   normal production configuration.
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

**A server somebody else runs** — a platform team's model endpoint, a vendor's FastAPI/MCP service.
Everything above is unchanged (the manifest says what the capability *is*, and that does not depend
on who hosts it); only the deployment differs, per D-2026-08-09-a-connector-we-do-not-run:

1. In the bundle's `connector.yaml`, declare the `endpoint:` as usual. It must speak MCP
   streamable-HTTP, and because it is not loopback it must carry a credential —
   `auth: {mode: bearer, token_env: CHEMCLAW_<NAME>_TOKEN}`, the variable name, never the token.
   A non-loopback URL with `auth: mode: none` is refused at load. Omit `health_url` if the server
   exposes none; `/readyz` then reports it `unprobed` rather than guessing a path.
2. In `values.yaml`, set `connectors.<name>.url` to its address. That bundle gets **no** Deployment
   and **no** Service, and the front door dials what you gave instead of an in-cluster name.
   `server: true` still mirrors the manifest's `endpoint:` and says nothing about who runs it.
3. Add the host to `networkPolicy.egressDestinations` (the rule already permits
   `egressPorts.https`, but the destination list is empty by default), and put the token in the
   secret set the pods already mount.

The tools such a server exposes are still read/compute only, still narrowed by `tools:`, and still
carry the turn's identity headers as *advisory* context — a connector outside our trust boundary
must never make an access decision on a header's word (`connectors/identity.py`).

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
startup instead. Verify a bundle standalone with `uvicorn chemclaw.connectors.<name>.server.app:app` and check
`/healthz`; tool *discovery* needs no database, but *invoking* a search does.

**What ships today.** The bundles are `molfp` and `rxnfp` (fingerprint search), `safety` (the
hazard screen), `chem` (bench chemistry over RDKit), `calc` (the fast calculators and the
calibration ledger), `bo` (Bayesian optimization) and `qm` (the durable QM/DFT run behind the
Nextflow launcher). `calc`, `bo` and `qm` each declare `jobs:` and therefore own durable work, so
each runs a second Deployment for its own Temporal worker; set `worker: true` on a bundle in the
chart to get one. `tests/test_repo_map.py` derives both sets from the `connector.yaml` files on
disk, so this paragraph is checked rather than remembered.

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
- **The development report** — its closure (retrievers, embedding index) is what core keeps for
  `gather_evidence` anyway, so a bundle would isolate nothing (D-115). It still returns the
  connector envelope, so `get_durable_job_status` collects it like any other job. The QM/DFT run
  used to be listed here beside it, on the reasoning that it needs the HPC identity bridge; that
  turned out to be a property of the *bundle's worker*, not of core, and it is `connectors/qm/`
  now (D-118).

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
`data/profiles/property-lookup.yaml` for a worked example.

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
the same order. `src/chemclaw/templates/README.md` has the full comparison and the field reference.

**To add one:** drop `data/templates/<name>.yaml`. The filename is the name here too.

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

**A steady ingest count is not proof anything landed.** `IngestSummary.ingested` counts entries
whose note this run *proposed* — the PR-gate is where a human merges it — so an entry whose branch
is blocked (the `kg-validate` hazard gate refuses it) or simply unreviewed is proposed again on
every run, indefinitely. Those entries are listed in `IngestSummary.awaiting_merge` (a subset of
`ingested`) and logged as `eln sync proposed N entry/entries whose notes are still unmerged`. A
count that does not fall between runs is a review queue nobody is working, not a source producing
new data: open the branches those entries named, and merge or reject them. Note that an entry's
*first* sync can appear here too when its export landed late — it sits inside the replay window,
so it will indeed be re-proposed next run.

## (vi) Change a fingerprint definition (ECFP radius/bits or DRFP bits)

`CHEMCLAW_ECFP_RADIUS`/`_ECFP_BITS`/`_DRFP_BITS` define the fingerprints. A **width** change
(`*_BITS`) also needs a matching `bit(N)` schema change (`infra/sql/002,003`) or inserts fail
loudly. Every fingerprint row records the *definition* it was indexed under, and similarity
search returns only rows matching the store's current definition — so after any definition
change, previously-indexed rows fall out of search (safe: no wrong scores, just missing hits)
until you **re-index** them (re-run the ELN sync / re-add molecules). If search comes back
empty after a config change, that is the tell: the index predates the new definition and needs
rebuilding.

## (vi-b) After an upgrade that changes what a note's indexed text is

`note_index` rows are keyed on a **stat** fingerprint (mtime + size), which detects a changed
*note* and by construction cannot detect a changed *definition of the text* — the file is
untouched, so an incremental `make reindex` finds nothing to do and the stored embeddings go on
describing the old text forever.

One upgrade has done this so far: D-2026-08-05 made a note's searchable text include its `type`
and its `compound_smiles`, which the dense and lexical indexes had never seen. **Run `make
reindex-full` once after upgrading past it.** The symptom of skipping it is not an error — dense
and lexical search simply keep answering as if the change had not happened, while the substring
leg answers as if it had.

**`CHEMCLAW_EMBEDDING_MODEL` and `CHEMCLAW_LLM_BASE_URL` no longer need this**
(D-2026-08-08-a-derived-index-must-record-what-derived-it). `note_index` records which embedding
configuration made each row (migration 039, the column `document_chunks` already had), so the
ordinary incremental `make reindex` — and the hourly workflow — re-embed exactly the rows a swap
superseded. Nothing to remember and no flag to pass. What that does mean is that the *first* run
after upgrading past this re-embeds the whole corpus once: every existing row has no key recorded,
which reads as unknown, and unknown is never treated as current. Same for `document_chunks`, whose
keys all change because the key now names the endpoint as well as the model.

**A share's `chunk_chars` / `chunk_overlap_chars` now take effect, and they are not free.** The
same migration set records which chunking cut each row (040), and both of the crawl's gates compare
it — so changing either number re-reads and re-cuts every document of that share, off the mount,
over the crawl's ordinary bounded passes. Before this the change was silently ignored, which was
cheaper and wrong. The first sync after the upgrade pays it once for the same reason as above:
nothing recorded what the existing rows were cut with.

**Migration 041 rebuilds `document_chunks`' primary key, and that is the one migration in this set
with a real duration.** It backfills the added column, sets it `NOT NULL` and replaces
`(doc_id, ordinal)` with `(doc_id, chunking_key, ordinal)` — building a unique index under an
`ACCESS EXCLUSIVE` lock. The migrator's `lock_timeout` bounds waiting *for* the lock, not the build,
so on a share-sized table budget seconds to a minute of the document search being unavailable, once.
Rows written before 040 get `''`, which no binding can produce: they still read as superseded at
both gates, and they stay searchable until the crawl replaces them rather than disappearing at
upgrade.

**Expect one drain, not two — and expect the corpus to be of mixed generation while it runs.** The
re-embed pass is scoped to the chunkings the enabled shares currently use, so a chunk that the crawl
is about to re-cut is not refreshed first and then thrown away. What no scoping can remove is the
window: the re-embed drains `CHEMCLAW_DOCUMENT_REEMBED_BATCH_SIZE` chunks per activity,
`CHEMCLAW_DOCUMENT_SYNC_MAX_ITERATIONS` times per run, so at the shipped 500 × 100 a million-chunk
share takes on the order of **days** of six-hourly runs. Throughout it, document search compares
queries embedded by the new model against vectors not yet refreshed — scores are degraded, results
are not missing. Watch `re-embedded N chunk(s)` in the background worker's log to see the drain
converge, and the `%d chunk(s) could not be re-embedded` line at ERROR for the ones it cannot fix.
To finish faster, raise the batch size or run `python -m chemclaw.cli.sync_share <name>`, which
drains that share's re-embed to completion before it crawls.

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
when the deployed code, cases, and `data/evals/baseline.json` are inconsistent. After a deliberate
metric change, refresh the committed baseline — otherwise every scheduled run re-alerts.

## (viii) Answer "is prompt caching paying off?"

The system prompt is large and largely identical turn to turn, so "cache the static prefix" is a
standing cost-saving proposal (REV-9). **Measure before building it** — the metric that answers the
question already exists, and the answer decides whether there is anything to build.

Scrape `/metrics` and read the four spend counters together:

```
chemclaw_input_tokens_total       # fresh prompt tokens, full price
chemclaw_cache_read_tokens_total  # prompt tokens served from the provider's cache, ~10x cheaper
chemclaw_cache_write_tokens_total # tokens written to the cache, priced above a fresh input token
chemclaw_output_tokens_total      # completion tokens, unaffected by any of this
```

The ratio `cache_read / (cache_read + input)` is the cache hit rate on the prompt side. Each outcome
implies a different action:

| Reading | What it means | What to do |
| --- | --- | --- |
| `cache_read` is a large fraction of prompt spend | The provider is already caching the prefix without being asked | Nothing. The saving is banked; a `cache_control` mechanism would add code for a benefit you already have. |
| `cache_read` ≈ 0 and `input` is large | The prefix is being re-billed every turn | There is a real saving to chase — see the caveats below before estimating it. |
| `cache_write` grows while `cache_read` stays flat | The cache is being paid for and never used | Sessions are too short or too spread out to hit it; shortening the prefix beats caching it. |

`cache_write` is **structurally 0 on the `openai_compatible` provider** — it reports cache reads but
has no cache-write concept — so a zero there on the production provider is not a fault and not a
signal. On the Anthropic dev path it is real.

Two caveats that make the saving smaller than a naive prefix measurement suggests, both of which
cost this review a wrong estimate:

- **Measure the provider you actually run.** The ~14.6 k-token prefix figure that started REV-9 was
  measured on the Anthropic dev path. Production is `openai_compatible`, where
  `agent_framework_openai` contains **zero** occurrences of `cache_control` — the mechanism is not
  reachable from there at all, so the fix is upstream work, not a config change here.
- **The system half is not cacheable through `Agent` as it stands.** `SkillsProvider` merges the
  skills manifest into the instructions with an f-string, which would `repr()` a structured block
  list into a string. Marking that half cacheable needs a change in `agent_framework`, not in
  Chemclaw.

Per-model attribution for the same spend is on the OTel side, not here: MAF emits
`gen_ai.client.token.usage` labelled by request model, response model, provider and token type, and
the shipped chart turns OTel on. These counters carry `profile`, which OTel has never heard of.

## (ix) Work the PR-gate review queue

Agent-authored notes never land directly; each is pushed to `note/<id>` and waits for a human
(D-005). Until D-2026-07-31-a-proposal-is-a-record-not-a-branch the only way to find one was to
browse `note/*` refs in the git host, and a rejection left no trace at all. Both are now routes.

- **What is waiting**: `GET /proposals?state=open`. Paged newest-first by row id; pass the last id
  you saw as `before_id` for the next page (`CHEMCLAW_PROPOSAL_LIST_LIMIT` bounds a page). A
  reviewer — anyone holding a role in `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` — sees every proposal;
  everyone else sees their own. With `CHEMCLAW_ENTRA_REQUIRED=false` (dev) everything is visible.
- **What it says**: `GET /proposals/{id}` returns the rendered note exactly as it would land in the
  tree, plus the `session_id` and `correlation_id` of the turn that produced it — so
  `make explain SESSION=<session-id>` reaches the conversation behind a proposal (D-166).
- **Decide**: `POST /proposals/{id}/decision` with `{"approved": true}` or
  `{"approved": false, "reason": "…"}`. A rejection **must** state why; that is the record's whole
  purpose. Deciding twice is a `409`, not a silent overwrite — the first decision stands.
- **Merges close themselves** when something calls `POST /events/knowledge-merged` with
  `{"note_ids": ["…"]}`, signed as
  `X-Chemclaw-Signature: sha256=<HMAC-SHA256 of the raw body under CHEMCLAW_NOTE_WEBHOOK_SECRET>`.
  Without the secret configured the route still forces a reindex for an operator running it by
  hand, and refuses to close anything.
  **That "something" is not a git host directly — a translation step is required, and this page
  used to imply otherwise.** GitHub signs under `X-Hub-Signature-256` and posts a whole
  pull-request payload; GitLab sends `X-Gitlab-Token`, which is the raw secret rather than an HMAC;
  Azure DevOps uses Basic auth. None of them emits a `note_ids` list, because only this system
  knows which note ids a merged branch carried. So wire the host's post-merge hook to a small proxy
  or a CI job that reads the merged branch, collects the note ids, and re-signs the body for this
  endpoint. The contract here is deliberately ours; the mapping is the operator's, and it is one
  step rather than a missing feature.

**If the queue only ever grows**, the webhook is the first thing to check: `curl` it with a signed
body naming a note you know was merged and read `proposals_closed` in the response. A `401` means
the signature does not match — the secret differs between the host and
`CHEMCLAW_NOTE_WEBHOOK_SECRET`, or the host signed a re-serialized body rather than the bytes it
sent.

**The metric to watch is `chemclaw_note_proposals_total{state}`.** A rising `open` against a flat
`merged` is a queue nobody is working. A non-zero `failed` means submissions are not reaching git
at all — the note is still recoverable, because a `failed` row keeps the rendered content:
`SELECT note_id, content FROM note_proposals WHERE state = 'failed';`.

## (x) Find out what a worker is doing (or why it stopped)

Until D-2026-08-01-every-process-carries-its-own-witness the answer was "read the logs and guess".
The workers had no HTTP surface, so nothing scraped them and no probe could contradict a pod that
Kubernetes was reporting as `Running` with a dead Temporal poll loop. Every worker now serves three
routes on `CHEMCLAW_WORKER_METRICS_PORT` (default 9000, the `metrics` container port):

```
kubectl port-forward deploy/chemclaw-background-worker 9000:9000
curl -s localhost:9000/readyz    # 200 = polling, 503 = the worker is not running
curl -s localhost:9000/metrics   # this pod's counters, gauges and histograms
```

- **`/readyz` is 503 but the pod is up.** The Temporal worker is not polling — a lost broker
  connection, or a shutdown that has begun. It is deliberately *not* a liveness signal: restarting
  on it would turn an ordinary reconnect into a crash loop, so the pod stays and reports honestly.
  Check the broker before the worker.
- **`/healthz` stops answering.** The event loop is wedged, almost always by a blocking call inside
  an activity, and the kubelet restarts the pod after `failureThreshold` (2 minutes by default —
  generous, because a false restart mid-job costs more than a slow true one). The metric to read
  after the restart is `chemclaw_tool_duration_seconds` on the pod that replaced it.
- **The counters read zero on a busy worker.** They are per-process, so you are scraping the wrong
  pod: a durable job launched from the front door increments the front door's registry, and the
  same job's *activity* increments the worker's. Scrape both before concluding a number is missing.

Two monitors collect all of this in-cluster: `servicemonitor.yaml` for anything with a Service (the
front door, each connector's MCP server) and `podmonitor.yaml` for the workers, which have none.
Both need `monitoring.additionalLabels` set to whatever your Prometheus's `serviceMonitorSelector`
and `podMonitorSelector` match — the default is empty, so a fresh install collects nothing until an
operator says where. If a target is `down` rather than absent, check
`networkPolicy.monitoringNamespaces`: that is the list granting the scraper ingress to the connector
port and the worker probe port.

## (xi) A migration that will not apply, and a release stuck in `pending-upgrade`

Migrations run as a Helm `pre-install,pre-upgrade` hook Job that completes before any app container
starts (D-034), so a failure here blocks the release rather than half-applying it. Three things were
missing until D-2026-08-01-a-migration-waits-in-front-of-live-traffic, and each has its own symptom.

**Why `helm rollback` below is safe: every migration in `infra/sql/` only expands.** Checked over
the whole directory, not one `infra/sql/*.sql` file contains a `DROP TABLE` or `DROP COLUMN` — every
one is a new table or an `ADD COLUMN`, and `chemclaw.core.migrate` refuses to let an applied
file change afterward (a checksum mismatch raises `MigrationError`; see (ii)). So the schema only
ever grows, which is exactly what a rollback needs: the older binary a rollback restores was written
against a schema that is still a strict subset of whatever is live, so every table and column it
expects is still there. That is the *expand* half of expand/contract, and this repo has practiced it
consistently — measured, not assumed. The **contract** half — dropping a column only once no
deployed code still reads or writes it — has never actually been exercised here: nothing has ever
been dropped, and no test or gate enforces the ordering the way `migrate`'s checksum check enforces
immutability. A migration that drops something would have to earn that safety by the same judgement
any production migration needs, not by a mechanism this repo has built for it.

**The Job failed after ~5 s with `canceling statement due to lock timeout`.** Working as intended:
an `ALTER TABLE` needs `ACCESS EXCLUSIVE` and could not get it, because something else holds a lock
on that table. Find it and decide whether to wait or to end it:

```sql
SELECT pid, state, wait_event_type, now() - query_start AS age, left(query, 120)
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle'
ORDER BY query_start;
```

A long-running report or an abandoned `idle in transaction` session is the usual answer. **Do not
raise `CHEMCLAW_PG_MIGRATION_LOCK_TIMEOUT_SECONDS` to get past it** — the timeout is what keeps the
migration from queueing in front of live traffic. Postgres's lock queue is FIFO, so a pending
`ACCESS EXCLUSIVE` request blocks *every later query on that table behind it*: raising the bound
converts a failed deploy into an outage lasting as long as the slowest open query.

**The Job failed after ~5 minutes waiting on `pg_advisory_xact_lock`.** Another migrator is running,
or one died holding the lock. Check:

```sql
SELECT pid, granted, now() - state_change AS age FROM pg_locks
JOIN pg_stat_activity USING (pid) WHERE locktype = 'advisory';
```

A live peer: wait and re-run, which applies nothing because it finds every file already recorded. A
dead one: the lock is transaction-scoped, so it is released the moment that backend disconnects —
there is nothing to clean up by hand.

**The release is stuck in `pending-upgrade`.** Helm waits for the hook, so before
`activeDeadlineSeconds` existed a retrying Job could hold a release there indefinitely and block
every later `helm upgrade`. The Job now fails at `migrateJob.activeDeadlineSeconds` (15 min) and the
recovery is:

```
kubectl logs job/chemclaw-migrate            # the hook is kept on failure, so the logs are there
helm rollback chemclaw                       # or `helm upgrade --install` again once the cause is fixed
```

**The Job says a migration was edited after being applied.** `MigrationError`, and the fix is never
to edit the file back: `schema_migrations` records a checksum precisely so an in-place change is
loud. Add a new numbered file that makes the change forward.

**`applied migrations: (none — already up to date)` on a fresh database.** The migration directory
resolved to nothing. `CHEMCLAW_SQL_MIGRATIONS_DIR` is workdir-relative (`/app/infra/sql` in the
image); an empty glob applies zero files and reports success (D-148).

## (xii) A caller is being refused (429 / 413), or should be and is not

Three bounds sit in front of the app, at three levels, because none of them can be enforced from
inside it (D-2026-08-01-a-cheap-request-is-still-a-request).

**429, `Retry-After: N`.** The per-principal request budget. It is a token bucket:
`CHEMCLAW_SERVICE_RATE_LIMIT_PER_MINUTE` is the sustained refill and `..._BURST` is what one caller
may spend at once. Watch `chemclaw_requests_rate_limited_total` — a steady non-zero rate is usually
a script someone wrote against the API rather than an attack, and the fix is to raise the burst for
that deployment, not to switch the limiter off.

Two properties worth knowing before you tune it:

- **It is per process.** With `service.autoscaling.maxReplicas: 6` the fleet ceiling is six times
  what you configured, and a caller pinned to one pod by the Route's affinity cookie (D-121) sees
  the per-process number. A genuine fleet-wide limit belongs at the ingress.
- **The probes are exempt by construction.** `/healthz`, `/readyz` and `/metrics` do not depend on
  `require_principal`, which is the only place the budget is spent. If a probe ever starts getting
  429s, the gate has been moved somewhere it should not be.

**413.** The request body exceeded `CHEMCLAW_SERVICE_MAX_REQUEST_BYTES`, refused before anything
read it. If a chemist reports that an attachment *at* the documented size is rejected, check that
this value still sits above `CHEMCLAW_ATTACHMENT_MAX_BYTES` — the body limit covers the whole
multipart envelope, boundaries and part headers included, so setting the two equal makes the
documented attachment size unreachable. `chemclaw_requests_too_large_total` counts these.

**503 with no request in the log at all.** Not the app: uvicorn refused at
`--limit-concurrency` (`CHEMCLAW_SERVICE_MAX_CONNECTIONS`). It bounds *connections*, not turns, and
sits far above `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` on purpose — a connection waiting for an
admission permit or holding an SSE stream costs almost nothing, so this is the backstop and the turn
cap is the policy. Raise it if long SSE streams plus browser keep-alives genuinely exceed it;
lowering it to shed load turns the transport into the admission control and sheds the wrong things.

**A slow client that never finishes its headers.** Bounded by
`CHEMCLAW_SERVICE_MAX_HEADER_BYTES`, and an idle connection is reclaimed after
`CHEMCLAW_SERVICE_KEEPALIVE_SECONDS`. Neither has a metric: they are transport-level and the
connection never becomes a request the app can count. `chemclaw_turns_in_flight` against
`chemclaw_turn_capacity` is the signal to read if the front door feels full and nothing is being
refused.

## (xiii) Restore a store — and what a restore does to the audit trail

**Read this before running a restore, not after.** A point-in-time restore of Postgres is a
*trailing deletion* of `audit_events`, and the hash chain cannot see one: the surviving rows link
cleanly, so a shortened GxP trail verifies as intact
(D-2026-08-01-a-restore-is-a-truncation-nobody-can-see).

### What this system needs from the stores it does not own

The chart deploys none of these. It states what it requires of whoever does.

| Store | Holds | If it is lost |
| --- | --- | --- |
| **Postgres** | the audit chain, sessions, the calculation cache, the note index, job records | the audit chain is the only part with a compliance obligation; the cache is regenerable by definition (D-011) and the note index is rebuilt by `make reindex` |
| **Temporal** | in-flight workflow history | running jobs die; finished results survive in `job_records` (D-157) and the calculation store |
| **Knowledge git repo** | every merged note | the corpus. It is a git repo, so any clone is a backup — including each pod's sidecar checkout |
| **HPC artifact store** | job outputs | regenerable at the cost of re-running the cluster job |

Only one of the four needs a *point-in-time* story rather than a recent-snapshot one, and it is the
audit trail — because it is the only store where "we lost the last hour" is a compliance statement
rather than an inconvenience.

### Restoring Postgres

1. **Before restoring, capture the current anchor.** From the log store, take the most recent line
   containing `audit_chain_anchor=` and keep the whole line. This is the *only* copy that survives
   the restore — the `audit_anchors` table is rolled back with everything else, and will come back
   agreeing with the truncated trail.
2. Restore, by whatever mechanism the Postgres owner provides.
3. Run the migrator (`make db-migrate` / the Helm hook) — it applies nothing if the restore was
   already current, and the advisory lock makes running it twice safe (§(xi)).
4. **Verify against the anchor you kept:**

   ```
   uv run python -m chemclaw.cli.verify_audit_chain --anchor '<the whole log line>'
   ```

   A clean chain plus `audit trail is short: N rows against an anchor of M` is the expected and
   correct outcome of a restore that lost records. It is telling you precisely what a GxP process
   needs to know and what nothing could tell you before: how many audited actions are gone.
5. **Record the gap, then re-seal.** The trail may be shortened by a legitimate recovery; it may
   never pretend it was not.

   ```
   uv run python -m chemclaw.cli.verify_audit_chain \
     --anchor '<the log line>' --reseal "PITR to 2026-08-01T09:00Z after storage failure INC-123" \
     --reseal-by "qa@example.com"
   ```

   `--reseal` refuses on a broken chain, deliberately: re-sealing over a break would sign the damage
   and the trail would verify clean forever afterwards.

### When the verifier reports a gap and there was no restore

Treat it as tampering until shown otherwise, and do **not** re-seal. The anchor is signed, so a gap
means either records were removed or the anchor was forged — and forging one needs
`CHEMCLAW_AUDIT_ANCHOR_SECRET`, which is not in the database. Preserve the trail, preserve the log
store, and escalate.

### If `CHEMCLAW_AUDIT_ANCHOR_SECRET` is unset

None of the above applies, because there are no anchors. The chain still catches modification,
reordering, interior deletion and prefix truncation — and a restore stays what it was before this
existed: an undetectable shortening of the compliance trail. Set the secret before writing a
recovery procedure, not after.

## (xiv) Cut a release: pin the image to bytes

`values.yaml` ships `tag: "0.1.0"` so `helm install .` works in dev without a registry round trip.
**A release must not deploy that tag.** A tag is a pointer: `helm rollback` to a release naming
`0.1.0` fetches whatever `0.1.0` means now, which is the one thing a rollback must not do — and this
system stamps a build revision onto every audit record (AG-14), so "which bytes produced this
result" stops being answerable the moment a tag is re-pushed
(D-2026-08-01-a-tag-is-a-pointer-not-a-build).

```
# 1. Build with the base pinned by digest (the file's default floats on purpose — see the ADR).
docker build -f deploy/Containerfile \
  --build-arg BASE_IMAGE=registry.access.redhat.com/ubi9/python-311@sha256:<base-digest> \
  --build-arg CHEMCLAW_REVISION="$(git rev-parse HEAD)" \
  -t "${REGISTRY}/chemclaw:${VERSION}" .

# 2. Push, then read back the digest the registry assigned.
docker push "${REGISTRY}/chemclaw:${VERSION}"
docker image inspect "${REGISTRY}/chemclaw:${VERSION}" --format '{{ index .RepoDigests 0 }}'

# 3. Deploy by digest. The tag is ignored entirely when this is set.
helm upgrade --install chemclaw deploy/helm/chemclaw \
  --set image.digest="sha256:<the digest from step 2>"
```

**Building without crest.** Redistributing a GPL-3.0 binary inside a product image is a licensing
decision, not an engineering one. `--build-arg INCLUDE_CREST=false` builds without it;
`calc.crest_cli` reports unavailable rather than failing, so the image loses conformer sampling and
nothing else. xtb (LGPL-3.0) is not affected by the same question — both are invoked as separate
processes over files and never linked, so this is about distribution, not licence compatibility.

**A private registry.** `image.pullSecrets` is a list of `{name: <secret>}` applied to every pod
spec. Before it existed, an operator whose registry needed authentication had no field to set and
the pods simply failed to pull, which reads as a broken image rather than a missing credential.

### When a supply-chain gate goes red

Three blocking gates run in `.github/workflows/image.yml`, and each fails differently:

| Gate | What it read | First move |
| --- | --- | --- |
| `pip-audit` | the exported lockfile — the exact versions the image installs | `uv lock --upgrade-package <name>`; reproduce locally with `make deps-audit` |
| `trivy` | the built image: base OS packages plus the xtb/crest layers | usually a stale base — rebuild picks up the current UBI9 |
| SBOM step | nothing; it records | it only fails if `syft` cannot run |

`trivy` runs with `ignore-unfixed: true` on HIGH and CRITICAL. That is a deliberate narrowing, not
an oversight: a gate that fires on every LOW in a distro base is one an operator disables within a
week. A finding that genuinely cannot be fixed gets an explicit `--ignore-vuln` **with its reason in
the diff** — never a downgrade of the whole gate, which is how a control becomes a badge.

The SBOM (SPDX) and the built image's digest are retained on the run for 90 days. That is what makes
"what was in the image that produced this audit record" answerable at all, and it is the reason the
floating base default is defensible: the bytes cannot be pinned in advance, so they are named after.

## (xv) Onboard, entitle and offboard a person

**Identity is entirely Entra.** This system has no user table, no local accounts and no invite
flow: it reads the caller's token and nothing else. So "add a user" is a directory operation, and
everything below is either an Entra change or a config change — **there is no code change in this
section at all.**

Two ways a tenant can express membership, and the difference matters when you write a role name:

| Tenant wiring | What lands in the turn's role set | How you write it in config |
| --- | --- | --- |
| App role assignment | the app role value, verbatim | `chemclaw.sharedrive.reader` |
| Group claim (`CHEMCLAW_ENTRA_GROUP_CLAIMS_AS_ROLES=true`) | each group claim, namespaced | `group:<claim value>` |

The prefix is not decoration. The same flat set gates every write tool, every skill and every
document share, so an unprefixed group value would be indistinguishable from an app role of that
name. A bare object id matches nothing.

### The roles this system reads

There is no fixed list to create — the names are yours, and every one of them is referenced from
config rather than from code. What is fixed is the **set of gates** that read them:

| Setting | What it gates | Shape |
| --- | --- | --- |
| `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` | expensive jobs (`expensive: true` in any `connector.yaml`), and who sees the whole PR-gate review queue rather than only their own proposals | comma list of role values |
| `CHEMCLAW_TOOL_ROLE_GATES` | one named tool → the roles that may call it | JSON `{"tool": ["role"]}` |
| `CHEMCLAW_SKILL_ROLE_GATES` | one skill's *visibility* — a caller holding none of its roles never sees it | JSON `{"skill": ["role"]}` |
| `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` | anything expensive that **no** manifest declares; a bundle's own `expensive: true` needs no entry here | comma list of tool names |
| a share's `required_roles:` | one mounted document share, in its `datasource.yaml` — a caller without it gets nothing from that source, not a filtered list | list of role values |

Two defaults worth knowing before you design the role set:

- **Write tools are closed by default.** `DEFAULT_WRITE_TOOL_GATES` (`agent/authz.py`) gates every
  job launcher and state-mutating tool to `entra_privileged_roles` unless you have written an
  explicit `tool_role_gates` entry for it. A deployment that sets **no** privileged role therefore
  refuses every expensive job — which is the intended failure, not a misconfiguration to route
  around. Setting `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` is the whole remedy.
- **None of it is enforced unless `CHEMCLAW_ENTRA_REQUIRED=true`.** In dev the gates are open so
  the app runs without a tenant. Never run a shared or exposed deployment that way — the front door
  refuses to start on a non-loopback bind precisely to stop it.

### Onboard someone

1. Assign them the app role (or add them to the group) in Entra. Nothing to restart.
2. Nothing else. Their first request carries the role; `require_actor` accepts it.

### Grant an existing role a new capability

Edit `CHEMCLAW_TOOL_ROLE_GATES` / `CHEMCLAW_SKILL_ROLE_GATES` in the chart's `config:` block and
roll the deployment. Adding a *share* to a role is the share's `required_roles:` instead, because
that entitlement belongs to the corpus rather than to the tool surface.

### Revoke access

Remove the app role or group membership in Entra. **There is no in-app kill switch, and this is a
decision rather than an omission**: a token already issued stays valid until it expires, so
revocation takes effect within your tenant's access-token lifetime (an hour by default). If you
need it faster than that, the lever is the tenant's — continuous access evaluation or a shortened
lifetime — not a deny-list here, which would be a second source of truth about who may act and
would drift from the directory the moment anyone edited it by hand.

For an immediate, deployment-wide stop, scale the front door to zero. There is deliberately no
per-user equivalent.

### Offboard: erase their data

Removing the role stops new access and deletes nothing. Per-actor rows live in nine tables, split
into two tiers by one rule — **the conversation is erasable, the record is not**:

```bash
make user-erase ACTOR=<entra-oid>            # dry run: real counts, writes nothing
make user-erase ACTOR=<entra-oid> APPLY=1    # commits
```

It removes their sessions, messages, events, turn lease, preferences and watch subscriptions. It
**keeps and counts** the rows that attribute scientific work to them — `audit_events`,
`plan_approvals`, `note_proposals`, `bo_suggestions`, `job_records`, `turn_costs` — and prints the
reason beside each. That is not a limitation to work around: an attributable record that can be
deleted on request is not an attributable record, and `audit_events` carries a tamper-evident hash
chain whose proof spans the rows either side of any deletion (see (xvi)). If a data-protection
obligation reaches the retained tier, that is a decision to take with the record's owner.

The dry run executes the deletes and rolls back, so the number you sign off on is the number that
will be deleted rather than a second query's guess at it.

## (xvi) Verify the audit trail, and the other commands with no section of their own

Four operations exist as `make` targets and had no entry here. Three of them are yours to run;
the fourth is automated and listed so nobody runs it by hand wondering why.

**`make audit-verify` — the GxP tamper-evidence check.** `audit_events` is a hash chain: each row
carries a hash over itself and its predecessor, so any edit or deletion breaks the chain from that
point on. Verifying it is what makes the trail *evidence* rather than a log. Nothing schedules it,
so decide a cadence and hold to it — monthly, plus after any restore (see (xiii), which describes
what a restore does to the chain without naming the command that checks it) and before any audit.
A reported gap with no restore behind it is an incident, not a maintenance task.

**`make share-sync SHARE=<source>` — crawl a mounted document share now.** The scheduled job is the
production path (every six hours by default); this is for the first crawl after attaching a share,
and for re-crawling after a bulk change nobody wants to wait six hours for. Run
`make share-estimate SHARE=<source>` first — it walks the share, reads nothing, and tells you what
the crawl would cost.

**`make safety-validate` — force-compile the hazard and genotoxicity tables.** Run it after editing
a rule table. CI runs it too; the point is that a bad SMARTS fails at deploy rather than on the
first live hazard question.

**`make schedules-apply` — do not run this by hand.** The chart runs it as a post-rollout Job, so
the Schedules follow the deployment automatically. It is here only so that finding it in
`make help` does not read as a missing step.
