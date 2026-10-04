# Operations runbook (admin)

How a system/admin configures and troubleshoots Chemclaw. Everything environment-dependent
comes from the one config source (`src/chemclaw/core/config/`, every field mirrored in `.env.example`,
overridable as `CHEMCLAW_<FIELD>`); in a cluster the same variables arrive through the Helm
chart's `config:` block in `deploy/helm/chemclaw/values.yaml`. `deploy/README.md` is the install
guide; this runbook covers configuring, extending and troubleshooting a running system.

## Prerequisites

- Local dev stack: `make up` starts Temporal (dev server + UI) and Postgres/pgvector;
  `make down` stops it. Then `make db-migrate` once (§(ii)). The **Temporal Web UI is at
  http://localhost:8081** — the first place to look at a running/failed job's event history.
  Frontend gRPC is `localhost:7233`.
- A host with no Docker daemon running: start it first (`sudo -n dockerd &`, then `docker info`
  answers within seconds), or use `make live-infra`, which builds Postgres and Temporal natively
  when no daemon is reachable (see the live-test section below).
- `make help` lists every target with one line each.
- The gate before calling any change done: `make check` (ruff lint + format, `mypy --strict`,
  pytest). `make ci` is what CI runs: the same plus the coverage floor, every validator and the
  dependency audit.

## Logging & troubleshooting

- **Verbosity is one switch.** Set `CHEMCLAW_LOG_LEVEL=DEBUG` (default `INFO`) and restart the
  affected process. `configure_logging()` runs at each entrypoint; no code change.
- **Where logs go and what shape they have.** Every process logs to **stderr** (`logging.basicConfig`
  with no `stream=`), so a `2>/dev/null` or a sidecar tailing only stdout sees nothing; under
  Kubernetes both streams land in the container log and `kubectl logs` shows them. The default line
  is `CHEMCLAW_LOG_FORMAT` — timestamp, level, logger, then `[<correlation_id>/<session_id>]` —
  so a line can be joined to a turn. `CHEMCLAW_LOG_JSON=true` (set in the shipped chart, off in
  code) emits one JSON object per line instead, with the same ids as top-level keys, for a log
  stack to parse. Secrets are redacted on every handler.
- **What gets logged:** each worker logs what it connected to on startup (`background worker
  connected: address=… namespace=… queue=…`, `<bundle> connector worker connected: queue=…`);
  every agent tool call is audited (name, arguments, outcome, latency —
  `src/chemclaw/agent/audit.py`); the ELN sync logs `ingested/rejected` counts plus a WARNING per
  rejected entry, per skipped broken export file, and one aggregated WARNING naming export files
  that arrived too late to be ingested (recovery: §(v)); `DEBUG` adds calculation cache
  hit-vs-compute (`calc cache hit: <key>` — the "why did this recompute?" answer).
- **Changing workflow code:** a control-flow change deployed while a run is in flight fails that run
  on replay. Follow `docs/guides/workflow-versioning.md` (patch-gate or drain) for any release touching a
  `@workflow.defn` body.
- **A stuck/failed job:** open the Temporal UI (:8081 locally) → the workflow → event history;
  cross-check the worker's logs. A worker not picking up jobs is usually the wrong
  queue/namespace — the startup line above shows exactly what it connected to — or a worker that
  is not running at all; §(x) is how to ask a worker directly.
- **Database down:** connections fail fast with `ConnectionError: Postgres unreachable at
  <dsn>: <cause>` (password redacted). It is a retryable infra fault, so Temporal retries the
  activity; fix the DSN/host and it recovers. The front door's `/readyz` answers 503 meanwhile.
- **OpenTelemetry traces.** Off in code, **on in the shipped chart**: `CHEMCLAW_OTEL_ENABLED=true`
  plus `CHEMCLAW_OTEL_ENDPOINT` (the chart's placeholder is
  `http://otel-collector.observability.svc:4317` — point it at your collector, and allow it in
  `networkPolicy` egress, or spans are dropped). `CHEMCLAW_OTEL_ENDPOINT` is copied into
  `OTEL_EXPORTER_OTLP_ENDPOINT` unless that is already set, so the standard `OTEL_EXPORTER_OTLP_*`
  variables keep working. The SDK and the OTLP gRPC exporter are ordinary dependencies of the
  image; an install without them that enables tracing fails at startup with a directive error
  rather than an import error.
  - **What the process installs:** a `TracerProvider` whose spans go through a `BatchSpanProcessor`
    to the OTLP **span** exporter, tagged `service.version=<CHEMCLAW_DEPLOYMENT_REVISION>`. The
    chart sets `OTEL_SERVICE_NAME=chemclaw-<component>` per Deployment (`service`,
    `background-worker`, `mcp-face`, `connector-<name>`, `connector-worker-<name>`,
    `interactive-worker-<name>`), and `OTEL_RESOURCE_ATTRIBUTES` adds `k8s.pod.name`,
    `k8s.namespace.name` and `chemclaw.component` from the downward API — so each process role is
    its own service in the trace backend. Outside the chart the service name is `chemclaw`.
    Traces only, on purpose: metrics are `/metrics` (Prometheus, scraped per pod) and logs are the
    stderr stream above, and neither needs a second copy over OTLP.
  - **`CHEMCLAW_OTEL_LLM_SPANS=true` gives you a span per model call** — token counts, model name
    and provider, plus the chain and tool spans around them — through OpenInference's LangChain
    instrumentation, over the same OTLP exporter. On in the shipped chart. Point the collector at
    Arize Phoenix to read these conventions natively; any OTLP backend receives the same spans, and
    nothing in the image depends on Phoenix.
  - **`CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA` decides whether those spans carry content**, and it is
    off (stated `"false"` in the chart). Off sets every OpenInference hide flag, so a span carries
    identifiers and counts and nothing a chemist typed. Turning it on is a decision about content
    leaving the pod: the collector's store then holds the same class of data `SECURITY.md`
    describes for the audit trail. It governs nothing while `CHEMCLAW_OTEL_LLM_SPANS` is off, and
    the process says so at WARNING if you set it anyway.
  - **Per-model token attribution is a trace query, not a metric.** The OTel metric
    `gen_ai.client.token.usage` went out with the previous agent framework and nothing in
    `langchain`, `langgraph` or `langsmith` emits it. Each model-call span carries
    `llm.token_count.prompt`/`.completion`/`.total` and `llm.model_name` instead. `/metrics`'s token
    counters carry `profile` rather than model, deliberately (D-152); §(viii) reads them.
  - **`make phoenix-up` gives the eval lane a trace reader**, on 6006 (UI) and 4317 (OTLP), from
    `infra/docker-compose.observability.yml` (`make phoenix-down` stops it).
    `infra/live/processes.sh` probes 4317 and turns the exporter on only when something is
    listening, so a lane started without it is unchanged. It is **not** in the Helm chart and is
    not meant to be: production traces go to whatever collector your org runs.
  - **`make phoenix-publish DIR=<transcripts> [NAME=<experiment>]` publishes a probe run you
    already have.** It calls no model — the run's `{probe, outcome}` files and the judge's
    `grades.json` beside them are the record. The dataset is the probe corpus
    (`data/evals/probes/`) and each run is an experiment over it, so publishing two runs into the
    same dataset lets Phoenix diff them.

## Exposing the front door (the two settings that decide whether it boots)

- **`CHEMCLAW_ENTRA_REQUIRED=true` for anything reachable beyond localhost.** With it off, every
  request runs as the shared dev principal with all authorization gates open, so the service
  **refuses to start** on a non-loopback bind (the `0.0.0.0` default) with `SECURITY:
  entra_required is False but the service binds a non-loopback interface …`. That message means the
  guard worked, not that the deployment is broken: set `CHEMCLAW_ENTRA_REQUIRED=true` (plus
  `CHEMCLAW_ENTRA_TENANT_ID` and `CHEMCLAW_ENTRA_AUDIENCE`, which are validated together at
  startup), or bind loopback for local dev. `CHEMCLAW_SERVICE_ALLOW_INSECURE=true` is the conscious
  opt-out and boots with a loud warning instead.
- **What `CHEMCLAW_ENTRA_REQUIRED=true` also refuses at startup** — each a `ValueError` naming the
  setting, so read the message rather than the stack
  (`D-2026-09-04-the-configurations-this-tree-now-refuses-to-start-in`):
  - a non-loopback `CHEMCLAW_TEMPORAL_ADDRESS` with none of `CHEMCLAW_TEMPORAL_TLS_CA` /
    `_TLS_CERT` / `CHEMCLAW_TEMPORAL_API_KEY` set (a plaintext broker channel);
  - a non-loopback `CHEMCLAW_POSTGRES_DSN` (and `_POSTGRES_MIGRATION_DSN`,
    `_SESSION_STORE_DSN` when set) without `sslmode=require`, `verify-ca` or `verify-full`
    (`verify-full` with `sslrootcert=` recommended). A DSN with no host at all is refused too —
    name the host;
  - `CHEMCLAW_HARNESS_AUTONOMY=plan_only` with `CHEMCLAW_HARNESS_ENABLED=false` (an approval gate
    that is not attached). The chart enables the harness.
