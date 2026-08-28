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
  `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector (or set `CHEMCLAW_OTEL_ENDPOINT`, which
  `core/logging.py` bridges to it). Requires the OpenTelemetry SDK + OTLP exporter
  extras installed; enabling without them raises a directive error.
  - **What the process installs**, since `configure_telemetry` stopped being one line into the
    agent framework: a `TracerProvider` whose spans go through a `BatchSpanProcessor` to the OTLP
    **span** exporter, tagged `service.name=chemclaw` and `service.version=<deployment revision>`.
    **The chart now sets `OTEL_SERVICE_NAME` per Deployment** (`chemclaw-service`,
    `chemclaw-background-worker`, `chemclaw-connector-<name>`, `chemclaw-connector-worker-<name>`),
    so the four process roles are four services in the trace backend rather than one — they all
    reported `service.name=chemclaw` until then, which made "which component emitted this span"
    unanswerable. `OTEL_RESOURCE_ATTRIBUTES` carries `k8s.pod.name` and `k8s.namespace.name` from
    the downward API on top. Traces only, on purpose: metrics are `/metrics` (Prometheus, scraped
    per pod) and logs are JSON on **stderr**, and neither needs a second copy over OTLP.
  - **Logs go to stderr, not stdout.** `configure_logging` calls `logging.basicConfig` with no
    `stream=`, and that default is `sys.stderr`. Three documents said stdout, which matters the
    moment anything separates the two streams — a `2>/dev/null`, a sidecar tailing one of them, a
    log driver configured per-stream. Under Kubernetes both land in the container log and
    `kubectl logs` shows them together, which is why the claim survived so long.
  - **`CHEMCLAW_OTEL_LLM_SPANS=true` gives you a span per model call** — token counts, model name
    and provider, plus the chain and tool spans around them — through OpenInference's LangChain
    instrumentation, over the same OTLP exporter. On in the shipped chart. Point the collector at
    Arize Phoenix to read these conventions natively; any OTLP backend receives the same spans, and
    nothing in the image depends on Phoenix.
  - **`make phoenix-up` gives the eval lane one to point at**, on 6006 (UI) and 4317 (OTLP), from
    `infra/docker-compose.observability.yml`. `infra/live/processes.sh` probes 4317 and turns the
    exporter on only when something is listening, so a lane started without it is unchanged.
    Content stays suppressed unless you set the flag below deliberately. It is **not** in the Helm
    chart and is not meant to be: production traces go to whatever collector your org runs, and
    this is the reader for archived probe runs.
  - **`make phoenix-publish DIR=<transcripts> NAME=<experiment>` publishes a run you already
    have.** It calls no model — `evals/live.py` already wrote `{probe, outcome}` per probe and the
    judge's verdicts sit in `grades.json` beside them, so the comparison surface is built from the
    record rather than from a re-run. The dataset is the *corpus* (`data/evals/probes/`) and each
    run is an experiment over it, so a run that covered fewer probes shows as coverage rather than
    as a corpus that shrank. Publish two runs into the same dataset and Phoenix diffs them.
  - **`CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` decides whether those spans carry content**, and it is
    off. Off sets every OpenInference hide flag, so a span carries identifiers and counts and
    nothing a chemist typed — measured by sweeping every exported attribute for the question and the
    answer, not by naming the keys that might hold them. Turning it on is a decision about content
    leaving the pod: the collector's store then holds the same class of data `SECURITY.md` describes
    for the audit trail. It governs nothing while `CHEMCLAW_OTEL_LLM_SPANS` is off, and the process
    says so at WARNING if you set it anyway.
  - **This is what closed the per-model attribution regression.** The agent framework's chat-client
    instrumentation recorded `gen_ai.client.token.usage` — an OTel *metric* (a histogram), despite a
    name that reads like a span attribute — labelled by request model, response model, provider and
    token type, and it went out with the framework: nothing in `langchain`, `langgraph` or
    `langsmith` emits it. The replacement is not that metric. It is a *span* per model call carrying
    `llm.token_count.prompt`/`.completion`/`.total` and `llm.model_name`, so the question is
    answered in the trace pipeline rather than the metric one. `/metrics`'s token counters are
    unchanged and still carry `profile` rather than model — deliberately, D-152 — and §(viii) is
    the place that reads them.

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
- **`eval "$(bash infra/live/processes.sh env)"` before running anything from a second terminal.**
  Every bundle this repository hosts authenticates its own `/mcp`, and `make live-up` *mints* those
  credentials rather than defaulting them. A command run in a fresh shell would mint its own and get
  401s from servers that are plainly up. `up` writes them into the lane's run directory and `env`
  reads them back; the file itself is an implementation detail, and the subcommand is the contract.
  It carries `CHEMCLAW_LIVE_PROBE_TOKEN` too, when the lane is enforcing identity, and `down`
  removes it — stale credentials for processes that are gone are a slower version of the same
  mismatch, not a milder one.
- **Running the lane with identity enforced** (the posture the chart ships) needs an issuer, and
  `Chemclaw3_mock` is one — see `D-2026-08-20-a-tenant-is-a-jwks-document-and-an-issuer-string`:

  ```bash
  # in the Chemclaw3_mock checkout
  MOCK_ENTRA_ENABLED=true uvicorn app.main:app --port 8090

  # here
  CHEMCLAW_ENTRA_REQUIRED=true \
  CHEMCLAW_LIVE_ENTRA_TOKEN_URL=http://127.0.0.1:8090/entra/mock-tenant/oauth2/v2.0/token \
    make live-up
  ```

  The audience, issuer and JWKS URL derive from that one endpoint, the probe identity is minted
  from the same tenant the front door validates against, and `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`
  defaults to `process-chemist` — named rather than left empty because both authorization gates
  fail *closed* on an empty privileged set, so an unset role would make the run measure a
  permissions error instead of the system. Mint any other identity by POSTing to that same URL:
  `{"oid":"u-bench"}` for a chemist with no entitlements, `{"expires_in":-60}` for an expired token,
  `{"unpublished_key":true}` for a forgery.
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

### Splitting the database principal (optional)

By default one credential does everything, and `infra/sql/006` describes `audit_events` as
"append-only by contract" — a contract nothing enforced and nothing prevented breaking. To make it
append-only in fact (D-2026-08-05-append-only-by-grant-not-by-contract):

1. Create a login role the application runs as, owning nothing:
   `CREATE ROLE chemclaw_app LOGIN PASSWORD '…';`
2. Point `CHEMCLAW_POSTGRES_DSN` at it, and put the schema owner's DSN in
   `CHEMCLAW_POSTGRES_MIGRATION_DSN` — in the chart, `secrets.migrationKeys`, which is mounted on
   the migration hook Job and on nothing else.
3. `make db-migrate && make db-grants`.

Verify it took: as `chemclaw_app`, `INSERT INTO audit_events …` succeeds and
`DELETE FROM audit_events` fails with `InsufficientPrivilege`. The owner credential can still
rewrite the trail — this narrows who holds that power and for how long, it does not remove it — so
the grant is the whole of the guarantee. The role also needs no `CREATE EXTENSION` right;
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

**A source that `provides` reactions turns the labeller on, with no second switch.**
`durable/schedules.py` creates a `reaction-labels` Schedule for any active source that supplies
reactions — deliberately, because `CHEMCLAW_DATA_SOURCES` plus the source's `labels:` block already
answers "is anything being labelled here". So attaching a reaction corpus is also the moment the
background worker starts dialling `CHEMCLAW_RXNLABEL_SERVER_URL` (`Chemclaw3-mcp`'s `rxnlabel`,
port 8865). The chart ships an in-cluster Service name for it, `secrets.optionalKeys.rxnlabelToken`
for its bearer and `networkPolicy.egressPorts.rxnlabel` for the wire — check all three before you
enable the corpus, because none of them fails loudly: an unreachable labeller leaves the drain
retrying and every faceted precedent question answering from an empty label index.

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
   worked example (one workflow, one queue and its own worker, however many jobs the manifest
   declares — `tests/test_repo_map.py` pins that shape, and no count is written here because a
   count in prose goes stale silently).
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
   exposes none; the probe then records it `unprobed` rather than guessing a path.
2. In `values.yaml`, set `connectors.<name>.url` to its address. That bundle gets **no** Deployment
   and **no** Service, and the front door dials what you gave instead of an in-cluster name.
   `server: true` still mirrors the manifest's `endpoint:` and says nothing about who runs it.
3. Add the host to `networkPolicy.egressDestinations` **and its port to `networkPolicy.egressPorts`**,
   then put the token in the secret set the pods already mount. Both halves are needed and the
   second is the one that gets missed: a NetworkPolicy egress rule restricts by port independently
   of the destination list, so a server on its own port is dropped no matter what you add to
   `egressDestinations`. Only assume `egressPorts.https` covers it if the server really is on 443 —
   the three bundles `Chemclaw3-mcp` serves are plain HTTP on 8858/8859/8860, and each needed its
   own entry.

The tools such a server exposes are still read/compute only, still narrowed by `tools:`, and still
carry the turn's identity headers as *advisory* context — a connector outside our trust boundary
must never make an access decision on a header's word (`connectors/identity.py`).

**Configuration.** `CHEMCLAW_CONNECTORS_DIR` (pathsep, like `PATH` — prepend a private bundle dir to
override a shipped one), `CHEMCLAW_CONNECTORS_ENABLED`, `CHEMCLAW_CONNECTOR_URLS`,
`CHEMCLAW_CONNECTORS_REQUIRED`, `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS`,
`CHEMCLAW_CONNECTOR_JOB_TIMEOUT_SECONDS`. A connector's request timeout and auth mode are per-manifest
(`endpoint.request_timeout`, `endpoint.auth`); the `bearer` mode names an env var, so no credential is
ever written into a bundle.

**Troubleshooting.** Each enabled connector is probed as `healthy`, `unreachable` or `unprobed`
(no `health_url` declared — honest for a third-party server). `GET /readyz` reports the *count* of
unreachable ones and never their names — it is unauthenticated by necessity, so its body is a public
document and a roster of the internal capability surface does not belong in one. The names are on
`/metrics` (`chemclaw_connectors_unhealthy`) and in the WARNING each failed probe logs, which also
carries the reason. An unreachable connector
costs its tools for that turn, not the turn itself; set `CHEMCLAW_CONNECTORS_REQUIRED=true` to fail
startup instead. Verify a bundle standalone with `uvicorn chemclaw.connectors.<name>.server.app:app` and check
`/healthz`; tool *discovery* needs no database, but *invoking* a search does.

**What ships today.** The bundles are `molfp` and `rxnfp` (fingerprint search), `calc` (the
semiempirical calculators, their durable searches and the calibration ledger), `bo` (Bayesian
optimization) and `results` (re-queueing stored calculations for an external
results store, §(xvi)) — plus `chem` (bench chemistry over RDKit) and `safety` (the
hazard screen), which this release **declares but does not run**: both are served by
`Chemclaw3-mcp`, so each needs its host in `networkPolicy.egressDestinations` and its bearer
(`CHEMCLAW_CHEM_TOKEN`, `CHEMCLAW_SAFETY_TOKEN`) provided, or every call to them is refused. The
physics behind `calc` is served there too — `CHEMCLAW_CALC_SERVER_URL` and `CHEMCLAW_CALC_TOKEN` —
even though the `calc` bundle's own tools, cache and durable jobs stay in this release.

**`chem` is declared here and served elsewhere.** Its capability is `Chemclaw3-mcp`'s
`servers/chem` on port 8858, so this release renders no Deployment and no Service for it and dials
the address in `connectors.chem.url` instead (D-2026-08-09). Two things that are the operator's,
because the chart cannot do them: add the host to `networkPolicy.egressDestinations`, and provide
`CHEMCLAW_CHEM_TOKEN` — that server enforces a bearer on `/mcp` itself, so a missing credential is
a refused call rather than an open one. `calc`, `bo` and `results` each declare `jobs:` and therefore own
durable work, so each runs a second Deployment for its own Temporal worker; set `worker: true` on a
bundle in the chart to get one. `results` is the one jobs-only bundle — it declares no
`endpoint:`, so `server: false` and it renders no app pod; its one job re-queues stored
calculations for an external results store, and is inert until `CHEMCLAW_RESULT_SINKS` names one
(§(xvi)). `tests/test_repo_map.py` derives both sets from the `connector.yaml` files on
disk, so this paragraph is checked rather than remembered.

**What stays in core is a rule, not an omission** (D-115), and `tests/test_tool_registry.py` pins the
set so adding to it is a reviewed edit:

- **Conversation plumbing** — anything reading or writing the turn's own state
  (`ask_clarifying_question`, attachments, preferences, watches). Another process does not have the
  turn.
- **The two PR-gate writers** (`propose_knowledge_note`, `record_confirmed_answer`) — the review
  boundary. A connector reaches the gate only by returning a note in a job envelope, for core to
  publish.
- **The knowledge-graph reads** (`find_notes`, `expand_note`, `find_knowledge_gaps`, and the
  `gather_evidence` sweep over them). The graph is core's *data layer*, not a capability: thirteen
  core modules import `kg`, so a bundle would move three thin tools and leave every one of those
  imports behind — a zero dependency win and a second read path to one note tree. Re-indexing stays
  in core with it.
- **The development report** — its closure (retrievers, embedding index) is what core keeps for
  `gather_evidence` anyway, so a bundle would isolate nothing (D-115). It still returns the
  connector envelope, so `get_durable_job_status` collects it like any other job. The DFT run used
  to be listed here beside it, on the reasoning that it needs the HPC identity bridge; that turned
  out to be a property of the *bundle's worker* rather than of core (D-118), and the whole tier has
  since been removed (`D-2026-08-26-semiempirical-is-the-whole-tier`).

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
is simply unreviewed is proposed again on
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

**You do not need the workflow (or a broker) to get this reading.** `make eval-baseline-check` runs
the same comparison offline, prints every metric's baseline/current/delta/band, and exits non-zero
only on a move in the *worsening* direction — so it is the one to run before refreshing the
baseline, and the one that answers "did anything get worse?" on a laptop. It declares the case-set
version it scored (`EVAL_CASE_SET_VERSION`) and refuses to report a number when that differs from
the baseline's: aggregates over two different case-sets are different quantities.

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
  measured on the Anthropic dev path. Production is `openai_compatible`, where `langchain_openai`
  contains **zero** occurrences of `cache_control` — the mechanism is not reachable from there at
  all, so the fix is upstream work, not a config change here. This survived the rebuild of layer 1
  unchanged, and it was re-measured rather than assumed to: the previous framework's OpenAI client
  had the same zero, and `langchain_anthropic` has 74 occurrences, which is why the dev path can
  do what the production path cannot.
- **The system half is not cacheable as the prompt is assembled.** `deepagents.SkillsMiddleware`
  renders the skills manifest into a string with `system_prompt_template.format(...)` and appends
  it to the system message, so the half that changes least is welded to the half that changes most.
  Marking it cacheable needs a change upstream, not in Chemclaw — the same conclusion the previous
  framework's `SkillsProvider` f-string forced, reached again for the same structural reason.
- **Measure a session that is inside its context budget.** Above
  `CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET`, `agent/compaction.py` rewrites the *front* of the message
  list on every model call — clearing older tool results, then dropping the oldest conversation
  groups — so the cacheable prefix changes by construction. A `cache_read ≈ 0` measured on such a
  session is compaction doing its job, not the provider failing to cache, and chasing it would be
  chasing a saving that is not there. `chemclaw_context_compactions_total` (below) tells you which
  kind of session you measured.

### Is the context policy firing, and is the budget set anywhere near the traffic?

```
chemclaw_context_compactions_total       # model calls whose message list was reduced
chemclaw_context_reclaimed_tokens_total  # estimated prompt tokens those reductions saved
```

A model call that needed no reduction increments **neither**, which is what makes the two readings
distinguishable — and that distinction is the whole reason these exist. The policy they report on
was absent for a phase while three settings, a config comment and a sentence in the system prompt
all described it, and nothing could have told you.

| Reading | What it means | What to do |
| --- | --- | --- |
| flat zero | No session in this process has crossed the budget | Nothing. This is the healthy default, and it is also the state in which the cache table above is meaningful. |
| the line is absent from `/metrics` | You are not scraping this process | Not a compaction signal at all: `core/metrics.py` pre-seeds every declared counter, so both names render at `0` from the first scrape of a process that has served nothing. An absent line means the worker's `/metrics` port is unscraped (`CHEMCLAW_WORKER_METRICS_PORT`), not that the policy is unwired. |
| rising steadily, `reclaimed` large per compaction | Long sessions are routinely over budget | Expected on a deployment with real chemists. Read it against `chemclaw_turn_duration_seconds`: reduction is cheap (sub-millisecond to ~6 ms per call), so a slow turn is not this. |
| rising on almost every call | The budget is below this deployment's normal turn | Raise `CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET` toward the model's real context window. Compacting a thread that would have fit spends estimator passes and drops context for nothing. |

Per-model attribution for the same spend **is not on this surface, and is no longer missing**. The
old framework emitted `gen_ai.client.token.usage` labelled by request model, response model,
provider and token type, and it went out with the framework — nothing in `langchain`, `langgraph`
or `langsmith` emits it. What replaced it is not a metric: `CHEMCLAW_OTEL_LLM_SPANS=true` puts one
span per model call in the trace pipeline carrying `llm.token_count.*` and `llm.model_name`, so
"which model, how many tokens" is a trace query and "what is this deployment spending per hour" is
these counters. They still carry `profile` rather than model, deliberately (D-152): the `turn_costs`
ledger already holds per-turn model attribution, and a second, lossier answer as a counter label
would be two systems to reconcile.

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

**`monitoring.additionalLabels` is not what decides whether they are read, and this section used to
say it was** ("a fresh install collects nothing until an operator says where"). That is false on the
stated target: OpenShift's user-workload monitoring selects **every** ServiceMonitor and PodMonitor
in every user namespace, with no label selector at all, so the shipped empty default is correct
there and adding labels changes nothing. It is true of a **self-managed Prometheus Operator**, whose
`Prometheus` resource carries a `serviceMonitorSelector`/`podMonitorSelector` that these labels have
to match — which is the deployment the value exists for.

What *does* decide it on OpenShift is a cluster-wide switch that is off by default: see
§ "Make the monitoring stack actually collect this" below. If a target is `down` rather than absent,
check `networkPolicy.monitoringNamespaces`: that is the list granting the scraper ingress to the
connector port and the worker probe port.

## (x-b) Make the monitoring stack actually collect this

**Do this before believing anything above.** The chart ships a ServiceMonitor, a PodMonitor and a
PrometheusRule with thirty-five alerts; on a stock OpenShift cluster **all three are inert custom
resources**. `oc get servicemonitor` lists them, nothing scrapes, no rule ever loads, and there is
no error anywhere — a deployment in this state is indistinguishable, from inside, from a healthy
one. It is the single most likely way this system ships and observes nothing.

Three switches, none of them the release's to flip, in the order they matter.

**1. User-workload monitoring — without it nothing is scraped.**

```bash
oc -n openshift-monitoring edit configmap cluster-monitoring-config
# under data.config.yaml:
#   enableUserWorkload: true
```

The ConfigMap may not exist; create it with that one key. Then check the stack came up and that
this release's targets are actually being collected — a monitor that exists is not a monitor that
matched:

```bash
oc -n openshift-user-workload-monitoring get pods
oc -n <release-namespace> get servicemonitor,podmonitor,prometheusrule
# and, from the console: Observe -> Targets, filtered to the release namespace
```

Every target should be `Up`. One `Down` is a NetworkPolicy question, not a monitoring one — see
`networkPolicy.monitoringNamespaces` in §(x).

`monitoring.additionalLabels` is **not** part of this on OpenShift: user-workload monitoring selects
every monitor in every user namespace regardless of labels. It exists for a self-managed Prometheus
Operator, whose `serviceMonitorSelector` these labels have to match.

**2. Alert routing — without it the alerts fire into nothing.**

User-workload alerts are forwarded to the platform Alertmanager, whose routing tree a cluster admin
owns and which normally drops what it does not recognise. So every rule can be `firing` in the
console while no human is ever told. Either that admin routes on `namespace="<release-namespace>"`,
or this namespace supplies its own routing, which needs **one** of these two — both off by default:

```bash
# the platform Alertmanager reads AlertmanagerConfigs from user namespaces
oc -n openshift-monitoring edit configmap cluster-monitoring-config
#   alertmanagerMain:
#     enableUserAlertmanagerConfig: true

# or: a dedicated Alertmanager for user workloads
oc -n openshift-user-workload-monitoring edit configmap user-workload-monitoring-config
#   alertmanager:
#     enabled: true
#     enableAlertmanagerConfig: true
```

Then give the release its receivers. The chart renders an `AlertmanagerConfig` when
`monitoring.alertmanager.enabled=true`, and **refuses to render** if you enable it without any —
an object that routes to nothing is the state this is fixing:

```yaml
monitoring:
  alertmanager:
    enabled: true
    defaultReceiver: chemistry-oncall
    criticalReceiver: chemistry-pager   # optional; severity=critical goes here instead
    receivers:
      - name: chemistry-oncall
        slackConfigs:
          - apiURL: {name: chemclaw-alertmanager, key: slackWebhookUrl}
            channel: "#chemclaw-alerts"
      - name: chemistry-pager
        pagerdutyConfigs:
          - routingKey: {name: chemclaw-alertmanager, key: pagerdutyRoutingKey}
```

Secrets are referenced by `SecretKeySelector` against a Secret in the release namespace, never
inlined — an `AlertmanagerConfig` is an ordinary readable object. Prove the route end to end before
trusting it, because everything above is silent when wrong:

```bash
oc -n <release-namespace> get alertmanagerconfig
# then watch a deliberately noisy alert arrive, or use amtool against the Alertmanager directly
```

**3. Dashboards — where they land is not where the console reads.**

The chart writes five dashboards into a ConfigMap labelled `console.openshift.io/dashboard: "true"`.
The OpenShift console reads that label **only in `openshift-config-managed`**, and the shipped
default writes into the release namespace, because a chart that fails to *install* on a dashboard is
worse than one that needs a second flag:

```bash
helm upgrade ... --set monitoring.dashboards.namespace=openshift-config-managed   # needs cluster-admin
oc -n openshift-config-managed get configmap -l console.openshift.io/dashboard=true
# then: Observe -> Dashboards
```

For a self-managed Grafana instead, add its sidecar's label and leave the namespace empty:
`--set monitoring.dashboards.labels.grafana_dashboard=1`.

**What the five cover.** `Chemclaw turns` (rate, outcomes, p50/p95/p99, tokens, in-flight against
capacity), `Chemclaw tools and model` (p95 **by tool**, refusals by reason, the provider seam),
`Chemclaw durable jobs` (success ratio, p95 by connector, Temporal task slots and pollers),
`Chemclaw front door` (per-route rate, error ratio and p95) and `Chemclaw data and storage` (ingest
lag, evidence per source, cache hit ratio, the result outbox, the Postgres pool). Between them every
metric this system declares has either a panel or an alert, and
`tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` is what keeps that true.

**The Temporal SDK's own metrics are off.** `monitoring.temporalSdkMetrics.enabled=true` renders a
second worker container port and a second `podMetricsEndpoint` for the SDK's Prometheus exporter —
task-slot occupancy, poller counts, schedule-to-start latency, the queue-side numbers no first-party
counter can produce, and the only thing `ChemclawWorkerNotPolling` can read. Leave it off until the
worker process actually binds that port: with nothing listening it is a permanently-down scrape
target, which `ChemclawTargetDown` would then report forever.

## (x-c) When an alert fires

Every rule carries a `runbook_url` pointing at its heading below, so an alert arrives with its own
entry attached. The rules' own `description` annotations say what happened and are not repeated
here; what follows is what to *do*, and what the alert does not mean.

Two things to know before reading any of them:

- **Every alert is per-fleet, and almost every metric is per-process.** A counter reads zero on a
  pod that is not the one doing the work: a durable job launched from the front door increments the
  front door's registry and its *activity* increments the worker's. Scrape both before concluding a
  number is missing.
- **Only `ChemclawTargetDown` and `ChemclawNoWorkerIsScraped` fire for a process that is gone.**
  Everything else reads an application counter, and a process that is not running emits no counters
  — which looks exactly like a healthy quiet system.

### chemclaw.records — a durable record is being lost

#### ChemclawAuditTrailIncomplete
`critical`. Tool calls keep succeeding while the trail of who ran them does not. Find the
`audit_sink_failure` marker in the front door's log; it is almost always the database. The rows
already lost are not recoverable — `durable/retention.py` refuses to prune this table for the same
reason this is critical.

#### ChemclawVerifierDegraded
`warning`. Answers are being scored by the citation gate instead of the judge, so every affected
turn goes to human review: this is a review-queue load signal as much as a model one. Check the
`verifier` model route's reachability. §(xvi-b) covers turning the judge on and off deliberately.

#### ChemclawUsageUnreadable
`critical` because the failure mode is an unbounded bill that looks like an idle deployment. The
provider changed its usage keys; affected turns meter zero tokens and the budget guard admits them
regardless. Read `chemclaw_tokens_total` against the provider's own console to size the gap.

#### ChemclawDurableUnreachable
`warning`. Chemists are being told durable jobs are unavailable. Check the broker before the
workers: `ChemclawTargetDown` covers the pods, this covers the thing they dial. The threshold
(`monitoring.alerts.durableUnreachableWarning`) is what suppresses a single blip.

#### ChemclawKnowledgeNotesLost
`critical`. Knowledge is being dropped silently — the publish is best-effort so a dead remote cannot
fail a finished calculation. Usual causes are a dead git remote, an expired push credential, or two
processes sharing one `note_repo_dir`. §(ix) is the PR-gate queue; the notes lost here never reached
it.

### chemclaw.correctness — an invariant is at risk

#### ChemclawTurnLeaseFailing
`critical`. A turn's cross-process lease could not be refreshed, so it may expire mid-turn and admit
a second turn onto the same session. Usually the session store under load — read
`ChemclawPgPoolSaturated` beside it.

#### ChemclawTurnClaimsLost
`critical`, and the other end of the same invariant: this one has already happened. Two turns can
now interleave into one session history. The session's thread is what is at risk, not the turn.

### chemclaw.availability — chemists are being refused or degraded

#### ChemclawTurnsFailing
`critical`. More than one turn in ten ends in an opaque internal error. Break it down with
`sum by (outcome) (rate(chemclaw_turns_finished_total[10m]))` — `errored` and `timed_out` are
different problems — then the front-door dashboard's per-route error ratio.

#### ChemclawFleetAboveItsTurnCeiling
`warning`. More front-door pods are running than the declared fleet ceiling accounts for, so the
shared LLM endpoint can be offered more concurrent turns than its budget permits. A manual scale, an
HPA edited in the cluster, or a rollout that left both generations up. Scale back, or raise
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` to a number the endpoint can actually serve.