- **Every process that makes model calls refuses a loopback `CHEMCLAW_LLM_BASE_URL`** (the code
  default is the mock gateway on `127.0.0.1:8820`) unless `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true`
  (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`). In a deployment
  set `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL` and, if the gateway wants one,
  `CHEMCLAW_LLM_API_KEY`.
- **Stale variables are not reported.** pydantic-settings reads only the `CHEMCLAW_` names it has
  fields for, so a removed setting left in the environment or a ConfigMap (for example
  `CHEMCLAW_ENTRA_CLIENT_ID`, or the `CHEMCLAW_PROPOSAL_*` pair of §(ix)) is silently ignored —
  remove it by hand. Only a *dotenv file* is checked: an unknown key there fails startup with
  `Extra inputs are not permitted`.
- **`CHEMCLAW_NOTE_REPO_DIR` must be set on any host that records notes — the default is always
  wrong in a deployment.** It ships as `.` (a dev convenience), which resolves to the process CWD.
  A note write commits **straight onto the base branch** of the clone it is handed and pushes that
  branch to the clone's remote (`kg/git_writer.py`) — there is no `note/<id>` branch, no force-push
  and no review step since `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`. Pointed at the
  checkout the service itself runs from, that would commit into the running application's source
  tree and push to the source repository, so `_require_dedicated_checkout` refuses before any git
  command runs, with `note_repo_dir '.' resolves to <path> — the checkout this process is running
  from`. That error is the guard doing its job, not a broken deployment. What the directory has to
  be, each item driven against `GitNoteWriter` rather than recalled:
  1. **A clone with a real `.git` directory, used by nothing else.** A linked worktree (`.git` is a
     file) is refused by name, because the cross-process write lock is a file under `.git/`.
  2. **Checked out on `CHEMCLAW_NOTE_BASE_BRANCH`** (default `main`). Notes are committed onto
     that branch, and a checkout on any other is refused by name (`the notes checkout at … is on
     'master', not the base branch 'main'`) — `git init` still names the branch `master` on many
     installs.
  3. **A remote named `CHEMCLAW_GIT_REMOTE`** (default `origin`) **that already carries the base
     branch.** Every write opens with `git fetch <remote> <base>` and `merge --ff-only`, and ends
     with `git push <remote> HEAD:refs/heads/<base>`. A bare `git init` fails here twice over — no
     remote, and an empty remote has no `main` to fetch — and both surface as a *retryable*
     `GitRemoteError`, so the write is retried before it is dropped. A local bare repository is
     enough for a dev or mock stack: `git init --bare -b main /srv/notes-origin.git`, then in the
     clone `git remote add origin /srv/notes-origin.git && git push -u origin main`. A local commit
     of its own is not required once the remote has one.
  4. **No committer identity is needed from the clone.** The writer hands every git child
     `CHEMCLAW_NOTE_COMMITTER_NAME`/`CHEMCLAW_NOTE_COMMITTER_EMAIL` as `GIT_AUTHOR_*` and
     `GIT_COMMITTER_*` (defaults `ChemClaw` / `chemclaw-notes@chemclaw.invalid`), which outrank any
     `user.*` config. Set a real address only if the notes remote's forge refuses the default.
  5. **The existing `knowledge/` tree.** Readers resolve `settings.knowledge_path`, which is
     `note_repo_dir` joined with `knowledge_dir` and nothing else, so this clone *is* the graph every
     reader scans: an empty clone serves an empty graph, with no error anywhere.

  A shallow clone is not refused — a note lands in a `--depth 1` clone — but the chart clones full
  history and the rebase that replays an unpushed note has not been driven on a shallow one.

  The Helm chart supplies items 1, 2, 3 and 5, and the writer item 4: `deploy/knowledge-sync.sh checkout` clones
  `knowledge.sync.repoUrl` on the base branch into `knowledge.noteRepoPath` (default
  `/var/lib/chemclaw/note-repo`), and the sync sidecar keeps it current with the writer's own
  `fetch` + `merge --ff-only` under the writer's lock rather than a reset, so a note whose push
  failed stays committed, stays readable, and is replayed by the next write. The *shallow* replica
  at `knowledge.sync.checkoutPath` is what a pod that records nothing publishes from, never what
  the writer commits into. Leaving the variable unset outside Helm is the quieter failure:
  `knowledge-sync.sh` logs `CHEMCLAW_NOTE_REPO_DIR unset — no writer clone provisioned` and skips
  the clone, so the first note write is the thing that discovers it. **Nothing reports a broken
  notes clone before that write does** — `/readyz` gates on Postgres reachability and schema
  currency and counts connectors, and does not look at the note repository at all. Check one by
  hand with `git -C "$CHEMCLAW_NOTE_REPO_DIR" status` and `git -C "$CHEMCLAW_NOTE_REPO_DIR" fetch`
  from the pod that writes; afterwards `chemclaw_notes_publish_failures_total` (§(ix)) is the
  signal.
- **Note writing is serialized per host.** Keep the background worker at one replica
  (`workers.background.replicas: 1` in `deploy/helm/chemclaw/values.yaml`); the writer's checkout lock is host-local, and the
  cross-pod half is the Postgres advisory lock, which is taken only under
  `CHEMCLAW_SESSION_STORE=postgres` (the chart sets it). Two writers on one `note_repo_dir` share
  one working tree and one index, so the second stages its files into the first's in-flight commit. On a filesystem where
  `flock` is not honoured (some NFS/ReadWriteMany setups) that assumption fails, and nothing
  serialises two writes on that one index at all.

## Talk to the agent from a terminal (testing)

Chemists reach the system through the front door (`chemclaw.api.app`, `CHEMCLAW_SERVICE_PORT`,
default 8080; the live lane runs it on 8000) and the
`Chemclaw3_ui` frontend, signed in with Entra ID. For local testing there is a CLI that builds the
same agent in-process: `make chat` (or `uv run chemclaw --admin`, the console script for
`python -m chemclaw.cli.chat`). It needs no Postgres or Temporal for a plain conversation
(`CHEMCLAW_SESSION_STORE=memory` is the code default); durable-job tools need `make up` and the
workers.

**The model gateway.** Every model call goes to the one OpenAI-compatible endpoint
`CHEMCLAW_LLM_BASE_URL` names, serving `CHEMCLAW_LLM_MODEL`
(`D-2026-09-04-a-gateway-is-the-only-provider`). The code default is the mock gateway
`chemclaw.cli.mock_llm` on `http://127.0.0.1:8820/v1` with model `mock` — start it in a second
terminal with `uv run python -m chemclaw.cli.mock_llm` for a credential-free session (it answers
from a scripted catalogue, not with chemistry). For real answers point the two settings at your
gateway and put its credential on `CHEMCLAW_LLM_API_KEY`.

- **The loopback guard.** Every process that makes model calls — this CLI, the front door, the MCP
  face and the background worker — refuses to boot on a loopback gateway unless
  `CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY=true`
  (`D-2026-09-12-a-gateway-guard-in-the-front-door-is-not-a-deployment-guard`). `make chat` and
  `infra/live/processes.sh` set it for you; `.env.example` ships it `false`, so the raw
  `uv run chemclaw --admin` form needs it exported, and without it exits with one sentence naming
  the two ways forward.
- **There is no credential preflight.** An empty key is legitimate (many internal gateways ignore
  the bearer), so a gateway that does want one answers 401 on the first turn rather than at
  startup. A *blanked* `CHEMCLAW_LLM_BASE_URL` or `CHEMCLAW_LLM_MODEL` is refused at startup with
  one sentence and a non-zero exit.
- **Admin mode is required.** The CLI has no OIDC token to validate, so it runs only with
  `--admin`, and without it refuses and exits non-zero. `--admin` bypasses *authentication* only:
  the turn runs as `CHEMCLAW_CLI_ADMIN_ACTOR` (default `admin@localhost`) holding the roles in
  `CHEMCLAW_CLI_ADMIN_ROLES` — **empty by default**, so it confers identity and no entitlement. A
  tool or skill gated on a role is refused or hidden here exactly as it would be for a chemist
  without that role; list the role there to test the entitled path.
- **One-shot vs. REPL:** `uv run chemclaw --admin -m "which solvent next for …?"` asks one
  question, prints the answer to stdout, and exits — `0`, or `2` when the answer it printed is
  incomplete (a cap was reached or the turn produced nothing; the reason is on stderr). With no
  `-m` it is an interactive chat; the thread accumulates, and `exit`, `quit`, `:q` or Ctrl-D leave.
- **REPL commands.** The harness ships in `plan_only` mode, so a state-changing tool waits for an
  approved plan: `/plan` shows the current plan, the state-changing tools it declares and whether
  it is approved; `/approve` approves it. `/workflows`, `/approve-workflow <name> [<fingerprint>]`
  and `/forget-workflow <name>` list, approve and drop composed workflows.
- **Attribute the run:** `--actor alice@lab` overrides the audit actor. The tool-audit trail is
  durable wherever `CHEMCLAW_SESSION_STORE=postgres`, log-only otherwise; `--audit-postgres`
  forces it to Postgres.

## Live-test the whole stack (Temporal + workers + Postgres, then the model)

The lane that proves the durable half actually works. Everything before this section tests one
layer; this runs the path a durable capability really takes — agent tool → `ConnectorJobWorkflow`
on `background-jobs` → the bundle's workflow on `connector-<name>` → the calculation cache →
`job_records` → the audit trail — against a real broker and a real database. The unit suite's
Temporal tests use the time-skipping test server with no model and no database, so this lane is
the only thing that exercises that path end to end
(`D-2026-08-04-a-lane-that-only-runs-where-docker-runs`).

**Preconditions.** A `Chemclaw3-mcp` checkout beside this one (elsewhere, export the path in the
variable `make live-up`'s refusal names) — `make live-up` starts the fleet servers from it and refuses to start without it, because
the lane runs with `CHEMCLAW_CONNECTORS_REQUIRED=true`, the posture the chart ships. `uv` on the
path for both checkouts. A Docker daemon is optional (below).

```sh
make live-infra     # Postgres/pgvector + Temporal — uses docker-compose when a daemon is
                    # reachable, otherwise builds and starts them natively (infra/live/)
make db-migrate     # apply infra/sql
make live-up        # this repo's connectors (:8810), the Temporal workers (incl. the interactive
                    # ones), the mock model gateway when the lane is pointed at it, the front door
                    # (:8000), and from the fleet checkout every server this repo declares an
                    # endpoint for plus the calc and rxnlabel backends — each on the port its own
                    # manifest or setting names
make live-status    # what is running (pids and ports, from .live/run/)
make live-jobs      # STAGE A: a real durable job, no model needed
make live-probes    # STAGE B: the probe corpus through the front door (needs a real gateway;
                    #   exits 3 if it reached nothing, 2 if nothing was graded — which is what
                    #   a mock gateway produces, since a script cannot be judged)
make live-down && make live-infra-down
```

**Stage A (`make live-jobs`) needs no model credential and is the load-bearing one.** It launches
`compute_reaction_energy` through the *real* generated job tool and then asks the live system
questions that have mechanical answers — the workflow's terminal state from Temporal, the cache row
and the `job_records` row from Postgres, whether a duplicate launch rejoins rather than recomputes,
whether a job whose worker is wedged comes back *pending* rather than hanging or crashing. Nothing
is scored from prose. It prints how many checks passed and exits non-zero if any failed. The
report lands in `tasks/live-test/transcripts/durable/<utc-stamp>/durable-smoke.md` — a new
directory per run, so a run never overwrites the committed record; promoting a run into the record
is a deliberate copy, and `--report` (and `make live-probes ARGS='--transcript-dir …'`) put output
exactly where you say.

**Two prerequisites the corpus layer needs, or the probes measure an empty database.** `make
reindex` fills `note_index`; the fingerprint tables are filled only as a side effect of the ELN
sync, so run it once from the epoch: `uv run python -m chemclaw.cli.live_data --backfill-only`
starts `ElnSyncWorkflow` on `background-jobs` and reports what arrived (`make live-data` then checks
the corpus value by value). The note writer needs a *dedicated* clone — `bootstrap.sh` creates
`.live/knowledge-repo` and `processes.sh` points `CHEMCLAW_NOTE_REPO_DIR` at it, because the
default `.` is the working checkout, which the writer refuses (see "Exposing the front door"), and
the whole knowledge-contribution half of a run would silently disappear.

**`make live-storm` is the third stage, and it needs no model at all.** The shipped default already
points the lane at the mock (`CHEMCLAW_LLM_BASE_URL=http://127.0.0.1:8820/v1`,
`CHEMCLAW_LLM_MODEL=mock`) and `make live-up` starts `chemclaw.cli.mock_llm` alongside everything
else. The storm then drives load, adversarial model behaviour and the front door's own limits with
zero LLM calls — the mock reports how many requests it served, which is how the run *proves* that
rather than asserting it. It is an HTTP mock speaking OpenAI's `/v1/chat/completions` (what the
agent's `ChatOpenAI` client posts to) and `/v1/responses`, not an injected chat client: the
streaming assembler, the middleware stack, budget admission, the audit sink and the session store
all sit between the socket and the agent. `make live-storm ARGS='--help'` lists its options.

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

- **The front door boots with no model credential.** There is no credential preflight
  (`D-2026-09-04-a-gateway-is-the-only-provider`), so the lane always starts and a gateway that
  wants a credential answers 401 on the first turn. To run Stage B against a real model, export
  `CHEMCLAW_LLM_BASE_URL`, `CHEMCLAW_LLM_MODEL` and `CHEMCLAW_LLM_API_KEY` before `make live-up`;
  with no base URL named the lane runs against the mock, which Stage A and the storm need and
  Stage B cannot grade.
- **The lane pins `CHEMCLAW_SERVICE_HOST=127.0.0.1`.** With `entra_required=false` the front door
  refuses a non-loopback bind (SEC-2) and the default is `0.0.0.0`, so without this it would
  correctly fail to start.
- **`eval "$(bash infra/live/processes.sh env)"` before running anything from a second terminal.**
  Every bundle this repository hosts authenticates its own `/mcp`, and `make live-up` *mints* those
  credentials rather than defaulting them. A command run in a fresh shell would mint its own and get
  401s from servers that are plainly up. `up` writes them into the lane's run directory and `env`
  reads them back (the subcommand is the contract, not the file). It carries
  `CHEMCLAW_LIVE_PROBE_TOKEN` too when the lane is enforcing identity, and `down` removes it.
  `bash infra/live/processes.sh restart <name>` kills and restarts one process with the same
  credentials.
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
- **Each worker gets its own probe port**, and the file is the authority: `processes.sh` writes
  each one to `.live/run/<name>.port` (`curl -s localhost:$(cat .live/run/worker-background.port)/readyz`).
  Do not assume a contiguous range — the bundle workers and the interactive workers are numbered
  separately. Each process logs to `.live/<name>.log`; a process that dies on start is named
  there by `make live-up`'s error.
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
and out, so a skill cannot outlive the tool it teaches. Run it before restarting.

**Two stored tiers sit on top of those directories**, and neither needs a release
(`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius`). Both live in the agent's memory
store, so they need `CHEMCLAW_SESSION_STORE=postgres` and `CHEMCLAW_AGENT_MEMORY_ENABLED=true`
(both set in the chart); without them the routes answer 503 rather than an empty list.

- **The organisation's tier** — in the prompt of every turn on this deployment. Any signed-in
  caller may read it (`GET /skills/org`, `GET /skills/org/{name}`,
  `GET /skills/org/{name}/versions`); changing it takes a role in
  `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`: `POST /skills/org` with `{"body": "<the whole SKILL.md,
  front-matter included>"}` publishes, `DELETE /skills/org/{name}` retires, and
  `POST /skills/org/{name}/revert` with `{"content_hash": "<a hash from the versions listing>"}`
  makes an earlier body active again — the store keeps every version, so a bad skill is reverted,
  not re-authored. A refusal is a 403 and is counted on `chemclaw_authz_refusals_total`.
- **A chemist's own tier** — `GET|POST /skills/mine`, `GET|DELETE /skills/mine/{name}`, acting on
  that person's turns only; the count per person is capped by `CHEMCLAW_AGENT_LOCAL_SKILLS_MAX`
  (409 past it).
- **What the agent proposes** (`propose_skill`) waits in the proposer's own queue:
  `GET /proposals`, decided with `POST /proposals/{kind}/{name}` and
  `{"content_hash": "…", "accepted": true|false}` (a decision is final). An administrator promotes a
  proposal to the organisation by posting its body to `POST /skills/org`, never by deciding
  somebody else's queue. No agent path writes a `SKILL.md` anywhere.

## (ii) Add or repoint a database

Set `CHEMCLAW_POSTGRES_DSN` and run `make db-migrate`. It applies every not-yet-applied
`infra/sql/*.sql` file in filename order and records each in the `schema_migrations` ledger with a
checksum (D-034), so re-running applies nothing and is safe; an already-applied file whose contents
changed fails the run with `MigrationError` rather than being silently skipped — a schema change is
always a new file (`infra/sql/NNN_<what>.sql`, the next free number). The target then converts any
stored session messages to the current shape (`python -m chemclaw.agent.message_migration`). In
the chart, a pre-install/pre-upgrade hook Job runs the migrations, `chemclaw.agent.store_setup` and
the grants, and a post-install/post-upgrade Job runs the message conversion; by hand, run the
commands below from a host that can reach the database. Verify with `SELECT filename FROM schema_migrations ORDER BY filename DESC
LIMIT 1;` against the newest file in `infra/sql/`.

Things that are easy to miss when repointing:

- **Under `CHEMCLAW_ENTRA_REQUIRED=true` a non-loopback DSN must state `sslmode=require`,
  `verify-ca` or `verify-full`**, or the process refuses to start (see "Exposing the front door").
- **The session layer can live in a second database**: `CHEMCLAW_SESSION_STORE_DSN` (empty means
  `CHEMCLAW_POSTGRES_DSN`) holds the conversation transcripts, LangGraph checkpoints, plan approvals,
  turn costs and the effect ledger. Migrate both.
- **Two releases need two databases.** Nothing in the schema is namespaced by release, and no
  chart guard can see that two releases share one.
- **The fingerprint width is coupled to the schema**: the `bit(2048)` columns must match
  `CHEMCLAW_ECFP_BITS` / `CHEMCLAW_DRFP_BITS` — see §(vi) before changing either.

**A `migrate.server_warning` line is a migration asking for a person.** The one that emits it
today is `108`: on a database an older image wrote a `NaN` or `±inf` into, it adds its finiteness
check `NOT VALID` rather than abort the run. New writes are refused either way, but the old rows
still reach a surrogate through `observations_for`. Confirm with `SELECT convalidated FROM
pg_constraint WHERE conname = 'experiment_arm_results_value_finite'` (`false` means the rows are
still there). Inspect them with `SELECT * FROM experiment_arm_results WHERE value IN
('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)` and, once they are dealt with, run
`ALTER TABLE experiment_arm_results VALIDATE CONSTRAINT experiment_arm_results_value_finite` by
hand.

**By hand, the full sequence is**

```sh
make db-migrate                                   # migrations + message conversion
uv run python -m chemclaw.agent.store_setup       # the agent memory store's tables, as the migrator
make db-grants                                    # the runtime role's privileges
```

(the Helm hook Job runs the same three, so this only concerns migrating by hand). The grants are
*not* in the tracked migration set and are re-applied on every deploy on purpose: a table added by a
new migration ships with no grant until they run, so a split-principal deployment breaks on first
use of it with `InsufficientPrivilege`. `store_setup` must come before them because the memory
store's tables (`store`, `store_migrations`) are upstream's schema, created outside `infra/sql/`,
and the grants only cover tables that exist. The grants no-op where no `chemclaw_app` role
exists.

### Splitting the database principal (optional)

By default one credential does everything, and `infra/sql/006` describes `audit_events` as
"append-only by contract" — a contract nothing enforced and nothing prevented breaking. To make it
append-only in fact (D-2026-08-05-append-only-by-grant-not-by-contract):

1. Create a login role the application runs as, owning none of the migrated schema:
   `CREATE ROLE chemclaw_app LOGIN PASSWORD '…';`
2. Point `CHEMCLAW_POSTGRES_DSN` at it, and put the schema owner's DSN in
   `CHEMCLAW_POSTGRES_MIGRATION_DSN` — in the chart, `secrets.migrationKeys`, which is mounted on
   the migration hook Job and on nothing else.
3. Run the three-command sequence above (`make db-migrate`, `store_setup`, `make db-grants`) —
   all three connect with the migration DSN.

Verify it took: as `chemclaw_app`, `INSERT INTO audit_events …` succeeds and
`DELETE FROM audit_events` fails with `InsufficientPrivilege`. The owner credential can still
rewrite the trail — this narrows who holds that power and for how long, it does not remove it — so
the grant is the whole of the guarantee. The role also needs no `CREATE EXTENSION` right;
that stays with the migrator, which is where `vector` already required superuser on most managed
Postgres.

**It does need `CREATE` on schema `public`, and step 3 is what gives it.** The tables LangGraph
keeps its turn state in are created by the *application* (`AsyncPostgresSaver.setup()`,
`AsyncPostgresStore.setup()`), and no migration in `infra/sql` declares them. Under PostgreSQL 15+
`PUBLIC` holds no `CREATE` on `public`, so without the grant the first turn fails with
`permission denied for schema public`. `infra/sql/grants/app_privileges.sql` grants it — but a
deployment that provisions its role by hand, or that locks the schema down after `make db-grants`,
has to keep it: upstream issues `CREATE TABLE IF NOT EXISTS` on **every process start**, and
Postgres checks the schema ACL before it checks existence, so pre-creating the tables does not
substitute for it. Verify with `SELECT has_schema_privilege('chemclaw_app', 'public', 'CREATE');`
→ `t`.

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
`connector.yaml` (D-120). The shipped ones are under `src/chemclaw/ingest/sources/`:

| Source | Halves | What it is |
| --- | --- | --- |
| `eln-json` | ingest | a free-text JSON ELN export directory (`CHEMCLAW_ELN_EXPORT_DIR`) |
| `eln-ord` | ingest | a native ORD export directory (`CHEMCLAW_ORD_EXPORT_DIR`) |
| `eln-databricks` | ingest + retrieve | a warehouse ELN, described by a `binding:` (see below) |
| `graph` | retrieve | the knowledge graph's notes |
| `vector`, `lexical` | retrieve | dense and lexical search over the same notes |
| `sharedrive` | retrieve | a mounted SMB/CIFS document share (see below) |
| `pistachio` | retrieve | a licensed reaction corpus in a warehouse |
| `vendored` | retrieve | datasets baked into the image under `data/vendored/` |
| `commitments-json` | commitments | a portfolio tool's JSON extract of programmes and milestones |

Set which are active with `CHEMCLAW_DATA_SOURCES` (a comma list; the default, which the chart does
not override, is `graph,eln-json`). The durable sync (`ElnSyncWorkflow`, Schedule `eln-sync`) ingests
**every** active ingest source, each with its own high-water cursor keyed by source name in
`sync_cursors`, so sources advance independently; the memory jobs read the same active set. After
changing the set, re-run `make schedules-apply` (the chart's post-install/post-upgrade hook does it)
so each Schedule a source earns is created — and one it no longer earns is pruned.

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
ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter
config:
  export_dir: /mnt/eln/staging
```

Then add `eln-json-staging` to `CHEMCLAW_DATA_SOURCES` and run `make datasource-validate`. Because
`CHEMCLAW_DATA_SOURCES_DIR` replaces the shipped default rather than extending it, list the shipped
directory after your own or the shipped sources disappear — with the variable unset,
`python -c 'from chemclaw.core.config import settings; print(settings.data_sources_dir)'` prints
it. The bare `eln-json`/`eln-ord` sources carry no `config:`, so they fall
back to `CHEMCLAW_ELN_EXPORT_DIR` / `CHEMCLAW_ORD_EXPORT_DIR`. Validate an export's reactions with
`make eln-validate`.

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
share, reads nothing, and reports what would be indexed and what cannot be read. Once enabled the
`document-sync` Schedule crawls it every `CHEMCLAW_DOCUMENT_SYNC_SCHEDULE_MINUTES`; `make
share-sync SHARE=<source>` crawls it now. The full concept, including how the share's AD group
becomes an entitlement, is in `docs/guides/sharedrive-concept.md`.

**Any active ingest source turns the reaction labeller on, with no second switch** — and the
default `CHEMCLAW_DATA_SOURCES` includes one (`eln-json`). `durable/schedules.py` creates a
`reaction-labels` Schedule whenever an ingest source or a warehouse reaction-corpus binding is
active, so a stock deployment's background worker dials `CHEMCLAW_RXNLABEL_SERVER_URL`
(`Chemclaw3-mcp`'s `rxnlabel` backend) every `CHEMCLAW_LABEL_SYNC_SCHEDULE_MINUTES`. The chart
ships an in-cluster Service name for it, `secrets.optionalKeys.rxnlabelToken` for its bearer and
`networkPolicy.egressPorts.rxnlabel` for the wire — check all three on install, because none of
them fails loudly: an unreachable labeller leaves the drain retrying and every faceted precedent
question answering from an empty label index.

## (iv) Add a capability — a tool, a durable job, and their skills (a **connector**)

A capability is a **connector bundle**: one folder declaring everything it contributes, and the only
mechanism for adding a tool (D-110). The shipped bundles are `src/chemclaw/connectors/<name>/`;
`src/chemclaw/connectors/README.md` is the manifest reference.

```
connectors/<name>/
  connector.yaml      # the manifest — the whole contract
  server/app.py       # optional: the FastAPI+MCP app, when this repository serves the capability
  workflows.py        # optional: its Temporal workflow(s), when the work runs long
  activities.py       # optional: their activities
  worker.py           # optional: the bundle's Temporal worker entrypoint
  skills/             # optional: the SKILL.md judgment that belongs to this capability
  profiles/           # optional: the agent profiles it enables
```

**To add one:**

1. Create the folder with a `connector.yaml`. For an MCP capability, declare an `endpoint:`
   (`transport: http`, its `url:`, optional `health_url:` and `request_timeout:` in seconds, and an
   `auth:` block) and the `tools:` the agent may call — then classify **every** tool exactly once
   under `read_only:` or `state_changing:`. The manifest refuses to load otherwise, because that
   split is what the plan gate and the dry-run gate read, and an unclassified tool would fail open.
   The agent-facing list is read/compute only: `make connector-validate` refuses a mutating name
   (`index_*`, `write_*`, `delete_*`, …), because mutation belongs on the job path or on a core
   knowledge-writing tool.
2. For a long-running capability, declare a `jobs:` entry naming the Temporal **workflow type**. The
   queue is *not* declared — it is `connector-<name>`, derived at dispatch, because a bundle's worker
   serves only what the bundle's own modules registered (D-150). Its workflow returns a
   `ConnectorJobResult`
   (`summary`, `data`, optional `note`); core's `ConnectorJobWorkflow` supplies the idempotent job
   id, the actor attribution, the note write and the session push-back. A job declares its
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
   declares — `tests/test_repo_map.py` pins that shape).
3. *If an interactive tool is expensive enough that a burst would overrun its server* — a
   depiction, a prediction, a seconds-long calculation — list it under `endpoint.queued:`
   (`tools:` plus `inline_wait_seconds:`). The call then waits in a `connector-<name>-interactive`
   Temporal queue for a free slot instead of being refused by a full pod, the turn waits up to
   `inline_wait_seconds` for the answer, and a longer call returns a job id like any durable job
   (`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`). Only a tool
   whose answer is a function of its arguments belongs there. Such a bundle needs an
   **interactive worker**: `python -m chemclaw.connectors.interactive_worker <name>`, rendered by
   the chart from `connectors.<name>.interactive` (`replicas`, `maxReplicas`,
   `maxConcurrentActivities` — size the last to the server's own concurrency slots;
   `keda.enabled=true` scales it on queue backlog where the KEDA operator is installed).
4. Run `make connector-validate`. It checks the manifest, that declared skills/profiles exist (and
   that no undeclared ones are hiding in the bundle), the read-only tool surface, that every job
   can actually be built, that a bundle's served tools match its declaration, and that every
   `CHEMCLAW_CONNECTOR_URLS` key names a bundle.
5. Enabling: an empty `CHEMCLAW_CONNECTORS_ENABLED` runs every discovered bundle **except** those
   whose manifest says `default_enabled: false`. Set a pathsep list to narrow, to fix the order
   (tool order is part of the prompt), or to name an off-by-default bundle; an unknown name there is
   a startup error, not a silently missing capability. In the chart the list is computed from the
   `connectors:` block (`connectors.<name>.enabled`), so set it there instead.

**Running them.** `make connectors` serves every enabled bundle that has a `server/` in this
repository in one dev process (`:8810`) and prints the `CHEMCLAW_CONNECTOR_URLS` to point the front
door at. In a cluster, each such bundle is its own Deployment + Service
(`connectors.<name>.enabled`, `server: true`, no `url:`), a bundle with `jobs:` gets a worker
Deployment with `worker: true`, and the chart *computes* `CHEMCLAW_CONNECTOR_URLS` from that same
block, so addresses cannot drift from the pods that exist.

**A bundle this image does not ship** — one of `Chemclaw3-mcp`'s `manifests/`, or a private one —
is mounted rather than built in: put its folder in a ConfigMap and list it under
`extraConnectors.bundles` (`{name: <bundle>, configMap: <configmap>}`); the chart mounts each at
`extraConnectors.mountPath/<name>` and prepends that directory to `CHEMCLAW_CONNECTORS_DIR`. Then
give it a `connectors.<name>` entry (`enabled: true`, `server: true`, `url:`) and do the three
steps below.

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
   then provide the token: add the key, named as the environment variable (e.g.
   `CHEMCLAW_<NAME>_TOKEN`), to the release's Secret (`secrets.name`) and list it under
   `secrets.optionalKeys`. Both network halves are needed and the second is the one that gets
   missed: a NetworkPolicy egress rule restricts by port independently of the destination list,
   so a server on its own port is dropped no matter what you add to `egressDestinations`. Only
   assume `egressPorts.https` covers it if the server really is on 443. Every sibling server
   `Chemclaw3-mcp` runs is plain HTTP on a port of its own, and
   `deploy/helm/chemclaw/values.yaml`'s `networkPolicy.egressPorts` is the maintained roster of
   which — **one entry per server**; read the port there rather than from this page.

Verify after `helm upgrade`: the front door's startup log carries a WARNING per connector it could
not reach, `/readyz` reports the unhealthy count, and `chemclaw_connector_unhealthy{connector="<name>"}`
reads 0 for the bundle (see **Troubleshooting** below).

The tools such a server exposes are still read/compute only, still narrowed by `tools:`, and still
carry the turn's identity headers as *advisory* context — a connector outside our trust boundary
must never make an access decision on a header's word (`connectors/identity.py`).

**Configuration.** `CHEMCLAW_CONNECTORS_DIR` (pathsep, like `PATH` — prepend a private bundle dir to
override a shipped one; setting it *replaces* the shipped default, so name the shipped directory
too), `CHEMCLAW_CONNECTORS_ENABLED`, `CHEMCLAW_CONNECTOR_URLS` (a JSON object, bundle name → MCP
URL), `CHEMCLAW_CONNECTORS_REQUIRED`, `CHEMCLAW_CONNECTOR_HEALTH_TIMEOUT_SECONDS`,
`CHEMCLAW_CONNECTOR_JOB_TIMEOUT_SECONDS`. A connector's request timeout and auth mode are per-manifest
(`endpoint.request_timeout`, `endpoint.auth`); the `bearer` mode names an env var, so no credential is
ever written into a bundle.

**What a name collision on that path does and does not replace.** The first directory wins the name
outright and the loser's manifest is not merged, not warned about and not logged — so the winning
manifest is the whole of the **tool surface**. It is *not* the whole of the bundle: a bundle's
`skills/` and `profiles/` directories are read from **every** directory carrying its name, winner
first (`connectors/registry._bundle_content_dirs`). That matters for the fleet's own manifests:
`Chemclaw3-mcp` publishes `chem` and `safety` under the same names and with no `skills:`, so when
its directory is first, its manifest wins and this repository's `connectors/safety/skills/` judgment
still loads — no `CHEMCLAW_SKILLS_DIR` workaround is needed.
`tests/test_sibling_manifest_agreement.py` compares every bundle-level manifest key between the two
trees.

**Troubleshooting.** Each enabled connector is probed as one of five states: `healthy`,
`unreachable` (the health route did not answer), `unpolled` (Temporal answered and nothing polls the
bundle's `connector-<name>` queue — a bundle that owns durable work and whose worker fleet is at
zero, whether or not it also serves an endpoint), `unknown` (the
queue could not be asked at all, so reachability was not determined; this neither counts nor gates,
because a broker outage is one fault shared by every durable bundle) or `unprobed` (nothing to ask —
no `health_url` declared and no durable work, honest for a third-party server). `unreachable` and
`unpolled` are the two that count as unhealthy and trip `connectors_required`.
`GET /readyz` reports the *count* of unhealthy ones and never their names — it is unauthenticated
by necessity, so its body is a public document. The names are on `/metrics` —
`chemclaw_connector_unhealthy{connector="<name>"}` is 1 per unhealthy connector, and the unlabelled
`chemclaw_connectors_unhealthy` is the count — and in the WARNING each failed probe logs, which also
carries the reason. An unreachable connector costs its tools for that turn, not the turn itself;
with `CHEMCLAW_CONNECTORS_REQUIRED=true` the front door refuses to start instead
(`ConnectorsUnavailable: connectors_required is set but these connectors are unreachable:
<name> (unreachable), …`). Fix order: is the server up (its own
`/healthz`)? is the URL the one in `CHEMCLAW_CONNECTOR_URLS`? does the NetworkPolicy allow host
*and* port? is the bearer variable set on both sides (a 401 is a missing or mismatched token)? A bundle
reported `unpolled` needs its worker Deployment (`worker: true`). Verify a bundle this repository
serves standalone with `uv run uvicorn chemclaw.connectors.<name>.server.app:app` and check
`/healthz`; tool *discovery* needs no database, but *invoking* a search does.

**What ships today.** The bundles are `molfp` and `rxnfp` (fingerprint search), `calc` (the
semiempirical calculators, their durable searches and the calibration ledger), `bo` (Bayesian
optimization) and `results` (re-queueing stored calculations for an external
results store, §(xvi)) — plus `chem` (bench chemistry over RDKit), `safety` (the
hazard screen) and `rxnpredict` (forward-reaction and reaction-condition prediction by ensemble),
which this release **declares but does not run**: all three are served by
`Chemclaw3-mcp`, so each needs its host in `networkPolicy.egressDestinations`, its port in
`networkPolicy.egressPorts` — an egress rule restricts by port independently of the peer list — and
its bearer (`CHEMCLAW_CHEM_TOKEN`, `CHEMCLAW_SAFETY_TOKEN`, `CHEMCLAW_RXNPREDICT_TOKEN`) provided,
or every call to them is refused. The
physics behind `calc` is served there too — `CHEMCLAW_CALC_SERVER_URL` and `CHEMCLAW_CALC_TOKEN` —
even though the `calc` bundle's own tools, cache and durable jobs stay in this release. **And five more this release declares, does not run, and
does not bind**: `props` (solvent and pure-component properties), `thermalsafety` (runaway and
thermal-hazard arithmetic from measured calorimetry), `kinetics` (isothermal rate and ideal-reactor
arithmetic), `unitops` (scale-up and unit-operation sizing) and `suitability` (USP <621>
chromatographic system suitability). These declare `default_enabled: false`, so an empty
`CHEMCLAW_CONNECTORS_ENABLED` binds none of them and the chart ships all five at `enabled: false`
(`D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`). Their manifests are
here anyway because the declaration validators resolve tool names through them, which lets the
judgment beside each one name the tools it is judgment about. Turning one on is the same three
obligations as the three above — host, port, bearer (`CHEMCLAW_PROPS_TOKEN`,
`CHEMCLAW_THERMALSAFETY_TOKEN`, `CHEMCLAW_KINETICS_TOKEN`, `CHEMCLAW_UNITOPS_TOKEN`,
`CHEMCLAW_SUITABILITY_TOKEN`) — plus a fourth that the other three do not have: it costs prefix on
**every** model call, not only on the calls that use it, so enable the ones a site's chemists
actually ask for rather than the set.

**`chem` is declared here and served elsewhere.** Its capability is `Chemclaw3-mcp`'s
`servers/chem`, so this release renders no Deployment and no Service for it and dials the address
in `connectors.chem.url` instead (D-2026-08-09) — that value and its `networkPolicy.egressPorts`
entry are where the port lives, because it is the sibling release's to choose. Two things that are the operator's,
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
- **The two knowledge writers** (`record_knowledge_note`, `record_confirmed_answer`) — the one
  write path. A connector reaches the graph only by returning a note in a job envelope, for core to
  write; `tests/test_knowledge.py` asserts no bundle holds a second way in.
- **The knowledge-graph reads** (`find_notes`, `expand_note`, `find_knowledge_gaps`, and the
  `gather_evidence` sweep over them). The graph is core's *data layer*, not a capability: dozens of
  core modules import `kg`, so a bundle would move three thin tools and leave every one of those
  imports behind — a zero dependency win and a second read path to one note tree. Re-indexing stays
  in core with it.
- **The development report** — its closure (retrievers, embedding index) is what core keeps for
  `gather_evidence` anyway, so a bundle would isolate nothing (D-115). It still returns the
  connector envelope, so `get_durable_job_status` collects it like any other job. There is no DFT
  run and no HPC tier (`D-2026-08-26-semiempirical-is-the-whole-tier`).

## (iv-b) Add a specialized agent (a **profile**)

A profile is a named override bundle over the one agent: its instructions, the tools it may use, and
whether the plan/execute harness runs. It only ever *narrows* — the audit trail, the per-tool
authorization gate and the skill role gates all run after it — so a profile gives a caller a smaller,
sharper agent, never a wider one.

**To add one:** drop `data/profiles/<name>.yaml` (the directory is `CHEMCLAW_PROFILES_DIR`, a
pathsep list) and restart. The filename is the profile name (a `name:` key inside is refused, so
the two cannot disagree). A profile about a single capability goes in that connector's bundle
instead (`connectors/<name>/profiles/<p>.yaml`, declared in its manifest) so it ships and is
reviewed with the capability. `GET /profiles` lists what this deployment registered.

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
name nothing provides is a startup error, not a silently smaller agent. The other optional fields
are `harness_autonomy`, `effort` (`low`/`medium`/`high`), `model_route` (a key of
`CHEMCLAW_MODEL_ROUTES`, never a model id) and `skill_names`; a misspelled field or value is
refused at load. See `data/profiles/property-lookup.yaml` for a worked example.

**To use one:** `POST /sessions {"profile": "property-lookup"}`. The profile is fixed for the
session's life — a conversation whose tools changed underneath it would have a thread that no longer
matches its own history — and an unknown name is a 400 at session creation. Under
`CHEMCLAW_SESSION_STORE=postgres` the profile is stored beside the session's owner
(`session_owners.profile`), so a session that is rehydrated after a restart or an eviction keeps its
narrowing.

**Profiles are also the helper and peer rosters.** `CHEMCLAW_AGENT_HELPER_ROSTER` (pathsep list,
shipped as `evidence`, `computation`, `safety`) names the profiles the agent may delegate to through
its `task` tool; a helper's surface is further cut to its caller's minus every side-effecting tool.
An unknown name there stops the front door at startup. `CHEMCLAW_AGENT_PEER_ROSTER` (empty, off by
default) names profiles that hand the whole conversation to one another.

## (iv-c) Add a fixed procedure (a **template**)

Reach for this only when the *order* must not vary. A profile is the first answer — it configures an
agent and lets the model choose the sequence, which is what you want while a procedure is still being
figured out. A template pins the sequence and runs it as a durable Temporal job: use it for a
validated protocol, a standard screening sweep, a report that must always gather the same evidence in
the same order. `src/chemclaw/templates/README.md` has the full comparison and the field reference.

**To add one:** drop `data/templates/<name>.yaml` (`CHEMCLAW_TEMPLATES_DIR`, a pathsep list). The
filename is the name here too.

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

An `agent` step may also name a `profile` to run under. Run `make template-validate` (CI does): it
checks that every step names a tool, job or profile that actually exists, so a pinned procedure
cannot fail on step four in production. It cannot check the arguments of a tool served by a
bundle this repository does not run; `make live-template-args` checks those against the running
connector servers (needs `make live-up`).

**To use one:** the template becomes a generated `run_<name>` tool the model can call like any
durable job — same authorization gate, same audit trail, same dry-run behaviour. It returns a job id;
poll with `get_durable_job_status`. Re-running with identical inputs returns the existing id rather
than paying twice.

**Editing one is safe.** A run pins the resolved template into its workflow input, so an edit cannot
change a run already in flight and there is no migration; the change applies to later runs only.

`CHEMCLAW_TEMPLATES_ENABLED` narrows which discovered templates are advertised (empty = all);
`CHEMCLAW_TEMPLATE_STEP_TIMEOUT_SECONDS` bounds one step and `CHEMCLAW_TEMPLATE_RUN_TIMEOUT_SECONDS`
the whole run (startup refuses a run ceiling that cannot contain one step). A run is a Temporal
workflow on `background-jobs`, so a stuck one is read in the Temporal UI like any other job.

## (v) Re-ingest a rejected ELN entry (after fixing the source record)

The durable sync rejects an entry that fails validation (bad structure, mass-balance
mismatch) and **advances past it** — a rejection is deterministic bad data, so re-fetching
it unchanged would only re-reject it. Each rejection is counted in the workflow result
(`rejected`, visible in the Temporal UI) and logged by the background worker as `eln sync rejected entry <id> (at <timestamp>): <reason>`. The one exception is an entry
stamped further in the future than `CHEMCLAW_ELN_SYNC_FUTURE_TOLERANCE_SECONDS`: it is rejected
*without* advancing the cursor, so a typo'd year cannot skip every later entry — fix its timestamp
at the source and the next scheduled run picks it up.

To re-ingest one after correcting its source record upstream, start a one-off `ElnSyncWorkflow`
with `since` set to just before that entry's timestamp. With the Temporal CLI (from a host or
pod that reaches the broker, with the same namespace and TLS flags as the workers):

```sh
temporal workflow start --task-queue background-jobs --type ElnSyncWorkflow \
  --workflow-id eln-resync-$(date +%s) --input '"2026-09-30T00:00:00+00:00"'
```

or the Temporal UI's *Start Workflow* with the same three values. A manual `since` re-syncs
**every** active ingest source from that point and leaves the stored cursors alone. Ingestion is
idempotent (id-keyed upserts throughout), so entries already held unchanged in that window are
counted as `skipped_existing` and only the corrected entry newly succeeds. There is no automatic
re-drive by design — re-ingestion is a deliberate, admin-triggered action. Verify that no
`rejected entry <id>` line recurs for it in the background worker's log during the run, and that
the workflow result's `ingested` count moved.

The **same procedure backfills a late-arriving export**: a file dropped into the export directory
after the sync's overlap window (`CHEMCLAW_ELN_SYNC_OVERLAP_SECONDS`), carrying an older payload
timestamp, is filtered out permanently and reported as `… export file(s) arrived after the sync
cursor but carry an older timestamp …`. Start the sync with `since` set before those entries'
timestamps to pull them in.

**Reading the result.** The workflow returns counters (per-entry ids are in the log): `ingested`
is entries this run newly indexed and stored (queryable the moment the write returns — there is no
review step); `citation_only` is the subset stored without a structure, so citable but in no
similarity index; `skipped_existing` is entries already held byte-identical, which the overlap
window re-fetches every run; `rejected` is above; `failed_sources` names a source whose sync
failed outright. A steady `ingested` count means the source is producing new data.

## (vi) Change a fingerprint definition (ECFP radius/bits or DRFP bits)

`CHEMCLAW_ECFP_RADIUS`/`_ECFP_BITS`/`_DRFP_BITS` define the fingerprints (defaults 2 / 2048 /
2048). A **width** change (`*_BITS`) also needs a new migration altering every `bit(2048)` column
it touches (`infra/sql/002`, `003`, `054`, `071`) or inserts fail loudly. Every fingerprint row
records the *definition* it was indexed under, and similarity search returns only rows matching
the store's current definition — so after any definition change, previously-indexed rows fall out
of search (safe: no wrong scores, just missing hits) until you **re-index** them.

**The tell.** The `molfp`/`rxnfp` servers log at startup `… fingerprint index is EMPTY: 0 records
indexed under the current definition …` or `… is PARTIAL: N record(s) indexed under the current
definition and M under a superseded one …`, and every similarity search answers with
`index_partial: true` (or says it could not be answered) until the rebuild finishes.

**Re-index**, once every pod runs the new setting. `make rekey-compounds` (§(vi-a)) rebuilds every
row stored under a definition other than the current one from the label the row already holds, so
it serves a radius or width change as well as a standardization bump: preview with
`make rekey-compounds`, write with `APPLY=1`, then dispose of the superseded generation with
`APPLY=1 DISPOSE=1` (as the schema owner). Rows it reports it could not rebuild need their source
re-synced: a one-off `ElnSyncWorkflow` with `since` at the epoch (`"1970-01-01T00:00:00+00:00"`,
the command in §(v)). Afterwards the PARTIAL line and `index_partial: true` are gone.

## (vi-a) After an upgrade that bumps `STANDARDIZATION_VERSION`

A bump changes the fingerprint definitions (the version is a token in both), so every row indexed
under the old one falls out of similarity search — and when it changes what a structure
standardizes to, it moves that compound to a new `compound_id`, leaving the old compound note
current beside the new one. **Run `make rekey-compounds` once after upgrading past a bump**: it
previews the per-kind counts; `make rekey-compounds APPLY=1` writes them. It supersedes each moved
compound note by the note under its new id (the old one is retired, never deleted, and its id keeps
resolving through `expand_note`), and re-fingerprints the shelved rows of both indexes from what
each row stores, so the full ELN re-sync below is no longer needed for a bump. It is idempotent: a
second run reports nothing left to write. Run it where the note writer runs — it commits through
the same dedicated checkout (`D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned`).
In a cluster that is the background worker, whose image has no `make`:
`kubectl exec deploy/chemclaw-background-worker -- python -m chemclaw.cli.rekey_compounds`
(add `--apply`, and `--dispose-superseded` for the step below).

**The re-key adds the rebuilt generation and keeps the old one**, so on its own it leaves every
similarity search reporting `index_partial: true`: since `infra/sql/094` the definition is part of
a fingerprint row's key, `index_partial` reads the oldest and newest definitions in the table, and
the runtime role holds no `DELETE` there. **Dispose of the superseded generation with
`make rekey-compounds APPLY=1 DISPOSE=1`** (`--dispose-superseded`), once every pod runs the new
image — a pod still on the old one keeps writing rows under the definition this deletes. Per index,
it deletes the rows stored under any other definition **only when that index's rebuild was
complete**: a shelved row whose label no longer parses has no current twin, so the shelf is kept,
the line says how many rows could not be rebuilt, and searches go on saying PARTIAL until those
entries are re-synced from source. It is the operator statement `094`'s header names, so it runs as
the schema owner — `CHEMCLAW_POSTGRES_MIGRATION_DSN` when the principal is split (see "Splitting the
database principal"), the one credential otherwise — and refuses without `APPLY=1`. The chart
mounts the migration DSN on the migrate Job only, so for an in-cluster disposal pass it to the one
command (`kubectl exec … -- env CHEMCLAW_POSTGRES_MIGRATION_DSN='…' python -m
chemclaw.cli.rekey_compounds --apply --dispose-superseded`). Idempotent: a second run finds nothing
superseded. Afterwards `similar_reactions` and `similar_molecules` answer
with `index_partial: false`.

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
ordinary incremental `make reindex` — and the `note-reindex` Schedule, which exists whenever
`vector` or `lexical` is in `CHEMCLAW_DATA_SOURCES` (every `CHEMCLAW_NOTE_REINDEX_SCHEDULE_MINUTES`,
hourly by default) — re-embed exactly the rows a swap superseded. Nothing to remember and no flag
to pass. What that does mean is that the *first* run
after upgrading past this re-embeds the whole corpus once: every existing row has no key recorded,
which reads as unknown, and unknown is never treated as current. Same for `document_chunks`, whose
keys all change because the key now names the endpoint as well as the model.

**A share's `chunk_chars` / `chunk_overlap_chars` now take effect, and they are not free.** The
same migration set records which chunking cut each row (040), and both of the crawl's gates compare
it — so changing either number re-reads and re-cuts every document of that share, off the mount,
over the crawl's ordinary bounded passes. The first sync after the upgrade pays it once for the
same reason as above:
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
`CHEMCLAW_DOCUMENT_SYNC_MAX_ITERATIONS` times per run, one run every
`CHEMCLAW_DOCUMENT_SYNC_SCHEDULE_MINUTES` (six-hourly by default) — multiply the three for your
share: a share of a million chunks takes **days to weeks** at the shipped values. Throughout it,
document search compares queries embedded by the new model against vectors not yet refreshed —
scores are degraded, results are not missing. Watch `re-embedded N chunk(s) under <key>` in the
background worker's log to see the drain converge, and `N chunk(s) could not be re-embedded …` at
ERROR for the ones it cannot fix. To finish faster, raise the batch size or run
`make share-sync SHARE=<name>` (`python -m chemclaw.cli.sync_share <name>`), which drains that
share's re-embed to completion before it crawls.

## (vii) Read eval-drift alerts

The scheduled `EvalDriftWorkflow` re-scores the committed eval case-set and raises one alert per
metric that moved past the relative noise band (`CHEMCLAW_EVAL_DRIFT_EPSILON`, default 0.05 of the
baseline value). **It is off unless you turn it on**: set `CHEMCLAW_EVAL_DRIFT_ENABLED=true`
(neither the code nor the chart does), and the `eval-drift` Schedule is created by the next
`make schedules-apply` / chart upgrade, firing every `CHEMCLAW_EVAL_DRIFT_SCHEDULE_MINUTES` (daily
by default). Two surfaces, both intentional:

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
metric change, refresh the committed baseline with `make eval-baseline` (it rewrites
`data/evals/baseline.json`; commit it through review) — otherwise every scheduled run re-alerts.

**You do not need the workflow (or a broker) to get this reading.** `make eval-baseline-check` runs
the same comparison offline, prints every metric's baseline/current/delta/band, and exits non-zero
only on a move in the *worsening* direction — so it is the one to run before refreshing the
baseline, and the one that answers "did anything get worse?" on a laptop. It declares the case-set
version it scored (the Makefile's `EVAL_CASE_SET_VERSION`) and refuses to report a number when that differs from
the baseline's: aggregates over two different case-sets are different quantities.

## (viii) Answer "is prompt caching paying off?"

The system prompt is large and largely identical turn to turn, so "cache the static prefix" is a
standing cost-saving proposal (REV-9). **Measure before building it** — the metric that answers the
question already exists, and the answer decides whether there is anything to build.

Scrape `/metrics` and read the four spend counters together:

```
chemclaw_input_tokens_total       # fresh prompt tokens, full price
chemclaw_cache_read_tokens_total  # prompt tokens served from the gateway's cache, ~10x cheaper
chemclaw_cache_write_tokens_total # tokens written to the cache, priced above a fresh input token
chemclaw_output_tokens_total      # completion tokens, unaffected by any of this
```

The ratio `cache_read / (cache_read + input)` is the cache hit rate on the prompt side. Each outcome
implies a different action:

| Reading | What it means | What to do |
| --- | --- | --- |
| `cache_read` is a large fraction of prompt spend | The gateway is already caching the prefix without being asked | Nothing. The saving is banked; a `cache_control` mechanism would add code for a benefit you already have. |
| `cache_read` ≈ 0 and `input` is large | The prefix is being re-billed every turn | There is a real saving to chase — see the caveats below before estimating it. |
| `cache_write` grows while `cache_read` stays flat | The cache is being paid for and never used | Sessions are too short or too spread out to hit it; shortening the prefix beats caching it. |

**Expect `cache_write` to read a flat 0**: an OpenAI-compatible endpoint caches implicitly and
reports reads, and only some report a write count at all, so a zero there is the normal reading
rather than a fault. `cache_read` is the number that says whether caching is happening.

**All four counters read zero if the gateway never reports usage.** They come from the usage
chunk the endpoint sends at the end of a stream, which is requested with
`CHEMCLAW_LLM_STREAM_USAGE=true` (the default). An endpoint that rejects `stream_options` needs it
off — and then every turn meters zero, the budget guards included. Check that
`chemclaw_input_tokens_total` moves on the first real turn.

**There is no `cache_control` to switch on.** Every model call goes to one OpenAI-compatible
gateway (`D-2026-09-04-a-gateway-is-the-only-provider`), and `cache_control` breakpoints are a
vendor spelling that client does not send. Whether a prefix is cached is entirely the gateway's
decision, and these counters are how you find out.

Three caveats that make the saving smaller than a naive prefix measurement suggests:

- **Measure the deployment you actually run.** The prefix size depends on the bound tools, skills
  and profile; `chemclaw_connector_tool_schema_tokens{connector=…}` shows each connector's share.
- **The system half is not cacheable as the prompt is assembled.** `deepagents.SkillsMiddleware`
  renders the skills manifest into a string with `system_prompt_template.format(...)` and appends
  it to the system message, so the half that changes least is welded to the half that changes most.
  Marking it cacheable needs a change upstream, not in Chemclaw — the same conclusion the previous
  framework's `SkillsProvider` f-string forced, reached again for the same structural reason.
- **Measure a session that is inside its context budget.** Above
  `CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET` — which is a budget on the whole request, the system
  prefix and every bound tool schema included, not on the thread alone —
  `agent/compaction.py` rewrites the *front* of the message list on every model call — clearing older tool results, then dropping the oldest conversation
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
distinguishable — and that distinction is the whole reason these exist.

| Reading | What it means | What to do |
| --- | --- | --- |
| flat zero | No session in this process has crossed the budget | Nothing. This is the healthy default, and it is also the state in which the cache table above is meaningful. |
| the line is absent from `/metrics` | You are not scraping this process | Not a compaction signal at all: `core/metrics.py` pre-seeds every declared counter, so both names render at `0` from the first scrape of a process that has served nothing. An absent line means the worker's `/metrics` port is unscraped (`CHEMCLAW_WORKER_METRICS_PORT`), not that the policy is unwired. |
| rising steadily, `reclaimed` large per compaction | Long sessions are routinely over budget | Expected on a deployment with real chemists. Read it against `chemclaw_turn_duration_seconds`: reduction is cheap (sub-millisecond to ~6 ms per call), so a slow turn is not this. |
| rising on almost every call | The budget is below this deployment's normal turn | Raise `CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET` toward the model's real context window. Compacting a thread that would have fit spends estimator passes and drops context for nothing. |
| rising on **every** call, from the first one | A configured trigger is below this request's own prefix, so it floors at 1 — "reduce on every model call" | Grep the process for `context.trigger_floored`, a WARNING naming the setting, its value and the measured prefix. Both context settings are budgets on the whole *request*: the system message, the skills listing and every bound tool schema come off them before the thread gets anything, and that prefix is bounded by `tests/test_context_floor.PREFIX_BOUND` — the ratchet's ceiling for the surface this repository serves plus the allowance for the bundles served out of `Chemclaw3-mcp`. The shipped `CHEMCLAW_AGENT_TOOL_RESULT_CLEAR_TRIGGER` is derived from that bound plus a thread allowance, so it is above the prefix and a shipped deployment is **not** in this state: this row means someone lowered it. Raise it back above the bound (and keep it at or below the budget, which startup enforces). The live figures are whatever `tests/test_context_floor.py` and `tests/test_compaction.py` measure — they move with every bound tool schema, so do not copy one out of this row. |

Per-model attribution for the same spend **is not on this surface**. "Which model, how many
tokens" is a trace query — `CHEMCLAW_OTEL_LLM_SPANS=true` puts one span per model call in the trace
pipeline carrying `llm.token_count.*` and `llm.model_name` (see "Logging & troubleshooting") — or a
SQL one over the `turn_costs` ledger, which records each turn's `model`, tokens and outcome. "What
is this deployment spending per hour" is these counters, which carry `profile` rather than model,
deliberately (D-152).

## (ix) Where agent-authored knowledge goes now

**There is no review queue, and that is the current design rather than a gap.**
`D-2026-09-05-the-gate-is-deleted-not-dormant` deleted the PR-gate: an agent-authored note is
written straight into the notes checkout, committed on the base branch and pushed, so it is
readable by every reader the moment its bytes land. There is no note-review route and no
post-merge webhook; remove any leftover `CHEMCLAW_PROPOSAL_*` variables or webhook secret from old
configuration. (Today's `GET /proposals` is a different thing: each chemist's own queue of
*skill* proposals, §(i).)

**What it needs.** A dedicated notes clone with push rights — `CHEMCLAW_NOTE_REPO_DIR` and the
five requirements under "Exposing the front door"; in the chart, `knowledge.sync.repoUrl` and the
`CHEMCLAW_KNOWLEDGE_REPO_TOKEN` key in the release Secret, without which every write fails at push.

**What to watch instead.** `chemclaw_notes_recorded_total` counts notes that reached the graph and
`chemclaw_notes_publish_failures_total` counts those that could not — the ratio is the health
signal, and a flat recorded count with a rising failure count is a git credential or a wedged
notes checkout, not an idle system. `chemclaw_fan_out_children_dropped_total` covers the memory
fan-out, which does not go through the best-effort publisher.

**What an operator does when a note is wrong.** Nothing at write time; there is no queue to hold it
in. The controls are downstream and a chemist reaches all three from the conversation: the note
carries `created_by: agent` on every retrieved chunk, `record_failure` writes a `contradicts` edge
that `kg/conflicts.py` surfaces, and a newer finding supersedes it with the old one's window closed.
A note whose *file* must go is an ordinary commit in the notes repo by a human with push rights.

**If a write fails.** The note is on disk in the checkout and uncommitted or unpushed — the writer
fast-forwards and retries on the next write, so the usual repair is to fix the remote or the
credential and let the next note carry it. `git -C "$CHEMCLAW_NOTE_REPO_DIR" status` (run in the pod whose
write failed — its log names the error) is what tells you which state you are in, and the checkout must be on the
base branch or the writer refuses.

## (ix-b) Who may change an experiment design

The protocol surface (`/protocols`) has its own authorization rule. A design is a chemist's own
experiment rather than shared knowledge, so the author is not excluded from deciding — they approve
their own plate.

- **Reads are open to any authenticated caller.** `GET /protocols`, `GET /protocols/{id}`,
  `GET /protocols/{id}/diff` and `GET /protocols/{id}/run-sheet.csv` take no ownership check at
  all. A design is a shared scientific artifact: the schema
  keeps `opened_by` through offboarding and the listing serves the deployment's designs, so the
  id's existence is not the secret.
- **Writes need the owner *or* a reviewer.** `POST /protocols/{id}/revisions` and
  `POST /protocols/{id}/status` both take `owner_permits(header.opened_by, actor)` **or** a role in
  `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`. The same rule for both, on purpose: a reviewer who may sign
  off on somebody's plate may also correct it, and every revision is append-only and carries its
  own `author`, so a reviewer's edit is attributed rather than silent. Nothing here is a two-person
  rule — if you want one, that is a decision to take in an ADR, not a setting.
- **A refused write answers 403, not 404**, because reads are open and only the right to change is
  withheld. It is recorded either way: `chemclaw_authz_refusals_total{resource="design"}` and a
  WARNING carrying `resource`, `reason`, `actor`, the clipped `target` and the `status` the caller
  was told. That series is what tells a scan of design ids from ordinary traffic.
- **With `CHEMCLAW_ENTRA_REQUIRED=false` (dev) ownership degrades open**, exactly as every other
  surface does — there is no real actor to own anything. Do not read a passing dev write as
  evidence about the enforced posture; `tests/test_protocol_authorization.py` drives both.

The agent-side half is the same rule in the same shape: `structure_experiment_request` and
`draft_experiment_protocol` refuse a `design_id` belonging to another chemist through
`owner_permits`, which is the one ownership predicate the session routes and the evidence tools
also read.

## (x) Find out what a worker is doing (or why it stopped)

Every worker serves three routes on `CHEMCLAW_WORKER_METRICS_PORT` (default 9000, the `metrics`
container port) — `/healthz`, `/readyz` and `/metrics`
(`D-2026-08-01-every-process-carries-its-own-witness`). The worker Deployments are
`chemclaw-background-worker`, `chemclaw-connector-worker-<bundle>` and
`chemclaw-interactive-worker-<bundle>`:

```
kubectl port-forward deploy/chemclaw-background-worker 9000:9000
curl -s localhost:9000/readyz    # 200 {"status":"ready",…} = running AND has heard from the broker
curl -s localhost:9000/metrics   # this pod's counters, gauges and histograms
kubectl logs deploy/chemclaw-background-worker | grep 'connected:'   # what it connected to
```

- **`/readyz` answers for the broker too.** The predicate is `worker_ready` (`durable/serve.py`):
  `worker.is_running` **and** `broker_seen_recently()`, which reads the freshness of the
  in-flight-jobs poll every worker runs and goes stale after `CHEMCLAW_JOBS_IN_FLIGHT_REFRESH_SECONDS`
  × 3 — 90 s on the shipped default. So a worker cut off from Temporal for longer than that reports
  503, and a rollout cannot complete through a broker outage. What the probe cannot tell you is
  *which* side broke, so during a suspected incident probe the broker before restarting anything.
  A worker that cannot reach the broker at startup exits 1 and crash-loops.
- **`/readyz` is 503 but the pod is up.** Either half of the predicate: the worker object is not
  running (a shutdown that has begun, or a failure before the poll loop started), or it is running
  and has heard nothing from the broker inside the staleness window. `chemclaw_jobs_in_flight`'s
  own staleness and `chemclaw_degraded_total{subsystem="jobs_in_flight"}` on the same pod separate
  the two. It is deliberately *not* a liveness signal: restarting on it would turn an ordinary
  reconnect into a crash loop, so the pod stays and reports honestly.
- **`/healthz` stops answering.** The event loop is wedged, almost always by a blocking call inside
  an activity, and the kubelet restarts the pod after `failureThreshold` (2 minutes by default —
  generous, because a false restart mid-job costs more than a slow true one). The metric to read
  after the restart is `chemclaw_tool_duration_seconds` on the pod that replaced it.
- **The counters read zero on a busy worker.** They are per-process, so you are scraping the wrong
  pod: a durable job launched from the front door increments the front door's registry, and the
  same job's *activity* increments the worker's. Scrape both before concluding a number is missing.

Two monitors collect all of this in-cluster: `servicemonitor.yaml` for anything with a Service (the
front door, each connector's MCP server) and `podmonitor.yaml` for the workers, which have none.

**`monitoring.additionalLabels` is not what decides whether they are read on OpenShift**:
user-workload monitoring selects **every** ServiceMonitor and PodMonitor in every user namespace,
with no label selector at all, so the shipped empty default is correct there. It matters for a
**self-managed Prometheus Operator**, whose `Prometheus` resource carries a
`serviceMonitorSelector`/`podMonitorSelector` that these labels have to match.

What *does* decide it on OpenShift is a cluster-wide switch that is off by default: see
§ "Make the monitoring stack actually collect this" below. If a target is `down` rather than absent,
check `networkPolicy.monitoringNamespaces`: that is the list granting the scraper ingress to the
connector port and the worker probe port.

## (x-b) Make the monitoring stack actually collect this

**Do this before believing anything above.** The chart ships a ServiceMonitor, a PodMonitor and a
PrometheusRule (`deploy/helm/chemclaw/templates/prometheusrule.yaml` is the roster of alerts, each
with a `runbook_url` into §(x-c) below); on a stock OpenShift cluster **all three are inert custom
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
# and, from the console: Observe -> Targets, filtered to the release namespace;
# Observe -> Alerting -> Alerting rules, filtered by "Chemclaw", shows the rules loaded
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

**What the five cover** (sources in `deploy/helm/chemclaw/dashboards/`). `Chemclaw turns` (rate,
how turns end, duration percentiles, tokens by profile, in-flight against capacity, the per-actor
cap), `Chemclaw tools and model` (p95 **by tool**, outcomes and refusals by reason, model calls by
provider, context management and the judge), `Chemclaw durable jobs` (jobs in flight, success
ratio, p95 by connector, activity failures, dropped push-backs, queued tool calls),
`Chemclaw front door` (per-route rate, error ratio and p95, event streams) and `Chemclaw data and
storage` (cache hit ratio, the Postgres pool, connector reachability, evidence per source, the
result outbox, ingest lag, knowledge notes written). Between them every metric this system declares
has either a panel or an alert, and
`tests/test_deploy_chart.py::test_every_declared_metric_has_a_consumer` is what keeps that true.

**The Temporal SDK's own metrics are off by default.** `monitoring.temporalSdkMetrics.enabled=true`
sets `CHEMCLAW_TEMPORAL_METRICS_PORT` on every worker (`monitoring.temporalSdkMetrics.port`, default
9001), so the SDK's Prometheus exporter binds there, and renders the matching container port,
`podMetricsEndpoint` and NetworkPolicy ingress — task-slot occupancy, poller counts,
schedule-to-start latency, the queue-side numbers no first-party counter can produce. It is also
what renders `ChemclawWorkerNotPolling`, which reads them. If the exporter cannot bind (the port is
taken), the worker runs without it and counts
`chemclaw_degraded_total{subsystem="temporal_sdk_metrics"}`, and the target shows `Down`.

## (x-c) When an alert fires

Every rule carries a `runbook_url` pointing at its heading below, so an alert arrives with its own
entry attached. The rules' own `description` annotations say what happened and are not repeated
here; what follows is what to *do*, and what the alert does not mean.

Two things to know before reading any of them:

- **Every alert is per-fleet, and almost every metric is per-process.** A counter reads zero on a
  pod that is not the one doing the work: a durable job launched from the front door increments the
  front door's registry and its *activity* increments the worker's. Scrape both before concluding a
  number is missing.
- **Only `ChemclawTargetDown`, `ChemclawNoWorkerIsScraped` and `ChemclawNoBackgroundWorkerIsScraped` fire for a process that is gone.**
  Everything else reads an application counter, and a process that is not running emits no counters
  — which looks exactly like a healthy quiet system.

### chemclaw.records — a durable record is being lost

#### ChemclawAuditTrailIncomplete
`critical`. Tool calls keep succeeding while the trail of who ran them does not. Find the
`audit_sink_failure` marker in the front door's log; it is almost always the database. The rows
already lost are not recoverable — `durable/retention.py` refuses to prune this table for the same
reason this is critical.

#### ChemclawAuditTrailShedding
`critical`. The same hole from the other cause, and the distinction is the whole reason it is a
separate alert: `ChemclawAuditTrailIncomplete` means the database **refused** a batch, this one
means it could not **keep up** with the producer. The write buffer reached
`CHEMCLAW_AGENT_AUDIT_BUFFER_MAX_EVENTS` and shed its oldest records to bound the front door's
memory (`resources.service.limits.memory` in the chart).

So do not start with connectivity — the database is reachable by construction here. Look at write
latency and pool saturation (`chemclaw_pg_pool_available`, `chemclaw_pg_pool_requests_waiting`,
`chemclaw_db_query_duration_seconds`), and at whether something else on that database is holding
locks. The `audit_buffer_full` log marker names how many rows went and at what bound.

This arm has **no exception to log**, which is why pooling it with the alert above would be a trap:
an operator following that runbook would grep for `audit_sink_failure`, find nothing, and conclude
the alert was wrong. Every shed event still reached the stdlib log, so the record is recoverable
from there even though the queryable trail has a gap. Raising the bound is a memory decision, not a
fix; the fix is the write latency.

#### ChemclawDeliveryChannelFailing
`warning`. An outbound channel is refusing messages, and the digest that could not be sent was
**still acknowledged** — the watermark turns on the in-app mailbox, deliberately, so nothing
retries this. Recipients on that channel are simply not being reached, and the notes it covered
will not re-qualify. The `channel` label names the folder; the `deliver.channel_failed` log marker
carries the driver and the error. Check the destination first (a rotated webhook secret and a
retired URL are the two common ones), then `CHEMCLAW_DELIVERY_CHANNELS` against the folders on
`CHEMCLAW_DELIVERY_CHANNELS_DIR`.

This counter and `chemclaw_deliveries_total` are the pair worth reading together: one channel at
zero while another climbs is a broken destination, and both at zero with delivery configured is an
outage of the seam itself.

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
processes sharing one `note_repo_dir`. §(ix) says where an agent-authored note goes; the notes lost
here never got there.

**Two of those causes are a *hard kill*, not a misconfiguration, and they wedge the pod** — every
later note write on it fails until something reconciles the checkout
(`D-2026-09-06-a-rollback-a-sigkill-skips-is-not-a-rollback`). A pod evicted between `git add` and
`git commit` leaves the note staged and uncommitted, and once the remote moves on those paths the
fast-forward refuses with no commits to replay; a kill during the commit itself can leave git's own
index.lock behind inside the checkout's .git directory. Both recover on their own, and both say so: look for
`kg.write.dead_write_residue_discarded` and `kg.write.stale_index_lock_cleared` at WARNING. Seeing
either once is the recovery working. Seeing one repeatedly is a pod being killed mid-write, which is
a restart-loop question rather than a git one — check the worker's terminations, not the remote.

#### ChemclawKnowledgeCorpusStale
`warning`, and **only rendered when `monitoring.alerts.knowledgeCorpusStaleSeconds` is non-zero** —
the chart ships it at 0 because the threshold is how often *your* chemists merge notes. The same
failure as the alert above in the other direction: notes are written and stop reaching the other
pods. `knowledge-sync.sh`'s `loop` swallows a failed refresh on purpose (a dead remote must not kill
the pod), so the pod serves the frozen snapshot indefinitely and every answer keeps citing it.
`chemclaw_knowledge_sync_age_seconds` is the age of the newest note on that pod's tree; **-1 means
the tree holds no note at all**, which is a volume that was never populated rather than a stale one.
That sentinel fires this same alert — the rule is `age > threshold or age < 0`, because -1 is never
greater than a positive threshold and the comparison alone could not reach the case this paragraph
calls the sharper one. Both arms are behind the one opt-in, so a site that does not use the
knowledge graph is not paged for an empty tree it meant to have. On a release with
`knowledge.sync.enabled` a -1 is never a pod that is merely still starting: the `knowledge-sync-init`
init container fills the tree before the app container starts, and fails the pod rather than let it
serve a half-published one — so -1 there means the publish produced nothing. Read
`kubectl logs <pod> -c knowledge-sync-init` first in that case.

Read the sync sidecar's log and its restart count (its `staleness` liveness probe is the sync-side
half of this signal) before concluding the corpus is merely quiet — this gauge cannot tell those
two apart, which is why its threshold is yours to state.

The gauge's underlying stat scan is cached for `CHEMCLAW_KNOWLEDGE_AGE_SCAN_TTL_SECONDS` (300 s),
so a reading can lag a *refresh* by up to that window. It cannot lag a **freeze**: the age is
recomputed from the clock on every scrape against the cached mtime, so a corpus that stopped
arriving keeps counting up in real time. The window only ever makes the number too large, never too
small.

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
`critical`, and only above `monitoring.alerts.turnsFloorPerSecond` of traffic — a ratio alone reads
100% on a single error in an idle window, which is what this rule did before the floor existed. If
the deployment is quieter than the floor, read `chemclaw_turns_finished_total` directly rather than
waiting for a page that cannot come. More than one turn in ten ends in an error event.
Break it down with
`sum by (outcome) (rate(chemclaw_turns_finished_total[10m]))` — `errored` and `timed_out` are
different problems — then the front-door dashboard's per-route error ratio. **If every turn is
failing at once, check the model gateway's credential first**: a 401/403 from
`CHEMCLAW_LLM_BASE_URL` ends every turn `errored` with SSE code `llm_auth`, shows as
`chemclaw_model_calls_total{outcome="auth"}`, and logs ERROR `model.gateway_refused_credential`
naming the gateway host and status — rotate `CHEMCLAW_LLM_API_KEY`
([troubleshooting §8](troubleshooting.md#8-model-gateway-errors)).

#### ChemclawTurnRelayFailing
`warning`. A pod holding running turns cannot read the follows and Stops other replicas address to
them (`chemclaw_turn_relay_poll_failures_total`; its WARNING `could not read the requests other
replicas addressed to this process's turns` carries the exception). Its own chemists see nothing
wrong; a chemist whose request the Service balanced to another pod gets a 503 on Stop and a reattach
that never opens until the turn ends. It is that pod's database path — read `ChemclawPgPoolSaturated`
beside it (D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica).

#### ChemclawFleetAboveItsTurnCeiling
`warning`. More front-door pods are running than the declared fleet ceiling accounts for, so the
shared LLM endpoint can be offered more concurrent turns than its budget permits. A manual scale, an
HPA edited in the cluster, or a rollout that left both generations up. Scale back, or raise
`CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` to a number the endpoint can actually serve.

#### ChemclawTurnsShed
`warning`. The admission guard is declining load. **A shed is an HTTP 200**, not a 503 — the turn
route streams, so the status line is written before a permit is asked for and the refusal arrives as
an SSE `{"type":"error","code":"at_capacity","retryable":true}` frame. Availability monitoring and
any uptime probe read it as a success, which is why this counter is the only signal there is. Either
the deployment is undersized or `service_max_concurrent_turns` is below what the endpoint serves.
Check `ChemclawTurnLatencyHigh` first: slow turns hold permits, so latency usually precedes shedding
rather than following it.

#### ChemclawTurnsShedHeavily
`critical`. More than a fifth of offered turns (shed + started) are being refused. Measured in the
load lane at offered concurrency 32: 33 of 48 turns shed while the pod sat at 35% of one core,
because a turn is ~93% waiting on the model. So do **not** read a low CPU graph as headroom here.
Two questions, in order: (1) is the fleet scaling — `kubectl -n <ns> describe hpa chemclaw-service`; if it
reports `FailedGetPodsMetric` for `chemclaw_turns_in_flight`, the custom-metrics API (prometheus
-adapter or equivalent) is not publishing that series and the HPA has fallen back to a CPU signal
that cannot reach its threshold, so it will sit at `minReplicas` forever. (2) if it *is* at
`maxReplicas`, the fleet is genuinely at its ceiling: raise
`service.autoscaling.maxReplicas` and `CHEMCLAW_SERVICE_FLEET_MAX_CONCURRENT_TURNS` together, and
only to a number the shared LLM endpoint's throughput budget supports.

#### ChemclawCalculationBackendRefusingForCapacity
`warning`. `servers/calc` has been refusing more than one call every twenty seconds for half an
hour. One refusal is the gate working — the backend sheds promptly instead of queueing a
calculation the caller will have abandoned, and since
`D-2026-09-05-a-refusal-for-capacity-is-not-a-refusal-of-the-question` the caller retries it with
backoff rather than failing the job outright. A sustained rate is different: it is the calculation
tier asking for capacity, and it is the **only** signal that asks.

1. **Check what is holding the slots.** A CREST conformer search is charged every slot on its pod
   (a slot is a core, and the search is given four threads), so one search occupies a whole pod for
   as long as it runs — hours, legitimately. `chemclaw_calc_backend_at_capacity_total` split by
   `tool` says whether the refusals are behind searches or behind ordinary optimisations.
2. **Add replicas, do not raise the per-pod ceiling.** Over-admitting turns prompt refusals into
   slow ones: the pod cannot run more calculations than it has cores, so a higher ceiling only
   converts a clear refusal into a queue nobody bounded.
3. **If the rate is near-constant rather than bursty**, the tier is simply under-provisioned for
   the user count. The review that produced this alert measured the shipped single replica
   saturating at roughly 15–20 concurrent chemists.

This alert is deliberately **not** on `chemclaw_degraded_total`. A busy backend and a dark backend
need different responses, and folding them into one series is how the signal an operator trusts for
"the backend is down" stops meaning that.

#### ChemclawQueuedToolsGoingDirect
`warning`. A tool a manifest lists under `queued:` could not start its `QueuedToolWorkflow`, so the
front door called the server directly (`connectors/queued.py`,
`D-2026-09-30-a-heavy-tool-call-waits-in-a-queue-rather-than-being-refused`). The fallback is what
keeps the capability up through a broker outage; while it lasts, a burst is refused pod by pod
again, exactly as before queues existed.

1. **Check the broker from the front door.** `chemclaw_queued_tool_calls_direct_total` rising
   together with the durable job tools failing is the Temporal frontend being unreachable or its
   mTLS material unreadable — the same fault every durable tool reports.
2. **If the broker is fine, check the interactive workers exist.** A run that *starts* but is never
   picked up does not trip this alert; it waits, and the turn hands back a job id. Look for
   `chemclaw-interactive-worker-<connector>` Deployments with ready pods
   (`kubectl -n <ns> get deploy | grep interactive-worker`).

#### ChemclawFrontDoorAtItsPermitCeiling
`warning`, and the leading indicator for both of the above. `sum(chemclaw_turns_in_flight) /
sum(chemclaw_turn_capacity)` has been over 0.9 for fifteen minutes: the next turn is about to be
shed. If the replica count is not rising, go to question (1) under `ChemclawTurnsShedHeavily` —
this alert existing and the fleet not growing is exactly the autoscaler-blind case.

#### ChemclawCalcBackendOverCommitted
`warning`. More calculation-backend sessions are held across the fleet than
`CHEMCLAW_CALC_BACKEND_MAX_CONCURRENT_REQUESTS` says that pod will serve. It pins
`OMP_NUM_THREADS=1` and is CPU-bound, so the surplus arrives as thrashing, then as activity
heartbeat timeouts, then as retries onto the same pod — which is why this fires before anything
fails. `sum by (pod) (chemclaw_calc_requests_in_flight)` says who is dispatching: a scaled `calc`
worker (`replicas × CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES`) or interactive tool traffic on that
bundle's own server pods, which have no per-process cap at all. Either scale back, or raise the
ceiling to what the server actually admits — `Settings` checks only the durable product, once, at
startup, so it cannot see the interactive half. Silent until a release declares a ceiling: the
gauge is 0 by default.

#### ChemclawConnectorsUnhealthy
`warning`. Turns are being answered without those capabilities and **nothing in the answer says so**.
`chemclaw_connector_unhealthy{connector}` names which; the data dashboard has it. Then
`ChemclawTargetDown` for whether the pod is gone or merely unreachable.

**This one reads the readiness sweep, which asks `GET /healthz` and nothing else** — so it is silent
for a connector whose pod is up and whose `/mcp` is broken. Measured on 2026-09-19 against a stub
answering `200` on `/healthz` and `500` on `/mcp`: `/readyz` said `{"status":"ready",
"connectors_unhealthy":0}`, the startup line said `molfp=healthy`, and this alert's series held `0`
while every tool call to that connector failed. `ChemclawConnectorsDegradingTurns` is the rule for
that case, and a flat `chemclaw_connectors_unhealthy` is not evidence against it.

#### ChemclawConnectorsDegradingTurns
`warning`. A turn opened this connector and it did not come up, so the turn answered without its
tools. `chemclaw_connectors_unreachable_total{connector}` is the series, one increment per connector
per turn, and the pod's own `connector … is unreachable` WARNING carries the reason and the
correlation id. It names the leaf exception — an `HTTPStatusError` with the status code, a
`MissingConnectorCredential` with the variable that is unset — so read it before assuming a network
fault:

    oc logs -l app.kubernetes.io/name=chemclaw --since=30m | grep 'is unreachable'

Three causes worth separating, because only the first is what `ChemclawConnectorsUnhealthy` would
also catch:

1. **the pod is gone or refusing connections** — `ChemclawTargetDown` and
   `ChemclawConnectorsUnhealthy` fire beside this one;
2. **the pod is up and `/mcp` is broken** — a 500, a garbage body, an MCP handshake that never
   completes. The readiness sweep calls it healthy, so this alert is the *only* one that fires;
3. **the bearer token is missing** — the variable the manifest's `auth.token_env` names
   (conventionally `CHEMCLAW_<NAME>_MCP_TOKEN`) is unset or empty on the front door. Nothing about
   the pod is wrong; the WARNING names the variable, and the fix is a Secret key mapped to it.

`chemclaw_tool_calls_total{tool,outcome="error"}` is the other half of the same picture, for a
connector that *does* come up and then fails its calls (§(x-c) `chemclaw.turns`).

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
`CHEMCLAW_PG_POOL_TIMEOUT_SECONDS`. Find the pod with
`max by (pod) (chemclaw_pg_pool_requests_waiting)` beside `chemclaw_pg_pool_available`. Then either
raise `CHEMCLAW_PG_POOL_MAX_SIZE` **and every ceiling it is checked against** —
`postgres.maxConnections`, and `postgres.sessionStoreMaxConnections` too where
`CHEMCLAW_SESSION_STORE_DSN` names a second server (`Settings` refuses the pod at startup if any of
them stops agreeing) — or lower the concurrency feeding it: `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS`
on the front door, `CHEMCLAW_WORKER_MAX_CONCURRENT_ACTIVITIES` on a worker. Deliberately `max()`
across pods: one saturated process is one saturated process, and averaging hides it.

#### ChemclawFleetAboveItsConnectionCeiling
`warning`. The same blind spot as the turn ceiling, for connections: the fleet can ask for more than
the server will serve, which surfaces as connect failures against a database that is not busy.

**Read which server it is about before changing anything.** The rule is two comparisons, not one:
the primary's live total is `sum(chemclaw_pg_pool_max_size) - sum(chemclaw_pg_session_pool_max_size)`
against `chemclaw_pg_fleet_max_connections`, and a split session store's is the subtrahend against
`chemclaw_pg_session_fleet_max_connections`. Run both halves in the console — the alert does not say
which branch fired, and raising `postgres.maxConnections` when it is the *session* server that is
over changes nothing. Without a split the second gauge is 0 in every pod and this is one comparison
again.

```promql
# primary server (CHEMCLAW_POSTGRES_DSN): live pools vs. postgres.maxConnections
sum(chemclaw_pg_pool_max_size) - sum(chemclaw_pg_session_pool_max_size or vector(0))
max(chemclaw_pg_fleet_max_connections)
# split session server (CHEMCLAW_SESSION_STORE_DSN): vs. postgres.sessionStoreMaxConnections
sum(chemclaw_pg_session_pool_max_size or vector(0))
max(chemclaw_pg_session_fleet_max_connections)
```

**Declaring the second ceiling never silences this alert — it is the only thing that checks the
session server at all.** With `CHEMCLAW_PG_SESSION_FLEET_MAX_CONNECTIONS` undeclared (0) the
session-server branch is off and an over-committed session server is silent.

Check the spelling of the two DSNs first. Both this alert and the startup check decide "split or
not" by comparing the DSN strings, so one server named two ways is measured as two servers each
inside its own ceiling, and neither check sees the real total. Spell both DSNs identically.

### chemclaw.cost

#### ChemclawTokenBurnHigh
`warning`. The fleet-wide burn is above `monitoring.alerts.tokensPerHourWarning`. The per-process
budget guard bounds one runaway process and says nothing about the fleet. §(viii) is how to tell
whether caching is paying off before you raise the threshold.

#### ChemclawTurnsRefusedByBudget
`warning`, and the same subject seen from the chemist's side: they get a 429 with no explanation.
Read `chemclaw_tokens_total` beside it — either the allowance is genuinely spent, or the window is
set below real traffic.

#### ChemclawBudgetNearingItsCap
`info`, and the only one of the three that arrives while you can still do something for free.
Somebody has crossed `budget_warn_fraction` (0.8) of a turn or token cap and has **not** been
refused yet; the rule above is what fires once they are.

**The scope is not on the series, so do not look for it there.** A budget scope is a session id or
an Entra `oid`, and `033_cost_attribution.sql` rules those out as label values for the cardinality
reason the per-metric series cap (D-152) enforces. The identity is in the WARNING log line from
`chemclaw.api.budget`, which names the scope, the unit, the percentage, both numbers and the
session id or `oid` itself:

    oc logs -l app.kubernetes.io/name=chemclaw --since=1h | grep 'budget .* spent'

Three things it can mean, in the order worth checking. **A real runaway** — read
`chemclaw_tokens_total` and the `turn_costs` rows for that actor; the per-turn ceiling
(`agent_max_turn_billed_tokens`) ships as a *runaway backstop*, above what the loop cap and the
context budget already authorise, so it does not bound a turn doing ordinary heavy work — one such
turn is enough to do this. **A cap set below real traffic** — if several unrelated principals
cross in the same window, the cap is the outlier, not them. **A window that is too short for the work** —
`budget_window_hours` is rolling and anchored at each principal's first turn, so a user who does a
day's work in an hour waits out the remainder.

The durable half only engages where `CHEMCLAW_SESSION_STORE=postgres` (the chart's setting).
Everywhere else the per-user counters are per-pod and reset on restart, so this alert under-reports
by roughly the replica count — and `chemclaw_degraded_total{subsystem="budget_window"}` is what
says the durable half was configured and could not be reached, which silently returns the cap to
its per-process meaning.

#### ChemclawAnswerRevisionsNotHelping
`warning`. Over the last 6 h, more than half of the turns whose flagged answer entered the revision
loop ran out of rounds without the answer becoming grounded (and more than 10 such turns tried, so a
quiet deployment cannot trip it). The ratio is
`chemclaw_answer_review_exhausted_total / chemclaw_answer_review_turns_total`. Nothing is *lost* —
those turns answered, and they carry `review_required`
exactly as they would have with `answer_review_max_rounds=0` — but each one spent a second model
call to arrive where it already was.

Three readings, and the first is the likeliest. **The claims are not fixable by rewording**: the
answer needs evidence the turn never retrieved, so the model drops or hedges and the shape gate
flags it again. Read `chemclaw_answer_review_turns_total` against
`chemclaw_evidence_source_chunks_total` for those turns — if retrieval contributed nothing, the
revision was never going to help and the fix is upstream. **The rounds are too few**: raise
`CHEMCLAW_ANSWER_REVIEW_MAX_ROUNDS` and watch the ratio — it is a fraction of *turns*, so raising
the rounds does not move the threshold under you. **The verdict is wrong**: if
`verifier_confidence_threshold` sits above what this corpus can support, every answer is flagged and
every revision exhausts — check what fraction of *all* turns are flagged before raising the rounds.

Turning it back to 0 is a legitimate outcome, not a defeat: the verifier's mark was the signal
before this loop existed and still is.

### chemclaw.fleet — a process is gone

#### ChemclawTargetDown
`critical`. A pod stopped answering `/metrics`. This is the only alert that fires for a process that
is *gone* rather than misbehaving: crash loop, eviction, OOM kill, or a NetworkPolicy that stopped
admitting the scraper. The `pod` label names it; the next commands are:

    oc -n <ns> describe pod <pod>            # Events: OOMKilled, ImagePullBackOff, FailedScheduling
    oc -n <ns> logs <pod> --previous         # the crashed container's last output

Its `for:` is **derived from the chart's own cold-start budget**, not stated: `up` says nothing
about readiness, so a pod is a target for the whole of its start. The window is the largest
`probes.*.startup.periodSeconds × failureThreshold` plus `monitoring.alerts.targetDownMarginSeconds`,
so raising a startup budget moves this alert with it.

Scoped by namespace rather than by `job`, because the `job` label the Prometheus Operator assigns
differs between a ServiceMonitor and a PodMonitor. If the release namespace holds other workloads,
narrow `monitoring.alerts.targetJobPattern`.

#### ChemclawNoWorkerIsScraped
`critical`, and a different failure from the one above: there is no target to be down. Pods that
cannot be scheduled, a PodMonitor whose selector no longer matches, or user-workload monitoring
turned off cluster-wide — in which case every alert here is inert and this is the only one that says
so. Start at §(x-b) step 1.

#### ChemclawNoBackgroundWorkerIsScraped
`critical`, and the one the alert above cannot give you. `ChemclawNoWorkerIsScraped` is
`absent(up{endpoint="metrics"})` over *every* pod in the release, and every enabled connector
*worker* serves a port of that name too (the front door, the connector servers and mcp-face do
not) — so it stays silent while the background worker specifically is gone. This one carries
`app_kubernetes_io_component="background-worker"`. With every connector worker disabled the two
fire together.

It means **nothing in this release is polling `background-jobs`**: sync, re-index, reports and the
connector-job wrapper are all stopped, and none of them emits a counter when it is not running, so
no other alert will say so. That Deployment uses `Recreate` (deliberately — two background workers
racing on one corpus clone is what `D-2026-08-27-what-a-second-background-worker-would-race-on`
pins the replica count to prevent), which means the old pod was taken down *before* the new one was
tried: there is no previous generation still serving.

`absent()` fires only when *no series matches*, so the cause is a pod that never became a scrape
target: unschedulable, `workers.background.replicas: 0`, a selector that stopped matching, or the PodMonitor
not selecting it. An image that will not pull or a container that exits still gets a PodIP and
reports `up == 0` — that is `ChemclawTargetDown`, not this. Start at §(x-b) step 1, then:

    kubectl -n <ns> describe deploy chemclaw-background-worker   # replicas, conditions
    kubectl -n <ns> get events --field-selector involvedObject.kind=Pod | grep background-worker

The `for:` is long on purpose and is derived rather than chosen: `Recreate` waits out the old pod's
whole `terminationGracePeriodSeconds`, then the new pod gets its startup budget, then the same
margin `ChemclawTargetDown` allows. If it fires, the window has already passed — this is not a
rollout in progress.

**What it does not catch**: a worker that serves `/metrics` and never passes `/readyz`. A PodMonitor
scrapes unready pods, so `up` is 1 and this stays silent. That case is `ChemclawWorkerNotPolling`
below, which renders only when the Temporal SDK exporter is enabled.

#### ChemclawWorkerNotPolling
`critical`, and rendered only when `monitoring.temporalSdkMetrics.enabled` is on. A worker is up and
answering its probes while asking Temporal for no work, so jobs queue and nothing runs them. This is
the gap the worker's probes leave: `/readyz` is deliberately not a liveness signal, because
restarting on a lost broker connection would turn an ordinary reconnect into a crash loop — but it
*does* report broker contact, because `worker_ready` is
`worker.is_running and broker_seen_recently()` (see §(x)). Ask it with
`oc -n <ns> port-forward pod/<pod> 9000` (`workerMetricsPort`) and `curl -s localhost:9000/readyz`.
So `/readyz` on the
named pod **is** a second opinion: 503 says this worker has heard nothing from the broker for
`jobs_in_flight_refresh_seconds` × 3, which points at the broker or the path to it; 200 says the
worker is polling and the alert is about what it is polling *for* — a queue name or a task-queue
mismatch.

**It cannot point at a bundle whose worker was never rendered.** The rule is `sum by (pod) (temporal_num_pollers{…}) == 0`, so a queue no pod polls
produces no series, `sum by (pod)` yields an *empty vector*, and `== 0` matches nothing — green
for ever, measured against a deliberately mistyped queue name with the whole fleet healthy. That
case is detected by nothing in this stack today: every probe was 200, `chemclaw_connectors_unhealthy`
was 0, and the only trace of the wedged run was `chemclaw_jobs_in_flight 1` with no age beside it.
Read the pod's own
`chemclaw_degraded_total{subsystem="jobs_in_flight"}` either way, and check the **broker** before
restarting anything.

### chemclaw.turns — the answer itself

#### ChemclawTurnLatencyHigh
`warning`. p95 is over `monitoring.alerts.turnLatencyP95Seconds`. Break it down in this order, all
on the tools and data dashboards, taking `histogram_quantile(0.95, …)` over each histogram's
`_bucket` series: `chemclaw_tool_duration_seconds` **by `tool`** — that label is what makes "which
tool is slow" answerable at all, and it did not exist until this pass — then
`chemclaw_model_call_duration_seconds` — the gateway's own latency, unlabelled because there is
one endpoint — then `chemclaw_evidence_source_seconds` by source. Slow turns hold admission
permits, so this tends to precede `ChemclawTurnsShed`.

#### ChemclawTurnsTimingOut
`warning`. Someone waited out `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` and got nothing. If
`ChemclawTurnLatencyHigh` is already firing these are its tail and the cause is upstream of the
timeout.

#### ChemclawTurnsAnsweringEmpty
`warning`, and the quietest bad outcome in the system: the turn succeeded and produced nothing to
read, **and nothing explains it** — a turn stopped by either cap is excluded from the counter, so
`ChemclawTurnsHittingACap` is the rule for that and this one is not. Usually a model that emitted
only tool calls, or a middleware that short-circuited after the last one;
`make explain SESSION=<session-id>` reconstructs the turn.

#### ChemclawToolCallsFailing
`warning`. Most calls to one tool are failing, and turns are still answering without whatever it
would have contributed — so the transcript of a degraded answer looks like any other, which is why
this is a rule and not only a panel. The `tool` label names it;
`sum by (tool, outcome) (rate(chemclaw_tool_calls_total[15m]))` is the breakdown.

`outcome="error"` is a raised exception **or** a connector answering `isError=True`
(`agent/audit.py` records both as `error`, which is the fix for a returned failure being written as
`ok`). It is never a governance refusal — that is `outcome="refused"` and
`chemclaw_tool_refusals_total{reason}`, and a dry run or an unapproved plan moving those is the
control working. So this rule is about faults, and the threshold is
`monitoring.alerts.toolErrorRatio` rather than anything near zero because a tool legitimately
refuses bad input by raising.

For a connector tool, read `ChemclawConnectorsDegradingTurns` beside it: that one is a connector
that never came up, this one is a connector that came up and fails its calls, and only this one can
name the tool. An in-process tool points at this image instead.

#### ChemclawTurnsHittingACap
`warning`. A turn was cut short with work still open. **Not a silent failure** — the chemist was told
in the same stream, as `spend_cap_reached` or `loop_cap_reached` — so the question this alert asks is
whether the ceiling is right, not whether something broke.

`sum by (outcome) (increase(chemclaw_turns_finished_total{outcome=~"spend_capped|loop_capped"}[1h]))`
says which cap and how often. The two have different remedies:

| `outcome` | the ceiling | the counter that isolates it |
| --- | --- | --- |
| `spend_capped` | `CHEMCLAW_AGENT_MAX_TURN_BILLED_TOKENS` | `chemclaw_turn_spend_caps_total` |
| `loop_capped` | `CHEMCLAW_HARNESS_MAX_LOOP_ITERATIONS` | `chemclaw_turn_loop_caps_total` |

The judgement is the one `deploy/helm/chemclaw/values.yaml` states beside the spend setting: if it
moves on turns that were doing real work, the number is too low; if it moves on runaway ones, the
guard is working and the request is what to look at. `make explain SESSION=<session-id>` reconstructs the turn,
and the pod's `the turn for session … hit its N billed-token cap after M tokens` WARNING carries both
numbers. The budget is a **request**-spend bound rather than a thread bound — see
`CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET` and §(viii) before raising it, because a turn whose prefix is
already large spends most of its allowance re-sending context.

### chemclaw.durable — the expensive half

#### ChemclawDurableJobsFailing
`critical`. More than `monitoring.alerts.jobFailureRatio` of jobs are ending `failed`, and — as with
`ChemclawTurnsFailing` — only once job traffic clears `monitoring.alerts.jobsFloorPerSecond`, since
one failed overnight search would otherwise be a 100% failure rate. Break down by
connector with `sum by (connector, outcome) (rate(chemclaw_jobs_finished_total[30m]))`, then by
activity with `chemclaw_activity_failures_total` — which counts one row per *attempt*, so a retry
storm shows as a rate rather than only in the broker's history. The Temporal UI's event history is
the next stop (§(x)).

#### ChemclawActivityRetryStorm
`warning`. Every attempt of one activity has failed for half an hour, so its job is retrying without
progress or has already given up. The Temporal event history for a workflow using it is the fastest
route to the exception (§(x)); `chemclaw_jobs_finished_total{outcome="failed"}` says whether jobs are
dying with it.

**`ActivityResultTooLarge` is the one exception here that is not a bug in the activity's body.** It
means the activity produced a result bigger than `CHEMCLAW_ACTIVITY_RESULT_MAX_BYTES`, which ships at
the broker's own 2 MiB `limit.blobSize.error`. `durable/interceptor.py` raises it *before* the
result is uploaded, on purpose: left to the broker, an oversized result either failed the workflow
with no first-party trace or (over the 4 MiB gRPC frame) retried for ever as a `ResourceExhausted`
"network" error, with no counter moving. The refusal is non-retryable, so it costs one attempt
rather than `CHEMCLAW_ACTIVITY_MAX_ATTEMPTS`.

The fix is almost always to bound the activity's output rather than to raise the ceiling — the
message names the activity and the byte count. `collect_digests` is the one shipped activity whose
result has no bound of its own (one entry per subscription, each carrying every matching note id and
headline), so a large corpus with many subscriptions is where to look first. Raising the ceiling
means raising the broker's `limit.blobSize.error` in the same change, or the refusal simply moves
back to the invisible side.

#### ChemclawPushBackDropped
`warning`. A finished job's result never reached the session that asked for it. The job succeeded
and the result is stored; what was lost is the chemist being told. Check
`chemclaw_event_streams_open` against `chemclaw_event_stream_capacity` on the front door first.

#### ChemclawFanOutChildrenDropped
`warning`. A fan-out parent completed **reporting success** with children missing, so its result is
incomplete. The scenario this exists for is three memory-synthesis jobs going green every night
while returning `[]`.

#### ChemclawResultPublishFailing
`warning`, and the severity is the point: this is the *retryable* half. `publish/outbox.py` spends
one attempt and leaves the row `pending`, so the result still has a route to its sink. The
calculation stands in the cache either way. Diagnose:

1. `grep 'publish\['` in the background worker's log — `publish[enqueue:…]` lines are a failure to
   *queue* (the session database, or `CHEMCLAW_RESULT_SINKS` naming a sink no manifest declares);
   anything else is delivery.
2. `SELECT sink, attempts, left(last_error, 160) FROM result_publications WHERE state = 'pending'
   AND last_error <> '' ORDER BY enqueued_at DESC LIMIT 20;` — the driver's own error.
3. A connect failure to a reachable database is usually the egress posture: the sink's host must be
   in `CHEMCLAW_EGRESS_ALLOW` **and** in `networkPolicy.egressDestinations` (§(xvi) step 2).
   `ChemclawEgressRefused` / `ChemclawEgressPreloadRefused` fire beside this one in that case.
4. Authentication failures are the `RESULTS_DB_*` variables the sink manifest names.

`ChemclawResultsDeadLettered` is the same publication once the retries are gone, and that one is
`critical`.

#### ChemclawResultProjectionFailing
`critical`, and **retrying will not help**: `publish/` could not turn a calculation into the typed
record its sink expects, which is a schema disagreement between this build and
`schema/result-store/`. The result is dropped before any delivery is attempted. The worker logs
`publish: could not project <calc_ref> (<calc_type>)` with the traceback; the calc type names the
projector to look at. A projector that raises will raise on every payload of that shape until the
code changes, so this is a defect to report with that traceback, not an operational fix. Once an
image with the fix is deployed, `python -m chemclaw.cli.backfill_publications` queues the results
that were skipped (it is idempotent).

#### ChemclawResultsDeadLettered
`critical`. Publications exhausted their retries and were retired to `failed`. Nothing will attempt
them again, so the scientific record this deployment publishes to is missing a computed result
until an operator re-queues it. `chemclaw_outbox_dead_lettered{sink}` is how many; the rows say why:

```sql
SELECT sink, attempts, left(last_error, 160), enqueued_at
FROM result_publications WHERE state = 'failed' ORDER BY enqueued_at DESC LIMIT 20;
```

Fix the destination first (`ChemclawResultPublishFailing`'s checks),
then re-queue as in §(xvi) *When a destination has been down*:

    python -m chemclaw.cli.backfill_publications --requeue

#### ChemclawRetentionNotSweeping
`warning`, and it is the *absence* of `chemclaw_table_bytes`. Every retention pass republishes that
family — for every table the disposal register names, whether or not the pass deleted anything — so
several missed passes of `CHEMCLAW_RETENTION_SCHEDULE_MINUTES` means the sweep itself is not
running, and nothing is disposing of `session_messages`, `tool_result_blobs` or the three LangGraph
checkpoint tables while it is not. Check the `retention` Temporal Schedule first — in the Temporal
UI under *Schedules*, or with the Temporal CLI:

    temporal schedule describe --schedule-id retention --namespace <temporal.namespace>

A missing schedule means the `chemclaw-schedules` post-install/post-upgrade Job did not run or
failed (a failed one is kept: `kubectl -n <ns> logs job/chemclaw-schedules`; re-running
`helm upgrade` re-applies them); a paused one was paused by hand. Then read the
background worker's logs. A pod that is gone entirely raises `ChemclawTargetDown` beside this; only
this one fires for a worker that is up with its sweep not running.

What each pass removed is in its own activity result, per table, in rows **and** in bytes — a sweep
that runs and removes nothing is a window left at 0, which this rule stays silent on because the
family is still being republished. `topk(5, chemclaw_table_bytes)` is the query for "what is filling
the volume", and the answer is often a table the register **refuses** to prune.

Rendered only for a release that states `retention.windows`: with `retention.unboundedGrowthAccepted`
there is no sweep to be absent. Its window and hold are `monitoring.alerts.silenceWindowPasses` and
`silenceHoldPasses` multiplied by the sweep cadence, so changing the cadence moves the alert with
it; the hold is what stops a fresh install from paging before its first pass.

#### ChemclawResultOutboxStuck
`warning`. The oldest undelivered publication for this sink is older than
`monitoring.alerts.outboxStuckSeconds`. Read `chemclaw_outbox_pending{sink}` for the depth and
`chemclaw_outbox_dead_lettered{sink}` for what has already been given up on.

#### ChemclawOutboxBacklogUnreported
`warning`, and it is the *absence* of the series the alert above reads. All three outbox gauge
families are written together by one drain pass and are empty on a fresh process until that pass
runs, so a drain that is not running at all produces no series — and a rule over
`max by (sink) (…)` then has nothing to evaluate and stays green, which reads exactly like an empty
queue. Check the `result-publish` Temporal Schedule first (as for `ChemclawRetentionNotSweeping`
above) and the background worker's logs second. A pod
that is gone entirely raises `ChemclawTargetDown` beside this; only this one fires for a pod that is
up with its drain not running.

Its window and hold are `monitoring.alerts.silenceWindowPasses` and `silenceHoldPasses` multiplied
by `CHEMCLAW_RESULT_PUBLISH_SCHEDULE_MINUTES`, so changing the cadence moves the alert with it. The
hold is what stops a fresh install from paging before its first pass.

### chemclaw.degradation — something is quietly not working

#### ChemclawSubsystemDegraded
`warning`, and the umbrella over every deliberate exception swallow that goes through
`metrics_bridge.degraded`. The `subsystem` label names which one; the pod's log carries the
exception. Turns keep being answered, which is exactly why this needs an alert rather than a panel.

It excludes `subsystem="evidence_source"`, which is a de-duplication rather than a gap:
`retrieval/fanout.py` calls `degraded()` *and* increments
`chemclaw_evidence_source_failures_total` on the same exception, so one broken retrieval leg used to
raise this alert and `ChemclawEvidenceSourceFailing` together, at the same severity over the same
window — of which only the second names the leg.

#### ChemclawEvidenceSourceFailing
`warning`. A retrieval leg is raising, so answers are composed from the remaining legs and cite
nothing from this one — and nothing in the answer says so. Read
`chemclaw_evidence_source_chunks_total` and `chemclaw_evidence_source_skips_total` on the same
`source` label: a leg that fails, a leg that declines and a leg that legitimately matches nothing are
three different states, and telling them apart is what
`D-2026-08-01-a-cap-that-starves-a-source` was written about.

#### ChemclawIngestLagUnreported
`warning`, and the same shape as `ChemclawOutboxBacklogUnreported` one tier over: the cursor-lag
family is populated by `load_cursor`/`store_cursor` and is empty until a sync runs, so "no sync ran
at all" is an absence that `ChemclawIngestCursorStalled` cannot evaluate. Check the `eln-sync`
Schedule and `chemclaw_ingest_records_total{source}`. A source that was never configured emits
neither series and is not what this is about.

#### ChemclawIngestCursorStalled
`warning`. A source is further behind than `monitoring.alerts.ingestLagSeconds`, so the corpus
chemists query is stale by at least that much. A wedged fetch advances no cursor and logs
`ingested=0`, which is byte-identical to a genuinely quiet source — the lag gauge is the only thing
that separates them. Read `max by (source) (chemclaw_ingest_cursor_lag_seconds)` for which source
and `sum by (source, outcome) (increase(chemclaw_ingest_records_total[1h]))` for whether it is
fetching anything, then the background worker's log for that source's sync. §(v) covers
re-ingesting rejected entries.

#### ChemclawGaugeReadFailing
`warning`. `render()` drops one gauge whose source raised rather than losing the whole scrape, so
every other series is intact and this one is simply *absent* — which on a graph is
indistinguishable from a value that has not changed. The `metric` label names it.

#### ChemclawMetricSeriesDropped
`warning`. A metric hit its per-metric label-set cap and is now undercounting by an unknown amount,
along with every alert and panel reading it. A label here is meant to be low-cardinality, so this
means something is generating values it should not; the series' own `metric` label names which
metric, and `count by (pod) ({__name__="<that metric>"})` shows how many label sets each pod is
holding. The cap is a code constant (`_MAX_SERIES_PER_COUNTER` in `core/metrics.py`, D-152), not a setting: the fix is the code path minting the
values, so report it with the label values you see.

#### ChemclawEgressRefused
`critical`. The in-process egress guard (`chemclaw.core.netguard`) refused an outbound connection to
a host that is not the LLM gateway, declared infrastructure, or a named exception — a dependency
reaching out at runtime, or a misconfiguration. This should be zero. Triage: find the host with
`oc -n <ns> logs <pod> | grep 'egress refused'`; if it is a destination the deployment legitimately
needs (a new connector, an internal service), add it to `CHEMCLAW_EGRESS_ALLOW` or fix the setting
it should have been derived from; if it is not, it is an attempted exfiltration path and the
component that raised it is the lead — the NetworkPolicy is the layer that also stopped it at the pod
boundary.

#### ChemclawEgressGuardDisarmed
`critical`. A process is running with `CHEMCLAW_EGRESS_GUARD_ENABLED=false`, so outbound calls are
bounded only by the NetworkPolicy — the defence-in-depth layer for "only LLM traffic leaves the
estate" is off in that process. Either it was disabled deliberately (and this alert should be
silenced for that deployment, with the reason recorded) or a values file turned it off by accident;
set it back to `true` and roll the affected pods.

#### ChemclawEgressPreloadDisarmed
`critical`. A process is running without the compiled egress interposer
(`src/chemclaw/core/netguard_preload.c`) loaded, so every client that opens sockets below the
interpreter — grpc's C-core, Temporal's Rust sdk-core, the OTLP gRPC exporter — is bounded only by
the NetworkPolicy. **`ChemclawEgressGuardDisarmed` will be silent**, because the in-process guard is
a different layer and reports separately; that pair reading 1 and 0 is exactly the condition this
alert exists for. Causes, in the order to check them: the pod was started with an explicit `command`
that bypasses `chemclaw-entrypoint` (the knowledge-sync containers do this deliberately and are not
in the alert's scope, since they run `git` rather than a component; the three hook Jobs *did* it by
accident and no longer do — `tests/test_netguard_preload.py` derives that set from the templates
now, and a Job declares no port, so this alert could never have reported them);
`CHEMCLAW_EGRESS_GUARD_ENABLED` is `false`, which turns off both layers by design; or the image was
built without the interposer, in which case `ls /app/lib` in the pod is empty and the fix is a
rebuild. `LD_PRELOAD` naming a path
that does not exist is ignored by the loader without a word, so believe the gauge rather than the
environment variable.

#### ChemclawEgressPreloadRefused
`critical`. The interposer refused an outbound dial or a name lookup. `kubectl logs` the pod and grep
`chemclaw-netguard-preload:` — the line names the destination, the port and the verb it refused
(`connect`, `sendto`, `sendmsg`, `sendmmsg` or `resolve`). **The two counters split dial from
lookup, not verb from verb**: every dial verb books on
`chemclaw_egress_preload_refused_connect` and only `resolve` on `..._refused_resolve`, because a
blocked destination and a blocked *name* want different next steps and the verb is in the log line
where the detail belongs. Decide whether the destination is legitimate. If it is, add its host to
`CHEMCLAW_EGRESS_ALLOW` (bare host, no scheme, no port) and roll the pods. **One destination is
commonly legitimate and is derived from no setting**: a *remote* git note repository, because
`kg/git_writer.py` shells out to `git`, which inherits `LD_PRELOAD` and so is bounded by this layer
alone. A collector named only in `OTEL_EXPORTER_OTLP_ENDPOINT` is **not** a second one — `netguard`
reads that variable and `…_TRACES_ENDPOINT` beside `CHEMCLAW_OTEL_ENDPOINT` whenever tracing is on,
which is the only posture in which anything dials a collector at all (measured: with only the
standard variable set, `derive_allowed` returns `['127.0.0.1', 'collector.example', 'localhost']` at
`otel_enabled=true` and drops it at `false`). If the destination is not legitimate, the refusal is
the control working — record what it was before silencing anything.

## (xi) A migration that will not apply, and a release stuck in `pending-upgrade`

Migrations run as the `chemclaw-migrate` Helm hook Job (`pre-install,pre-upgrade,pre-rollback`)
that completes before any app container starts (D-034), so a failure here blocks the release rather
than half-applying it. The Job (the image's `migrate` component) applies the migrations and then the
grants — what `make db-migrate` and `make db-grants` do by hand — as the migration principal. Its
bounds are `CHEMCLAW_PG_MIGRATION_LOCK_TIMEOUT_SECONDS` (per statement),
`CHEMCLAW_PG_MIGRATION_LOCK_WAIT_SECONDS` (for the advisory lock), and
`migrateJob.backoffLimit` / `migrateJob.activeDeadlineSeconds` for the Job
(D-2026-08-01-a-migration-waits-in-front-of-live-traffic). Each failure below has its own symptom;
read the Job's log first:

```
kubectl -n <ns> logs job/chemclaw-migrate     # kept on failure; deleted on success
```

**`helm rollback` keeps every table and column the older image needs — and that is not the same as
safe.** Checked over the whole directory, not one `infra/sql/*.sql` file contains a `DROP TABLE` or
`DROP COLUMN`, and `chemclaw.core.migrate` refuses to let an applied file change afterward (a
checksum mismatch raises `MigrationError`; see (ii)). So the schema only ever grows and the older
binary still finds every column it reads.

**What it does not keep is every *constraint* that binary depends on**: some migrations drop and
re-add a primary key, replace a `CHECK`, null a backfilled column or rewrite a column's type. The
authority is `_REVIEWED_ROLLBACK_BREAKS` in `tests/test_migrations_are_additive.py`, and **Roll back
a release** below says what each one strands. Only the *expand* half of expand/contract has ever
been exercised here; nothing has ever been dropped, and no gate enforces the ordering a drop would
need.

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

**The Job failed after ~5 minutes (`CHEMCLAW_PG_MIGRATION_LOCK_WAIT_SECONDS`) waiting on
`pg_advisory_xact_lock`.** Another migrator is running,
or one died holding the lock. Check:

```sql
SELECT pid, granted, now() - state_change AS age FROM pg_locks
JOIN pg_stat_activity USING (pid) WHERE locktype = 'advisory';
```

A live peer: wait and re-run, which applies nothing because it finds every file already recorded. A
dead one: the lock is transaction-scoped, so it is released the moment that backend disconnects —
there is nothing to clean up by hand.

**The release is stuck in `pending-upgrade`.** Helm waits for the hook; the Job fails at
`migrateJob.activeDeadlineSeconds` (900 s shipped), after up to `migrateJob.backoffLimit` retries,
and Helm then marks the release `failed`. Recovery:

```
helm -n <ns> status <release>                    # pending-upgrade / failed
kubectl -n <ns> logs job/chemclaw-migrate        # read it NOW: the next hook run replaces this Job
helm -n <ns> rollback <release>                  # or `helm upgrade --install` again once the cause is fixed
```

Read the log before the rollback: the rollback's own `pre-rollback` migrate Job is created under the
same name and the failed one is deleted first (`before-hook-creation`). If `helm status` still says
`pending-upgrade` after the Job is gone (the Helm client was killed mid-wait), `helm rollback` to
the last `deployed` revision in `helm history` is what clears it.

**What `helm rollback` does and does not undo.** It restores the release manifest — every Deployment
and Service, and `chemclaw-config`, the ConfigMap the pods read, which is an ordinary tracked
resource for exactly this reason (a hook is not release state, so a rollback would leave the newer
configuration live). It does **not** undo a data conversion:
`chemclaw-convert` is a `post-upgrade` hook whose backfill rewrites `session_messages` rows, and
neither rollback nor uninstall re-runs or reverses it — which is also why this chart must not be
deployed with `helm upgrade --atomic` (`deploy/jenkins/targets/openshift.sh` does not).

**Rolling back *across* that move is the one rollback that is not routine.** A revision installed
before `chemclaw-config` and the ServiceAccount became tracked has neither object in its manifest,
so Helm deletes both while restoring Deployments that name them — and prints "Rollback was a
success!". Both now carry `helm.sh/resource-policy: keep`, which Helm reads off the live object at
deletion time, so they survive it; measured on k3s v1.29.9, with the annotation the same rollback
leaves both standing and the pods start. What survives is the **newer** release's configuration,
because the target revision has none to restore — so after a rollback past that boundary, check it:

```
helm history chemclaw -n <ns>                       # is the target revision from the older chart?
kubectl -n <ns> get configmap chemclaw-config -o yaml   # this is the newer release's data
```

Prefer rolling *forward* across that boundary. Note the cost the annotation buys this with:
`helm uninstall` now leaves those two objects behind, which is what the older chart did.

### `make helm-validate` says a binary is "not installed - see docs/guides/runbook.md"

It means this section. The chart gate needs three binaries on `PATH`; all three install in under a
minute, and the target then passes locally. Install the versions CI pins (`HELM_VERSION`,
`KUBECONFORM_VERSION`, `PROMETHEUS_VERSION` in `.github/workflows/ci.yml`):

```
helm         # https://get.helm.sh/helm-<HELM_VERSION>-linux-amd64.tar.gz
kubeconform  # https://github.com/yannh/kubeconform/releases  (kubeconform-linux-amd64.tar.gz)
promtool     # https://github.com/prometheus/prometheus/releases  (inside the prometheus tarball)
```

`make helm-validate` then renders the chart twice (shipped defaults, and every off-by-default
switch on), validates each render against the Kubernetes schemas, and runs `promtool check rules`
over the alert rules. Its `Skipped: N` lines are kinds kubeconform has no schema for (the OpenShift
`Route` among them); `tests/test_deploy_chart.py::test_every_resource_kubeconform_skips_is_one_this_file_declared`
holds that list. A green `make test` without these binaries has *not* checked the chart.

### `helm upgrade` refuses: "exists and cannot be imported into the current release"

```
Error: UPGRADE FAILED: Unable to continue with update: ServiceAccount "chemclaw" in namespace
"<ns>" exists and cannot be imported into the current release: invalid ownership metadata;
annotation validation error: missing key "meta.helm.sh/release-name" ...
```

**Expected, once, for every release installed before `chemclaw-config` and the runtime
ServiceAccount became tracked resources.** On the previous chart both were `pre-install,pre-upgrade`
hooks with `hook-delete-policy: before-hook-creation`, so they persist between releases — and Helm
creates hook resources with a plain `Create`, so they carry no ownership annotations. The current
chart claims those same two names in the manifest, and Helm will not adopt an unowned object.

Nothing is half-applied: this is a prepare-time refusal, before any hook runs, and
`helm upgrade --dry-run` refuses identically. `deploy/jenkins/targets/openshift.sh` performs the
adoption itself (and reports it without acting when `DRY_RUN=true`, its default), so the pipeline
path needs nothing here. For a hand-run `helm upgrade`, adopt the two objects and re-run:

```
kubectl -n <ns> annotate --overwrite configmap/chemclaw-config serviceaccount/chemclaw \
  meta.helm.sh/release-name=<release> meta.helm.sh/release-namespace=<ns>
kubectl -n <ns> label --overwrite configmap/chemclaw-config serviceaccount/chemclaw \
  app.kubernetes.io/managed-by=Helm        # already set by the old chart; harmless if unchanged
helm upgrade --install <release> deploy/helm/chemclaw -n <ns> ...
```

Adopt only objects your own previous release created — check `helm.sh/hook` and
`app.kubernetes.io/instance=<release>` on them first (`kubectl get -o yaml`). An object that
collides for any other reason is somebody else's, and taking it over is a decision rather than a
step.

**The Job says a migration was edited after being applied.** `MigrationError`, and the fix is never
to edit the file back: `schema_migrations` records a checksum precisely so an in-place change is
loud. Add a new numbered file that makes the change forward.

**`applied migrations: (none)` on a fresh database.** The migration directory
resolved to nothing. `CHEMCLAW_SQL_MIGRATIONS_DIR` is workdir-relative (`/app/infra/sql` in the
image); an empty glob applies zero files and reports success (D-148).

## Replay the migrations against a database that already has the schema

A restore from a logical dump, or a runner re-pointed at a hand-built database, gives you objects
without a matching `schema_migrations` ledger. Two merged migrations are not re-runnable in that
state and the runner sends everything in one transaction, so the run aborts and *nothing* applies.
Run both statements first — unconditional and idempotent, so there is no arm to work out:

```sql
ALTER TABLE session_messages DROP CONSTRAINT IF EXISTS session_messages_shape_known;
ALTER TABLE note_proposals   DROP CONSTRAINT IF EXISTS note_proposals_state_known;
ALTER TABLE note_proposals   ADD CONSTRAINT note_proposals_state_known
    CHECK (state IN ('open', 'merged', 'rejected', 'failed', 'superseded'));
```

Without them the run stops at `046_review_hardening_indexes.sql`
(`DuplicateObject … session_messages_shape_known`) or at `058_note_proposal_superseded.sql`
(`UndefinedObject … note_proposals_state_known`). The authority is `_REVIEWED_REPLAY_BREAKS` in
`tests/test_migrations_are_additive.py`, which carries the recipe beside each one. Then run
`make db-migrate` (it records every file in `schema_migrations` as it applies it) and
`make db-grants`.

## A fingerprint index or a label corpus mid-rebuild

**`PARTIAL: N record(s) indexed under the current definition and M still under a superseded one.**
A fingerprint-definition change (the `std6`→`std7` bump, say) retired those M rows. Searches answer
over the N and say so in their own `verdict` — they are never wrong, only narrow. There is no
re-index target: the fingerprint tables are written only by the ELN sync, so a rebuild means
re-running that sync from the start, which means deleting the corpus's `corpus_cursors` row. Two
limits before you do. The runtime role holds no `DELETE` on `molecule_fingerprints` or
`reaction_fingerprints`, so a molecule whose standardized SMILES changed leaves its old row behind
permanently and it stays in the superseded count. And re-fingerprinting from the stored labels
rather than from the corpus is not a valid rebuild: the stored label is the *previous*
standardization's output, and standardization discards information.

**`N of M reaction(s) were stamped with nothing derived.`** Those rows carry the marked stamp, so
they have left the stale set and the drain advances, and `coverage` counts them as unlabelled —
which is what it should do. They are re-derived on the next version bump. If `current_version()`
returns nothing at all, the whole corpus is in that state: bring the labelling server back and bump
the version to force a pass.

## Roll back a release

The case (xi) covers is the *safe* one: the migrations did not apply, so nothing moved. This is the
other one — the migrations applied, the release is bad, and the previous image has to come back
against a database that has already moved on.

**Do this first.**

```
helm -n <ns> history <release>                   # pick the last good revision
helm -n <ns> rollback <release> <revision>
```

The release pipeline pins by digest, so this restores the bytes that were reviewed, and Helm runs
the *target* revision's `pre-rollback` migrate Job — so the grant file that revision was written
against is re-applied with it, and its migrate half is a no-op
(`D-2026-09-09-a-grant-set-that-contracts-is-not-a-pre-upgrade-step`). A rollback to a revision whose
chart predates the `pre-rollback` hook does not get that, and needs `make db-grants` run by hand from
the restored image.

**Then read the logs for one line.** `migrate.database_ahead` names how many ledger rows the restored
image ships no file for, and the newest one. It is a WARNING and not a refusal, because the schema
only goes forward and a rollback must still start — but it is the only thing that will tell you the
database is ahead. `make db-migrate` prints `(none)` in this state, which means "this image applied
nothing", not "the database matches this image".

**What is still broken after a successful rollback**, by the migration that stranded it:

| Migration | What the restored image loses | Loud? |
|---|---|---|
| 041, 056, 063 | the document-share sync, ELN ingest, and the fingerprint index stop writing (`ON CONFLICT` no longer plans) | yes — `InsufficientPrivilege`/`InvalidColumnReference` in the log |
| 088 | **every turn's cost ledger row**, indefinitely | only where monitoring is deployed: `ChemclawSubsystemDegraded` fires, and `operations.activity.spend` then reports an empty ledger with no error |
| 090 | the calculation cache stops filtering by epoch, so `find_calculations` offers superseded results to the model as evidence to cite | **no — this one is silent.** Treat browse results as unfiltered until you are forward again |
| 089 | the publish lease is ignored, so a drain re-claims a row another is mid-delivering and the attempt budget empties twice as fast | no |
| 091 | nothing — the column widened to double precision and the restored image writes a Python float into it exactly as before | n/a |
| 092 | a session taking its **first** turn during the rollback window comes back with `session_owners.updated_at` NULL, so it is missing from `GET /sessions` until it is spoken in again; the pre-092 image derives the order and never maintains the column | **no — this one is silent.** Re-run 092's backfill by hand to restore it |
| 093 | `record_observation` stops writing: the restored image's `ON CONFLICT (property, input_hash)` no longer plans against a key that now carries `source`, so no observation is recorded and no calibration is scored | yes — `InvalidColumnReference` in the log |
| 094 | every fingerprint and corpus-reaction write stops: the restored image's `ON CONFLICT (id)` / `(source, id)` no longer plans against a key that now carries `definition` | yes — `InvalidColumnReference` in the log |
| 106 | nothing — a plain GIN index on `turn_costs.skills_loaded` is dropped, and no `ON CONFLICT` names it and no query plans through it. Re-run 105 if you want it back | n/a |
| 115 | nothing it reads — but artefacts have no foreign key to `session_owners`, so the restored image **deletes a session, erases a leaver and forgets an empty session without their artefacts**: the transcript goes and the artefacts stay, reachable by no session, and a leaver's artefact revisions in other people's sessions keep their name | **no — this one is silent.** After rolling forward, run `DELETE FROM session_exhibits e WHERE NOT EXISTS (SELECT 1 FROM session_owners o WHERE o.session_id = e.session_id)` on the session database (revisions cascade), then re-run every erasure requested during the rollback window (`(xv)`, *Offboard: erase their data*) so the leaver's revisions elsewhere go too |
| 116 | no write — the `session_exhibits` kind `CHECK` widens, and the restored image writes only kinds it still admits. But a `geometry` artefact written before the rollback **cannot be opened or exported** by it: its spec model refuses the kind, so the listing shows the artefact and every read of its body fails | yes — a refusal naming `geometry` on every such read. Roll forward, or leave those artefacts unopened until you do |
| 117 | no write — the kind `CHECK` widens again, for `html`. A restored image **cannot open or export** an `html` artefact, nor any revision whose spec binds a value (`$bind`, `rows_from`) — its spec model refuses both — so the listing shows them and every read of their bodies fails | yes — a refusal naming `html`, `$bind` or `rows_from` on every such read. Roll forward, or leave those artefacts unopened until you do |

119 needs no row: it adds a nullable column the restored image never names, and a person's revision written during the window records no introduced figures, which the newer image derives from the revision and its parent exactly as it did before the column existed.

058 and 106 are exempted and do not actually break: 058's `CHECK` widens, and 106 drops a plain index rather than a unique one — `DROP INDEX` is flagged because the pattern cannot tell the two apart.

**This table is checked against the registers.**
`tests/test_migrations_are_additive.py::test_every_reviewed_break_tells_the_operator_what_it_costs`
fails if an entry of `_REVIEWED_ROLLBACK_BREAKS` or `_REVIEWED_SEMANTIC_BREAKS` is missing from this
section. Rows the registers do *not* hold (090, 115) are here because they cost the operator
something even though no exemption was needed.

**What no rollback undoes**: the ConfigMap history, the `post-upgrade` data conversion, and any row
the newer generation wrote in a shape the older one cannot read.

## (xii) A caller is being refused (429 / 413), or should be and is not

Several bounds sit in front of the app, at different levels, because most of them cannot be enforced
from inside it (D-2026-08-01-a-cheap-request-is-still-a-request). Read the bold leads below; each
names the refusal, the setting and the counter.

**429, `Retry-After: N`.** The per-principal request budget. It is a token bucket:
`CHEMCLAW_SERVICE_RATE_LIMIT_PER_MINUTE` is the sustained refill and
`CHEMCLAW_SERVICE_RATE_LIMIT_BURST` is what one caller may spend at once. The code default `0`
disables it; the chart ships `120` and `30`. Watch `chemclaw_requests_rate_limited_total` — a steady non-zero rate is usually
a script someone wrote against the API rather than an attack, and the fix is to raise the burst for
that deployment, not to switch the limiter off.

Two properties worth knowing before you tune it:

- **It is per process.** With `service.autoscaling.maxReplicas: 6` the fleet ceiling is six times
  what you configured, and a caller pinned to one pod by the Route's affinity cookie (D-121) sees
  the per-process number. A genuine fleet-wide limit belongs at the ingress.
- **The probes are exempt by construction.** `/healthz`, `/readyz` and `/metrics` do not depend on
  `require_principal`, which is the only place the budget is spent. If a probe ever starts getting
  429s, the gate has been moved somewhere it should not be.

**429, `Retry-After: N`, on `POST /sessions/{id}/messages` only — and this is a *different* refusal
with the same shape.** The per-actor concurrent-turn cap
(`D-2026-09-19-a-pod-wide-cap-is-not-a-fair-one`). It answers when one principal already holds
`CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS_PER_ACTOR` turns *in flight* on this process, so it is a
count of simultaneous turns rather than a rate, and the request budget above can be wide open while
this fires. **The counter is `chemclaw_turns_refused_actor_cap_total`**, not
`chemclaw_requests_rate_limited_total` — reading the wrong one is the likeliest way to spend an
afternoon here, because the header and the status are identical.

- **Read it beside `chemclaw_turns_shed_total`.** Rising alone means the guard is doing its job:
  one chemist wanted more than their share and everyone else was unaffected. Rising *together* means
  the pod is genuinely full as well, and the per-actor cap is not why anyone is waiting.
- **`chemclaw_turn_actor_capacity` reading 0 means the guard is off**, which is the code default
  (the chart ships `4`). A deployment that meant to enable it and did not looks exactly like one where nobody has hit the
  cap, and that gauge is the only thing that tells the two apart from a scrape.
- **It is inert under the shared dev principal.** With `CHEMCLAW_ENTRA_REQUIRED=false` every caller
  is one oid, so "per actor" would mean "per pod" and one client would refuse everyone; the guard
  skips itself rather than invert. A deployment fronting the API with a single service credential
  for many humans has the same problem and no such escape — the cap has nothing to divide there.
- **It is not in the access log's 429 population alone.** The refusal logs at INFO naming the
  principal, above the durable claim and the admission permit, so a refused retry costs no permit,
  no turn slot and no Postgres claim. `Retry-After` is a jittered *cadence* (built from
  `CHEMCLAW_SERVICE_TURN_ADMISSION_TIMEOUT_SECONDS`), not an estimate of when the caller's own turn
  will end — that is bounded by `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS` and can be minutes.
- **A chemist reporting "it says my budget is exhausted" is a client-side misclassification**, not
  this cap: `Chemclaw3_ui` renders a 429 *without* `Retry-After` as a terminal `budget_exhausted`.
  If you see that, something between the pod and the browser is stripping the header.

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

**The table above is about a store being *lost*, and corruption is the opposite case.** "The cache
is regenerable by definition (D-011)" is true of an empty `calculation_results` and exactly false of
a wrong one: D-011 is *why* a persisted result is never recomputed, so a value altered in place is
served for ever. Measured — one row edited by hand moved a reaction energy from −23.2 to −42.0
kcal/mol, an 18.8 kcal/mol error inside a stated ±3.0 uncertainty, and every signal stayed green:
the durable smoke test passed 5/5, the poisoned row counted as a cache **hit** (raising the hit-ratio
panel), and 0 of 13 eval metrics moved, because that baseline is 11 pinned retrieval cases and 2
live ones and touches no computed value.

Nothing in the metric plane can be made to notice this: every alert reads a counter incremented on a
failure, refusal, absence or capacity path, and a well-formed wrong answer takes the success path.
`artifact_blobs` is content-addressed and `schema_migrations` carries a checksum; `calculation_results`,
the store whose contents *are* the science, has neither. **Restoring it is not a recovery step you
can reach for, because nothing tells you to.** The controls that do apply are the ones at the point
of use: the citations a chemist checks, and a second run of the same job — two rows for one reaction
that disagree is, today, the entire detection surface.

Only one of the three needs a *point-in-time* story rather than a recent-snapshot one, and it is the
audit trail — because it is the only store where "we lost the last hour" means the answer to "who
ran that?" is gone for good, rather than merely inconvenient.

### Restoring Postgres

1. Restore to the chosen point by whatever mechanism your Postgres provider offers.
2. Re-run `make db-migrate` (or redeploy, which runs the migrate hook). A point-in-time restore
   brings `schema_migrations` back consistent with the schema, so the runner skips every recorded
   file and applies only what the restore point predates. A **logical dump** restored into a fresh
   database may arrive without a matching ledger — then follow *Replay the migrations against a
   database that already has the schema* first. If the restored database is *ahead* of the
   running image, the log says `migrate.database_ahead` (see *Roll back a release*).
3. Record the restore point and the window of audit rows it discarded in your operational log.
   Nothing in the database can tell you afterwards that they were ever there.
4. Re-run `make db-grants` if the deployment splits the database principal (see *Splitting the
   database principal*, above) — a restore can bring back a role grant state that predates it.

The other two stores need no procedure here: Temporal's in-flight history is lost by definition, and
the knowledge repo is a git repo (any clone is a backup).

## (xiv) Cut a release: pin the image to bytes

**One release that crosses both repositories, and it is the sign-off route.** The core and
`Chemclaw3_ui` are deployed separately — the UI by `oc set image` against a Deployment an operator
created (`D-2026-08-26-a-release-is-a-descriptor-and-a-target`) — and that independence holds for
every route but one. `POST /protocols/{id}/status` takes a **required** `expected_status` on a model
that forbids extra fields, so the two are incompatible in *both* directions across the boundary: an
older UI is refused 422 for omitting it, a newer UI is refused 422 for sending it to an older core.
Nothing else on either side is affected, and no other route couples them.

So deploy the pair together when crossing `D-2026-09-04-a-compare-and-set-on-the-document-is-silent-about-the-decision`.
The field is required rather than optional deliberately: an `expected_status` nobody sends makes the
compare-and-set always agree, which is a control that exists only in the docstring. The cost of that
choice is this paragraph. A chemist who hits it sees a refused sign-off and loses nothing — the
design and its `experiment_protocol_status_events` history are untouched — so the failure is loud
and safe rather than silent.

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
#    The three --set lines after the digest are refusals the chart makes until a release states them:
#    allowAnyDestination=true (or list networkPolicy.egressDestinations instead),
#    unboundedGrowthAccepted=true (or state retention.windows instead),
#    and temporal.namespace, which has no default: one Temporal namespace per release.
helm upgrade --install chemclaw deploy/helm/chemclaw -n <ns> \
  --set image.digest="sha256:<the digest from step 2>" \
  --set networkPolicy.allowAnyDestination=true \
  --set retention.unboundedGrowthAccepted=true \
  --set temporal.namespace=chemclaw-prod
```

`temporal.namespace` is not an escape hatch like the two before it — it is the value, and the chart
ships no default for it. `CHEMCLAW_TEMPORAL_ADDRESS` names a broker in the cluster-shared
`temporal` namespace, so two releases sharing one Temporal namespace share one task queue and one
schedule-id space: a peer's `helm upgrade` rewrites this release's Schedules and prunes the ones it
does not know. **Two releases need separate databases for the same reason**, and that half is not
enforceable from the chart: most tables carry no deployment discriminator, so one release's
retention sweep disposes of another's expired threads under its own window.

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

**Two gates block and one step only records**, all three in `.github/workflows/image.yml`:

| Gate | What it read | First move |
| --- | --- | --- |
| `pip-audit` | the exported lockfile — the exact versions the image installs | `uv lock --upgrade-package <name>`; reproduce locally with `make deps-audit` |
| `trivy` | the built filesystem — the base OS packages and every Python environment the base ships, which no lockfile audit can see | read the finding's path; if it is ours, fix it in `uv.lock` or `deploy/Containerfile`; if it is genuinely unfixable, an entry in `.trivyignore.yaml` with its reason and an expiry |
| SBOM step | nothing; it records | it only fails if `syft` cannot run |

`trivy` runs with `--ignore-unfixed` on HIGH and CRITICAL, and that is a deliberate narrowing
rather than an oversight: a gate that fires on every LOW in a distro base is one an operator
disables within a week, and a finding with no released fix is not something a build can act on. A
finding that genuinely cannot be fixed gets an entry in `.trivyignore.yaml` **with its reason and
an expiry in the diff** — never a downgrade of the whole gate, which is how a control becomes a
badge. Every entry there today is a package listed in pip's own vendored manifest (vendor.txt under
pip's _vendor directory) in the base image's `/opt/app-root` environment; the file carries each reason and expiry.

**A finding for a package you cannot find on disk is usually one of those.** The scanner reads
pip's vendored manifest, a text file, so a `find` for a `dist-info` directory will not locate it
(`D-2026-09-14-the-phantom-packages-were-pips-vendored-manifest`). Check the finding's `PkgPath`
before concluding the scanner is wrong.

The SBOM step runs on `main` only. The SBOM (SPDX) and the built image's digest are retained on the
run for 90 days. That is what makes
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
| `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` | expensive jobs (`expensive: true` in any `connector.yaml`) and the other privileged actions (cancelling a durable run) | comma list of role values |
| `CHEMCLAW_TOOL_ROLE_GATES` | one named tool → the roles that may call it | JSON `{"tool": ["role"]}` |
| `CHEMCLAW_TOOL_AUTHZ_DEFAULT` | every tool with no `CHEMCLAW_TOOL_ROLE_GATES` entry: `allow` (default) or `deny` | `allow` \| `deny` |
| `CHEMCLAW_SKILL_ROLE_GATES` | one skill's *visibility* — a caller holding none of its roles never sees it | JSON `{"skill": ["role"]}` |
| `CHEMCLAW_ENTRA_EXPENSIVE_ACTIONS` | anything expensive that **no** manifest declares; a bundle's own `expensive: true` needs no entry here | comma list of tool names |
| a share's `required_roles:` | one mounted document share, in its `datasource.yaml` — a caller without it gets nothing from that source, not a filtered list | list of role values |

Two defaults worth knowing before you design the role set:

- **Expensive jobs are closed by default; most other writes are not.** Every expensive action —
  each tool a bundle declares `expensive: true`, plus the ones core owns (`CORE_EXPENSIVE_ACTIONS`
  in `agent/authz.py`: development reports, memory synthesis, external-input requests, hypothesis
  tournaments) — needs a role in `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`, which the chart ships empty. So
  a fresh deployment refuses every expensive job to everyone; that is the intended failure, and
  setting the privileged role is the remedy. `DEFAULT_WRITE_TOOL_GATES` closes only the three
  knowledge-graph writers the same way. Every other tool follows `CHEMCLAW_TOOL_AUTHZ_DEFAULT`
  (`allow`), and what governs the rest of the write surface is the plan gate
  (`CHEMCLAW_HARNESS_ENABLED=true`, `CHEMCLAW_HARNESS_AUTONOMY=plan_only`): a person approves the
  plan before any state-changing tool runs
  (`D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest`). To restrict a
  specific launcher to a role, give it a `CHEMCLAW_TOOL_ROLE_GATES` entry.
- **None of it is enforced unless `CHEMCLAW_ENTRA_REQUIRED=true`** (the chart sets it; the code
  default is `false`). In dev the gates are open so the app runs without a tenant. The front door
  refuses to start that way on a non-loopback bind unless `CHEMCLAW_SERVICE_ALLOW_INSECURE=true`
  says so explicitly — never set that on a shared or exposed deployment.

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

Removing the role stops new access and deletes nothing. Per-actor rows are split into two tiers by
one rule — **the conversation is erasable, the record is not**:

```bash
make user-erase ACTOR=<entra-oid>            # dry run: real counts, writes nothing
make user-erase ACTOR=<entra-oid> APPLY=1    # commits
```

It removes their sessions, messages, events, turn lease, preferences and watch subscriptions. It
**keeps and counts** the rows that attribute scientific work to them, and prints the reason beside
each.

**Do not enumerate either tier from this page — run the dry run and read what it prints.** The
tables are `agent/leaver.py`'s `_ERASE`, `_RETAINED` and `_RETAINED_IN_PAYLOAD`, and the report
above names every one of them with its row count, plus the reason for each retained table and a
third section for the tables it can neither clear nor count. Neither tier is short, and the retained
one is not only the audit trail: it also holds the durable-effect, pending-request, BO-campaign and
experiment-protocol tables, each of which names a person, and one row in that tier records a
delivery to an external results store this system cannot erase from at all. A data-protection answer
assembled from a list in a document is an answer about the commit that document was written on.

That is not a limitation to work around: an attributable record that can be deleted on request is
not an attributable record, and for a tool call that changed nothing durable the trail is the only
place it is recorded at all. The application credential cannot delete from
`audit_events` either — the grant withholds DELETE (see *Splitting the database principal*). If a
data-protection obligation reaches the retained tier, that is a decision to take with the record's
owner.

The dry run executes the deletes and rolls back, so the number you sign off on is the number that
will be deleted rather than a second query's guess at it.

**If a turn was running while it swept, the run says so and exits `2`.** The command claims each of
that person's sessions before it touches anything and refuses the whole run while one is busy — but
a lease can lapse, a session can be created between the enumeration and the commit, and a deployment
that does not run the Postgres session store takes no durable claim at all. In any of those a turn
can commit rows *after* the sweep, under a session whose ownership row is already gone, and every
actor-scoped route in this system finds a session through that row. **Re-running the erasure is
therefore not the remedy** — it reaches nothing, and it prints zeros, which reads as success.

The run prints the session ids and the command that clears them:

```bash
python -m chemclaw.cli.erase_actor --finish <session-id> [<session-id> ...]           # dry run
python -m chemclaw.cli.erase_actor --finish <session-id> [<session-id> ...] --apply   # commits
```

Stop the turn first (`POST /sessions/{id}/turn/stop`), or the same thing happens again. This form
deletes by session id, which is a route that never reads the ownership row, and it **refuses any
session that still has one** — so it can only finish what is already orphaned and is not a way to
delete somebody's live conversation. It runs on the application's own privileges: nothing here needs
a database owner. Like the actor form it exits `2` if it wrote and did not finish, and its report
names what it deliberately leaves behind.

## (xvi) Attach an external results database

Every calculation this system performs is projected into a typed scientific record and delivered to
a database **it does not own** (`D-2026-08-25-a-cache-is-not-a-record`). Publishing is **off until
you attach one**: `CHEMCLAW_RESULT_SINKS` is empty by default, and with no sink named the enqueue
costs one list lookup and no database work at all.

**1. Create the schema.** This system never holds DDL privileges on the store it publishes to, so
apply the DDL with a principal that does — the same split `postgres_migration_dsn` and
`postgres_dsn` already make for this system's own database.

```
uv run python -m chemclaw.cli.sink_schema --all > results-schema.sql   # DDL + registry seed, in order
psql "$RESULTS_ADMIN_DSN" -v ON_ERROR_STOP=1 -f results-schema.sql
```

(`make sink-schema` runs the same module, but `make` echoes the command line onto stdout, so use
`make -s sink-schema > …` if you redirect it.)

The seed is *generated* from `chemclaw.publish.properties` and `chemclaw.publish.solvents` rather
than checked in, because those are what the writer canonicalizes against: a seed file that had
drifted from them would build a database whose foreign keys reject rows this system considers valid.
Re-run it after any upgrade — the inserts are idempotent, and a new calculator ships registry rows
rather than migrations.

**2. Point a sink at it.** The shipped manifest, `src/chemclaw/publish/sinks/postgres/sink.yaml`,
addresses host `chemclaw-results`, port `5432`, database chemclaw_results (its `database:` key) and reads its
credentials from `RESULTS_DB_USER` / `RESULTS_DB_PASSWORD`. Either make your database answer at
that address (for example a Service of that name), or copy the folder, edit `host:`/`database:`,
and put your copy first on the discovery path — sinks are discovered as `<dir>/<name>/sink.yaml`
and the earlier directory wins a name collision, so your address is not a change to this
repository:

```
CHEMCLAW_RESULT_SINKS_DIR=/etc/chemclaw/sinks     # holds postgres/sink.yaml; os.pathsep list
CHEMCLAW_RESULT_SINKS=postgres
RESULTS_DB_USER=... RESULTS_DB_PASSWORD=...       # the target's own credentials,
                                                  # unprefixed: not settings of this system
```

On the chart: `CHEMCLAW_RESULT_SINKS` goes in `config:`; the two credentials go in your Secret and
are mapped under `secrets.optionalKeys`; a site that cannot use the shipped address puts its edited
folder in a ConfigMap and lists it under `extraSinks.sinks` (`name: postgres`, `configMap: <it>`),
which mounts it on every pod and sets `CHEMCLAW_RESULT_SINKS_DIR` with that folder first. **And the
destination must be allowed out**: a sink's host is manifest-supplied, so nothing derives it — add
it to `CHEMCLAW_EGRESS_ALLOW` (bare host) and add a peer for it to
`networkPolicy.egressDestinations` (a port other than `networkPolicy.egressPorts.postgres` needs an
entry there too), or every delivery is refused (`ChemclawEgressRefused`, then `ChemclawResultPublishFailing`).

The manifest names *environment variables*, never values; they are read at connect time, so a
rotated secret is picked up by the next connection rather than the next deploy. `make sink-validate`
checks that the driver resolves and takes its config, and it runs in CI. A name in
`CHEMCLAW_RESULT_SINKS` that no manifest declares is a startup error.

**3. Backfill.** Publishing hooks a calculation as it completes, so a store attached to a running
deployment would otherwise receive only what is computed from that moment on — while
`calculation_results` and `job_records`, neither ever pruned, hold everything before it.

```
python -m chemclaw.cli.backfill_publications --dry-run    # what would be queued
python -m chemclaw.cli.backfill_publications              # queue it
```

Safe to run twice and safe to run live: the outbox's identity index makes a second pass a no-op.
A chemist can do the same thing as a durable job (`republish_calculations`, the `results` bundle).

**Watching it.** Each drain pass (the `result-publish` Schedule, every
`CHEMCLAW_RESULT_PUBLISH_SCHEDULE_MINUTES`) publishes three gauges per sink:
`chemclaw_outbox_pending` (depth), `chemclaw_outbox_oldest_pending_seconds` (age) and
`chemclaw_outbox_dead_lettered` (given up). Read those, not `chemclaw_results_queued_total` minus
`chemclaw_results_published_total` — dead-lettered rows never leave that difference, so it is not
a backlog. A growing age means the destination is down or too slow;
`chemclaw_result_publish_failures_total` and `result_publications.last_error` say which. The
alerts are `ChemclawResultOutboxStuck`, `ChemclawResultPublishFailing`,
`ChemclawResultsDeadLettered` and `ChemclawOutboxBacklogUnreported`.

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
   `CHEMCLAW_VERIFIER_ENABLED=true`, the front door's lifespan runs one structured-output probe
   against the routed `"verifier"` model
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

## (xvi-c) Artefacts: turn them on, sandbox their html, keep them bounded

Artefacts are the versioned working documents a session shows beside its chat — a plan, a table,
structures, a chart, a 3D geometry, an html page, a pinned tool result (`src/chemclaw/exhibits/`,
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`). The agent writes them with
`create_exhibit`/`revise_exhibit`/`read_exhibit`; a chemist edits, pins and exports them over
`/sessions/{id}/exhibits`. They are **on by default**, and so are html pages and their scripts.

**1. The switches.** Both in `config:` (`values.yaml` carries the commented block):

| Setting | Off means |
|---|---|
| `CHEMCLAW_AGENT_EXHIBITS_ENABLED` | the three tools are unbound from every turn (their prefix is paid back) and `GET /sessions/{id}/exhibits` answers `enabled: false`, so the UI shows what a session already holds **read-only**, with no new-artefact UI; a message carrying `exhibit_refs` is a 422 `invalid_exhibit_ref` |
| `CHEMCLAW_AGENT_HTML_ARTEFACTS_ENABLED` | a *new* html artefact is refused (422 over REST, a worded refusal to the model, and `create_exhibit` stops offering the kind); existing ones still list, read and revise; the listing says `html_enabled: false` |

Whether an html page's **scripts** run is not a backend switch — it is the UI's
`HTML_SCRIPTS_DEFAULT` (step 2).

**2. The UI's html sandbox — a second origin, and it is a deployment step.** An html page never
runs on the API origin (no route here answers `text/html` for an artefact). It renders in the UI's
**second listener** on its own origin, inside an opaque-origin frame — `Chemclaw3_ui`'s README,
"HTML sandbox", is the reference and its `deploy/openshift/` the example manifests. What the release
owes it:

- **A separate hostname, a Route with its own TLS, and a Service port to the UI's `SANDBOX_PORT`**
  (default `8081`, never `PORT`). Prefer a host under a *separate registrable domain* from the app,
  so the two are different sites as well as origins. Nothing in front of the sandbox host may
  authenticate or rewrite headers: no oauth-proxy, no added `X-Frame-Options`, the page's own CSP
  passed through untouched.
- **`APP_ORIGIN` and `SANDBOX_ORIGIN` exactly as the browser types them** — scheme, host and port.
  The shell takes content only from `APP_ORIGIN`; a chemist who reaches the app at any other address
  sees the page as escaped source with a notice naming both origins. Unset `SANDBOX_ORIGIN` and
  every html artefact is shown as source ("HTML preview needs a separate sandbox origin"). The BFF
  logs one line at startup, `html sandbox on: …` or `html sandbox off: …` — read it.
- **Scripts run by default** (owner decision,
  `D-2026-10-03-model-written-html-runs-its-scripts-by-default`); a viewer can **Disable scripts**
  per view. **`HTML_SCRIPTS_DEFAULT=off` on the UI is the kill switch** — a UI configuration
  change, no backend release — after which nothing runs until somebody presses **Run scripts**.
- **Apply the browser policy on every managed browser**, because CSP cannot stop WebRTC: on Chrome
  and Edge `WebRtcIPHandling=disable_non_proxied_udp` (this *reduces* the exposure — a page can still
  relay over TURN/TCP through a proxy), on Firefox `media.peerconnection.enabled=false` (this removes
  WebRTC). What is left with scripts on is the stated residual risk: egress of what the page holds
  where the policy is not applied, and a clipboard write after a click — never the session, the
  transcript or another artefact.

**3. Deploy order: backend and migrations before the UI.** The core chart runs migrations
115–119 as its pre-upgrade hook, and the UI reads shapes only this backend serves (a geometry's
`structure_id`, `tool_failed.call_id`, the sandbox `ready` handshake). `Jenkinsfile.release` applies
`core` before `ui` and nothing reorders it; by hand, `helm upgrade` the core release, wait for it to
be ready, then roll the UI. The UI must be `Chemclaw3_ui#138` or later — the release that renders
bound values and html artefacts in its sandbox — and a UI built for this backend's hardening shapes
needs this backend first.

**4. Sizing.** Every write is bounded by the `CHEMCLAW_EXHIBIT_MAX_*` caps (`.env.example` names
each): the spec's bytes (checked before any binding resolves), rows, structures, points, atoms, the
html page, artefacts per session and revisions per artefact. Two costs sit in the **front door's
memory** and are worth sizing the pod for:

- a geometry citing a calc artifact, its `.xyz` export and `GET /calc-artifacts/content` read the
  blob **decompressed whole** — bounded by `CHEMCLAW_CALC_ARTIFACT_MAX_DOWNLOAD_BYTES`, decided from
  the recorded size before the read;
- the binding cache (`CHEMCLAW_EXHIBIT_BINDING_CACHE_BYTES`, stored bytes, several times that once
  parsed) is **per process**: every front-door replica holds its own.

**5. Retention.** State `retention.windows.CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS` — the chart
refuses windows that leave it out unless `retention.exhibitsGrowthAccepted: true` says artefacts are
kept for the deployment's lifetime. **Upgrade step: a release whose values set `retention.windows`
without the exhibits window or `retention.exhibitsGrowthAccepted` now refuses to render** —
`helm upgrade` stops on "retention: this release states retention windows and must say how long
artefacts are kept"; add one of the two to the values file before upgrading. While it is 0 a session holding an artefact is never forgotten
by the ownership sweep either. **Keep `CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS` at least as long if a
bound value must stay readable**: a `$bind` cell reads its stored tool result on every read, and
once that window sweeps the result the cell reads empty. A person's push to the session's other
tabs expires after `CHEMCLAW_EXHIBIT_PUSH_RETENTION_HOURS` (default 24) through its own schedule,
`exhibit-pushes`, wherever the session store is Postgres, whatever the windows say
(`D-2026-10-03-an-artefact-push-expires-on-its-own-schedule`).

**6. The upgrade that brings artefacts moves the context budget, and a site pin holds it back.**
The three tools' schemas are prefix on every model call, and every stored tool result now ends with
one line, `⟨r:<12 hex>⟩` — the handle a binding names it by (a model that quotes it is quoting an
address, not a figure; no grounding check reads it as one). The shipped budgets were re-derived for
both (`D-2026-10-02-the-artefact-prefix-is-paid-from-the-window-margin`,
`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`). **Remove any site pin of
`CHEMCLAW_AGENT_CONTEXT_TOKEN_BUDGET`, `CHEMCLAW_AGENT_CONTEXT_PREFIX_BASIS`,
`CHEMCLAW_AGENT_MAX_TOOL_RESULT_CHARS` or `CHEMCLAW_GATHER_EVIDENCE_MAX_CHARS`** so the derived
defaults apply — or re-derive the pinned values from `core/config/agent.py`'s arithmetic against
this release's `tests/test_context_floor.PREFIX_BOUND`. A pin left from the previous release keeps
the old budget under a larger prefix, and the thread pays the difference.

**7. Monitoring.** On the *Tools and model* dashboard, **Artefact writes and refusals**:

- `chemclaw_exhibit_writes_total{author_kind,op}` — every revision written, agent or human, created
  or revised;
- `chemclaw_exhibit_refusals_total{reason}` — `invalid` (a 422 or a worded refusal: spec, binding,
  citation, cap), `stale_revision`, `exhibit_limit`. A climbing `invalid` with flat writes is a model
  that cannot write the shape it is offered.

Log events: `exhibit.created` / `exhibit.revised` (fields `exhibit_id`, `session`, `actor`,
`author_kind`, `kind`, `revision`) for every write on every path; `report.exhibit_skipped` with a
`reason` (`memory`, `session_gone`, `not_a_participant`, or the refusal's class) when a development
report could not be shown as an artefact — the report note is the result either way; and
`retention.exhibit_pushes` from the push prune. A push or a structure read that failed is counted on
`chemclaw_degraded_total{subsystem="exhibits"}`, which is what fires
**`ChemclawSubsystemDegraded{subsystem="exhibits"}`**: the write committed and a tab missed its push
(it refetches on focus), or a geometry's structure could not be read and showed as unavailable.

**8. Troubleshooting.**

| Symptom | What it means |
|---|---|
| 409 `{"code": "exhibit_limit"}` | the session holds `CHEMCLAW_EXHIBIT_MAX_PER_SESSION` artefacts, or the artefact `CHEMCLAW_EXHIBIT_MAX_REVISIONS` revisions. Revise an existing one, or carry on in a new one from its head; raising the cap is a sizing decision |
| 409 `{"code": "stale_revision", "head_revision": N}` | the edit was made against an older revision — somebody (or the agent) revised it first. The UI refetches and re-applies; nothing was lost |
| 422 `{"code": "invalid_exhibit_ref"}` on `POST …/messages` | an `exhibit_refs` entry names an artefact this session does not hold or a revision it lacks, or artefacts are off. (More references than `CHEMCLAW_EXHIBIT_MAX_REFS` is a plain validation 422.) |
| `GET /calc-artifacts/content` 404 | the reference is malformed, names nothing, or the blob was evicted between the listing and the read. An artefact citing it still reads; its `.xyz` export is a 404 too |
| `GET /calc-artifacts/content` 413 | the artifact is over `CHEMCLAW_CALC_ARTIFACT_MAX_DOWNLOAD_BYTES`, judged before reading. Raise the cap only with the front door's memory in mind (step 4) |
| a bound cell reads empty, `bindings[].ok: false` "no longer stored (retention swept it)" | the tool result it was bound to went with `CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS`. The artefact still reads and revises (the binding is carried); detach the value to keep it. Avoid it with step 5 |
| a geometry shows no structure, `bindings` entry `{path: "xyz", tool: "structure", ok: false}` | its `structure_id` no longer resolves in the structure store, or the store could not be read (then `ChemclawSubsystemDegraded{subsystem="exhibits"}` fired) |
| html artefacts show as source | the UI's sandbox is off or the page is opened at an address other than `APP_ORIGIN` — read the UI's `html sandbox` startup line |

## (xvii) The other commands with no section of their own

**Running a `make` target against a deployed release.** The image carries no `make` and no
`Makefile`; every operator target is a thin wrapper over `uv run python -m <module>`, and the image
has that venv on `PATH`. So run the module in a pod that already holds the release's configuration
and credentials — the background worker:

```
oc -n <ns> exec -it deploy/chemclaw-background-worker -- python -m chemclaw.cli.erase_actor <oid>          # make user-erase
oc -n <ns> exec -it deploy/chemclaw-background-worker -- python -m chemclaw.cli.explain <session-id>       # make explain
oc -n <ns> exec -it deploy/chemclaw-background-worker -- python -m chemclaw.cli.sync_share <source>       # make share-sync
```

`grep -A3 '^<target>:' Makefile` shows the module and flags for any other target. Migrations and
grants are the exception: they need the migration principal, so apply them by redeploying (the
`chemclaw-migrate` hook Job), not by `exec`.

Three operations exist as `make` targets and have no section of their own. Two are yours to run;
the third is automated and listed so nobody runs it by hand wondering why.

**`make reindex-full` — rebuild the derived note index from scratch.** `make reindex` (and the
hourly workflow) re-embeds only notes whose stored fingerprint changed; `reindex-full` ignores the
stored fingerprints. Use it after restoring Postgres without the index, or when an embedding change
left stored vectors that no fingerprint flags.

**`make synthesize KIND=campaign|playbook|optimization|observation-promotion [FRESH=1]` — run a
memory-synthesis miner now.** No Schedule mines knowledge on a timer; these run on demand and write
agent-authored notes into `knowledge/` (§(ix)). It is a durable job on `background-jobs` with a
once-a-day id, so a second run the same day rejoins the first; `FRESH=1` forces a new run when
today's predates the corpus change you care about (an import or backfill that just finished).

**`make share-sync SHARE=<source>` — crawl a mounted document share now.** The scheduled job is the
production path (`CHEMCLAW_DOCUMENT_SYNC_SCHEDULE_MINUTES`, 360 by default); this is for the first crawl after attaching a share,
and for re-crawling after a bulk change nobody wants to wait six hours for. Run
`make share-estimate SHARE=<source>` first — it walks the share, reads nothing, and tells you what
the crawl would cost.

**`make schedules-apply` — do not run this by hand in a chart deployment.** The chart runs it as
the `chemclaw-schedules` post-install/post-upgrade hook Job, so the Schedules follow the deployment
automatically; if one is missing, re-run `helm upgrade` (see `ChemclawRetentionNotSweeping` in
§(x-c)). Run it yourself only against a local `make up` stack.