#### ChemclawTurnsShed
`warning`. The admission guard is declining load. Either the deployment is undersized or
`service_max_concurrent_turns` is below what the endpoint serves. Check
`ChemclawTurnLatencyHigh` first: slow turns hold permits, so latency usually precedes shedding
rather than following it.

#### ChemclawCalcBackendOverCommitted
`warning`. More calculation-backend sessions are held across the fleet than
`CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS` says that pod will serve. It pins
`OMP_NUM_THREADS=1` and is CPU-bound, so the surplus arrives as thrashing, then as activity
heartbeat timeouts, then as retries onto the same pod — which is why this fires before anything
fails. `sum(chemclaw_calc_requests_in_flight)` by pod says who is dispatching: a scaled `calc`
worker (`replicas × CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES`) or interactive tool traffic on that
bundle's own server pods, which have no per-process cap at all. Either scale back, or raise the
ceiling to what the server actually admits — `Settings` checks only the durable product, once, at
startup, so it cannot see the interactive half. Silent until a release declares a ceiling: the
gauge is 0 by default.

#### ChemclawConnectorsUnhealthy
`warning`. Turns are being answered without those capabilities and **nothing in the answer says so**.
`chemclaw_connector_unhealthy{connector}` names which; the data dashboard has it. Then
`ChemclawTargetDown` for whether the pod is gone or merely unreachable.

#### ChemclawSubsystemUnavailable
`warning`. Requests are being shed with 503 because a dependency did not answer — the durable broker
or the document index. The `shedding` log line on the same pod names the method, the path and the
subsystem.

#### ChemclawGroupClaimOverage
`warning`. A user's Entra token replaced `groups` with `_claim_names`, so their group-derived
entitlements could not be read and any gated corpus answers emptily *for them only*. There is no
request-time fix; assign the group to an app role so it arrives in `roles` instead. §(xv) covers
entitlement.

#### ChemclawDatabaseUnavailable
`critical`. Sessions, the audit trail and the calculation cache all live there. §(xiii) is the
restore path; check the pool alerts below before assuming the server is down, since a saturated pool
presents as connect timeouts against an idle database.

#### ChemclawDatabaseQueriesFailing
`warning`, and not an outage: the server is answering and rejecting. A schema disagreement, a
constraint violation, or a migration that did not fully apply (§(xi)). The `kind` label names the
operation; the driver's own message is in the pod's log.

#### ChemclawPgPoolSaturated
`warning`. Callers are queueing for a connection and will fail as `ConnectionError` after
`CHEMCLAW_PG_POOL_TIMEOUT_SECONDS`. Raise `CHEMCLAW_PG_POOL_MAX_SIZE` **and**
`postgres.maxConnections` together (`Settings` refuses a pair that stops agreeing), or lower the
concurrency feeding it. Deliberately `max()` across pods: one saturated process is one saturated
process, and averaging hides it.

#### ChemclawFleetAboveItsConnectionCeiling
`warning`. The same blind spot as the turn ceiling, for connections: the fleet can ask for more than
the server will serve, which surfaces as connect failures against a database that is not busy.

### chemclaw.cost

#### ChemclawTokenBurnHigh
`warning`. The fleet-wide burn is above `monitoring.alerts.tokensPerHourWarning`. The per-process
budget guard bounds one runaway process and says nothing about the fleet. §(viii) is how to tell
whether caching is paying off before you raise the threshold.

#### ChemclawTurnsRefusedByBudget
`warning`, and the same subject seen from the chemist's side: they get a 429 with no explanation.
Read `chemclaw_tokens_total` beside it — either the allowance is genuinely spent, or the window is
set below real traffic.

### chemclaw.fleet — a process is gone

#### ChemclawTargetDown
`critical`. A pod stopped answering `/metrics`. This is the only alert that fires for a process that
is *gone* rather than misbehaving: crash loop, eviction, OOM kill, or a NetworkPolicy that stopped
admitting the scraper. The `pod` label names it; `oc describe pod` and the previous container's logs
are the next two commands.

Scoped by namespace rather than by `job`, because the `job` label the Prometheus Operator assigns
differs between a ServiceMonitor and a PodMonitor. If the release namespace holds other workloads,
narrow `monitoring.alerts.targetJobPattern`.

#### ChemclawNoWorkerIsScraped
`critical`, and a different failure from the one above: there is no target to be down. Pods that
cannot be scheduled, a PodMonitor whose selector no longer matches, or user-workload monitoring
turned off cluster-wide — in which case every alert here is inert and this is the only one that says
so. Start at §(x-b) step 1.

#### ChemclawWorkerNotPolling
`critical`, and rendered only when `monitoring.temporalSdkMetrics.enabled` is on. A worker is up and
answering its probes while asking Temporal for no work, so jobs queue and nothing runs them. This is
the gap the worker's probes leave *on purpose*: `/readyz` is deliberately not a liveness signal,
because restarting on a lost broker connection would turn an ordinary reconnect into a crash loop.
Check `/readyz` on the named pod (§(x)) and the broker before restarting anything.

### chemclaw.turns — the answer itself

#### ChemclawTurnLatencyHigh
`warning`. p95 is over `monitoring.alerts.turnLatencyP95Seconds`. Break it down in this order, all
on the tools and data dashboards, taking `histogram_quantile(0.95, …)` over each histogram's
`_bucket` series: `chemclaw_tool_duration_seconds` **by `tool`** — that label is what makes "which
tool is slow" answerable at all, and it did not exist until this pass — then
`chemclaw_model_call_duration_seconds` by provider, then `chemclaw_evidence_source_seconds` by
source. Slow turns hold admission permits, so this tends to precede `ChemclawTurnsShed`.

#### ChemclawTurnsTimingOut
`warning`. Someone waited out `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` and got nothing. If
`ChemclawTurnLatencyHigh` is already firing these are its tail and the cause is upstream of the
timeout.

#### ChemclawTurnsAnsweringEmpty
`warning`, and the quietest bad outcome in the system: the turn succeeded and produced nothing to
read. No error counter moves. Usually a model that emitted only tool calls, or a middleware that
short-circuited after the last one; `make explain <session>` reconstructs the turn.

### chemclaw.durable — the expensive half

#### ChemclawDurableJobsFailing
`critical`. More than `monitoring.alerts.jobFailureRatio` of jobs are ending `failed`. Break down by
connector with `sum by (connector, outcome) (rate(chemclaw_jobs_finished_total[30m]))`, then by
activity with `chemclaw_activity_failures_total` — which counts one row per *attempt*, so a retry
storm shows as a rate rather than only in the broker's history. The Temporal UI's event history is
the next stop (§(x)).

#### ChemclawActivityRetryStorm
`warning`. Every attempt of one activity has failed for half an hour, so its job is retrying without
progress or has already given up. The Temporal event history for a workflow using it is the fastest
route to the exception (§(x)); `chemclaw_jobs_finished_total{outcome="failed"}` says whether jobs are
dying with it.

#### ChemclawPushBackDropped
`warning`. A finished job's result never reached the session that asked for it. The job succeeded
and the result is stored; what was lost is the chemist being told. Check
`chemclaw_event_streams_open` against `chemclaw_event_stream_capacity` on the front door first.

#### ChemclawFanOutChildrenDropped
`warning`. A fan-out parent completed **reporting success** with children missing, so its result is
incomplete. The scenario this exists for is three memory-synthesis jobs going green every night
while returning `[]`.

#### ChemclawResultPublishFailing
`critical`. A result could not be delivered to a configured sink. The calculation stands in the
cache; the scientific record this deployment publishes to does not have it.
`chemclaw_sink_delivery_seconds` and the sink's logs name which. §(xvi) is the attach procedure.

#### ChemclawResultProjectionFailing
`critical`, and **retrying will not help**: `publish/` could not turn a calculation into the typed
record its sink expects, which is a schema disagreement between this build and
`schema/result-store/`. The result is dropped before any delivery is attempted.

#### ChemclawResultsDeadLettered
`warning`. Publications exhausted their retries and were retired to `failed`. Nothing will attempt
them again; re-queueing is an operator action. They also never leave the queued-minus-published
difference, which is why that difference is not a backlog and why the alert below reads an age.

#### ChemclawResultOutboxStuck
`warning`. The oldest undelivered publication for this sink is older than
`monitoring.alerts.outboxStuckSeconds`. Read `chemclaw_outbox_pending{sink}` for the depth and
`chemclaw_outbox_dead_lettered{sink}` for what has already been given up on.

### chemclaw.degradation — something is quietly not working

#### ChemclawSubsystemDegraded
`warning`, and the umbrella over roughly forty deliberate exception swallows. The `subsystem` label
names which one; the pod's log carries the exception. Turns keep being answered, which is exactly
why this needs an alert rather than a panel.

#### ChemclawEvidenceSourceFailing
`warning`. A retrieval leg is raising, so answers are composed from the remaining legs and cite
nothing from this one — and nothing in the answer says so. Read
`chemclaw_evidence_source_chunks_total` and `chemclaw_evidence_source_skips_total` on the same
`source` label: a leg that fails, a leg that declines and a leg that legitimately matches nothing are
three different states, and telling them apart is what
`D-2026-08-01-a-cap-that-starves-a-source` was written about.

#### ChemclawIngestCursorStalled
`warning`. A source is further behind than `monitoring.alerts.ingestLagSeconds`, so the corpus
chemists query is stale by at least that much. A wedged fetch advances no cursor and logs
`ingested=0`, which is byte-identical to a genuinely quiet source — the lag gauge is the only thing
that separates them. §(v) covers re-ingesting rejected entries.

#### ChemclawGaugeReadFailing
`warning`. `render()` drops one gauge whose source raised rather than losing the whole scrape, so
every other series is intact and this one is simply *absent* — which on a graph is
indistinguishable from a value that has not changed. The `metric` label names it.

#### ChemclawMetricSeriesDropped
`warning`. A metric hit its per-metric label-set cap and is now undercounting by an unknown amount,
along with every alert and panel reading it. A label here is meant to be low-cardinality, so this
means something is generating values it should not; the pod's log names the metric.

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

**Read this before running a restore, not after.** A point-in-time restore of Postgres silently
shortens `audit_events`: the trail comes back missing whatever was written after the restore point,
and nothing in the system will tell you so. The system used to carry a hash chain and signed
high-water anchors that made *some* alterations detectable — never this one, which is what
D-2026-08-01-a-restore-is-a-truncation-nobody-can-see was about — and they have since been removed.
So the record of what was lost is the restore itself: write down the restore point and the window it
discarded, wherever your operational record lives, at the time you do it.

### What this system needs from the stores it does not own

The chart deploys none of these. It states what it requires of whoever does.

| Store | Holds | If it is lost |
| --- | --- | --- |
| **Postgres** | the audit trail, sessions, the calculation cache, the note index, job records | the audit trail is the only part that cannot be regenerated from anything; the cache is regenerable by definition (D-011) and the note index is rebuilt by `make reindex` |
| **Temporal** | in-flight workflow history | running jobs die; finished results survive in `job_records` (D-157) and the calculation store |
| **Knowledge git repo** | every merged note | the corpus. It is a git repo, so any clone is a backup — including each pod's sidecar checkout |

Only one of the three needs a *point-in-time* story rather than a recent-snapshot one, and it is the
audit trail — because it is the only store where "we lost the last hour" means the answer to "who
ran that?" is gone for good, rather than merely inconvenient.

### Restoring Postgres

1. Restore to the chosen point by whatever mechanism your Postgres provider offers.
2. Re-run `make db-migrate`. Every migration is `IF NOT EXISTS` and re-runnable, so this is a no-op
   on a restore that was already current and closes the gap on one that was not.
3. Record the restore point and the window of audit rows it discarded in your operational log.
   Nothing in the database can tell you afterwards that they were ever there.
4. Re-run `make db-grants` if the deployment splits the database principal (see *Splitting the
   database principal*, above) — a restore can bring back a role grant state that predates it.

The other two stores need no procedure here: Temporal's in-flight history is lost by definition, and
the knowledge repo is a git repo (any clone is a backup).

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
  --set image.digest="sha256:<the digest from step 2>" \
  --set networkPolicy.allowAnyDestination=true \  # or list networkPolicy.egressDestinations
  --set retention.unboundedGrowthAccepted=true   # or state retention.windows
```

**The pipeline does exactly the three steps above.** `Jenkinsfile` builds with
`CHEMCLAW_REVISION`, publishes, reads the digest back from the registry and passes it as
`image.digest` — and `deploy/jenkins/targets/openshift.sh` refuses any value that is not a
`sha256:` digest, so the tag path cannot be taken by accident. Run it by hand when Jenkins is not
available; the commands are the same commands. `deploy/jenkins/README.md` is the reference, and
`D-2026-08-26-a-release-is-a-descriptor-and-a-target` is why a release is a file rather than a set
of build numbers.

**No calculation binaries.** This image once installed xtb (LGPL-3.0) and crest (GPL-3.0), with a
build flag for declining to redistribute the second. Neither ships now: the physics moved to
`Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`), so nothing in `src/` invokes
either binary and the redistribution question belongs to the repository whose code runs them.

**A private registry.** `image.pullSecrets` is a list of `{name: <secret>}` applied to every pod
spec. Before it existed, an operator whose registry needed authentication had no field to set and
the pods simply failed to pull, which reads as a broken image rather than a missing credential.

### When a supply-chain gate goes red

Two blocking gates run in `.github/workflows/image.yml`, and each fails differently:

| Gate | What it read | First move |
| --- | --- | --- |
| `pip-audit` | the exported lockfile — the exact versions the image installs | `uv lock --upgrade-package <name>`; reproduce locally with `make deps-audit` |
| SBOM step | nothing; it records | it only fails if `syft` cannot run |

**There is no image scan, and this section used to say there was.** It listed `trivy` as the second
of three blocking gates and described how it was tuned, in the present tense; `trivy` appears
nowhere in the workflow, the Makefile or anything else that runs. That is worse than a missing
control — an operator reading this page would have believed the base OS layers were being scanned
and that a red build would tell them. The scan is a real gap, tracked in `BACKLOG.md`, and it is
held for a stated reason rather than forgotten: per
`D-2026-08-01-a-tag-is-a-pointer-not-a-build`, the candidate scan kept reporting packages
(`setuptools` 70.3.0, `msgpack` 1.1.2) that an exhaustive `find / -xdev` in the same build could not
locate, and a gate whose last word contradicts the artifact it scanned makes every future red build
ambiguous.

When the scan is merged, it should run with `ignore-unfixed: true` on HIGH and CRITICAL. That is a
deliberate narrowing, not an oversight: a gate that fires on every LOW in a distro base is one an
operator disables within a week. A finding that genuinely cannot be fixed gets an explicit
`--ignore-vuln` **with its reason in
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
deleted on request is not an attributable record, and for a tool call that changed nothing durable
the trail is the only place it is recorded at all. The application credential cannot delete from
`audit_events` either — the grant withholds DELETE (see *Splitting the database principal*). If a
data-protection obligation reaches the retained tier, that is a decision to take with the record's
owner.

The dry run executes the deletes and rolls back, so the number you sign off on is the number that
will be deleted rather than a second query's guess at it.

## (xvi) Attach an external results database

Every calculation this system performs is projected into a typed scientific record and delivered to
a database **it does not own** (`D-2026-08-25-a-cache-is-not-a-record`). Publishing is **off until
you attach one**: `CHEMCLAW_RESULT_SINKS` is empty by default, and with no sink named the enqueue
costs one list lookup and no database work at all.

**1. Create the schema.** This system never holds DDL privileges on the store it publishes to, so
apply the DDL with a principal that does — the same split `postgres_migration_dsn` and
`postgres_dsn` already make for this system's own database.

```
make sink-schema > results-schema.sql        # DDL + the registry seed, in the order to apply them
psql "$RESULTS_ADMIN_DSN" -v ON_ERROR_STOP=1 -f results-schema.sql
```

The seed is *generated* from `chemclaw.publish.properties` and `chemclaw.publish.solvents` rather
than checked in, because those are what the writer canonicalizes against: a seed file that had
drifted from them would build a database whose foreign keys reject rows this system considers valid.
Re-run it after any upgrade — the inserts are idempotent, and a new calculator ships registry rows
rather than migrations.

**2. Point a sink at it.** Mount a folder holding your own `sink.yaml` and put it first on
`CHEMCLAW_RESULT_SINKS_DIR`, so your address is not a change to this repository:

```
CHEMCLAW_RESULT_SINKS_DIR=/etc/chemclaw/sinks
CHEMCLAW_RESULT_SINKS=postgres
RESULTS_DB_USER=... RESULTS_DB_PASSWORD=...        # the target's own credentials,
                                                  # unprefixed: not settings of this system
```

The manifest names *environment variables*, never values; they are read at connect time, so a
rotated secret is picked up by the next connection rather than the next deploy. `make sink-validate`
checks that the driver resolves and takes its config, and it runs in CI.

**3. Backfill.** Publishing hooks a calculation as it completes, so a store attached to a running
deployment would otherwise receive only what is computed from that moment on — while
`calculation_results` and `job_records`, neither ever pruned, hold everything before it.

```
python -m chemclaw.cli.backfill_publications --dry-run    # what would be queued
python -m chemclaw.cli.backfill_publications              # queue it
```

Safe to run twice and safe to run live: the outbox's identity index makes a second pass a no-op.
A chemist can do the same thing as a durable job (`republish_calculations`, the `results` bundle).

**Watching it.** `chemclaw_results_queued_total` minus `chemclaw_results_published_total` **is** the
backlog — there is deliberately no pending gauge, because that subtraction is exact and free while a
gauge would need a `COUNT(*)` per scrape. A rising difference means the destination is down or too
slow; `chemclaw_result_publish_failures_total` and the `last_error` column say which.

**When a destination has been down.** Rows that spent their attempt budget move to `state='failed'`
and are **kept** — they are the record that something was never published. Once the cause is fixed:

```
python -m chemclaw.cli.backfill_publications --requeue
```

Only *delivered* rows are ever pruned (`CHEMCLAW_RETENTION_RESULT_PUBLICATIONS_DAYS`), and that
predicate is the policy: sweeping a pending or failed row on a clock would turn an outage into a
silent gap.

## (xvi-b) Switch on the answer verifier (the LLM-as-judge)

Off by default, in code and in the chart, and turning it on is a deployment decision with two
facts attached — both learned the hard way and both now enforced rather than documented-only:

1. **Startup probes the judge, and refuses to serve if it cannot comply.** With
   `CHEMCLAW_VERIFIER_ENABLED=true` on an `openai_compatible` provider, the front door's lifespan
   runs one structured-output probe against the routed `"verifier"` model
   (`agent/verifier.require_verifier_capability`). An endpoint that rejects or ignores
   `response_format` (json_schema) fails the boot with a message naming the setting — because
   without that support the judge silently degrades to the offline citation gate on **every**
   turn while looking enabled, for the lifetime of the deployment.
2. **Verdicts at the margin are re-rolled.** The judge's score is reproducible on unambiguous
   answers and unstable exactly where `CHEMCLAW_VERIFIER_CONFIDENCE_THRESHOLD` (0.7) lives, so a
   confidence landing within `CHEMCLAW_VERIFIER_REVIEW_BAND` of the threshold triggers up to
   `CHEMCLAW_VERIFIER_BAND_REROLLS` extra rolls and the median decides
   (`D-2026-08-27-a-verdict-at-the-margin-is-a-coin-toss` — the width is measured, not chosen).
   Watch `chemclaw_verifier_band_rerolls_total` against answers verified: that ratio is the
   band's real cost, and it should be a small fraction. `chemclaw_verifier_degraded_total`
   climbing means the judge endpoint is failing and answers are getting the weaker deterministic
   verdict — a judge outage, not a slow path.

To enable: set the `CHEMCLAW_VERIFIER_*` keys the chart's `values.yaml` carries commented-out,
route the judge with `CHEMCLAW_MODEL_ROUTES='{"verifier": "<cheap-model>"}'`, and roll. To re-fit
the band on your own corpus: `make live-verifier-margin` re-rolls the raw judge and prints the
recommended width (see the CLI's own docstring for what the number does and does not mean).

## (xvii) The other commands with no section of their own

Two operations exist as `make` targets and had no entry here. One is yours to run; the other is
automated and listed so nobody runs it by hand wondering why.

**`make share-sync SHARE=<source>` — crawl a mounted document share now.** The scheduled job is the
production path (every six hours by default); this is for the first crawl after attaching a share,
and for re-crawling after a bulk change nobody wants to wait six hours for. Run
`make share-estimate SHARE=<source>` first — it walks the share, reads nothing, and tells you what
the crawl would cost.

**`make schedules-apply` — do not run this by hand.** The chart runs it as a post-rollout Job, so
the Schedules follow the deployment automatically. It is here only so that finding it in
`make help` does not read as a missing step.
