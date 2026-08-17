# infra/sql · infra/live · deploy · .github/workflows · Makefile · pyproject.toml — security & hardening

Round 1, fresh-eyes audit. Lens: what untrusted input reaches, what fails open, what leaks.
Everything below was read in full and, where a claim was checkable, run against the live Postgres
in this sandbox (`pg_isready 127.0.0.1:5432 — accepting connections`) or against `helm template`.

---

## `db-grants` reports success while reconciling nothing — the append-only audit grant fails open on any role not literally named `chemclaw_app`

- **Severity**: high
- **Location**: `infra/sql/grants/app_privileges.sql:29-35` (`app_role CONSTANT TEXT := 'chemclaw_app'` + the `RETURN` on a missing role); `src/chemclaw/core/grants.py:apply_grants`; `deploy/helm/chemclaw/templates/migrate-job.yaml:55-59` (`… && python -m chemclaw.core.grants`)
- **Trigger**: A deployment that splits the database principal — i.e. sets
  `secrets.migrationKeys.postgresMigrationDsn` / `CHEMCLAW_POSTGRES_MIGRATION_DSN` — and creates its
  runtime role under any name other than the string `chemclaw_app`. The role name is a hardcoded SQL
  literal; it is not a chart value, not a setting, and appears nowhere in `values.yaml` except inside
  a prose comment on line 541. Nothing in the chart, the Job, the Makefile target or the runner
  compares the DSN's role to that literal.
- **Consequence**: the `DO $$ … $$` block takes the `RAISE NOTICE … RETURN` branch and applies
  **zero** privileges. The runtime role keeps whatever an operator's bootstrap gave it — typically
  `GRANT ALL ON ALL TABLES` — so the credential that serves a chat turn can `UPDATE` and `DELETE`
  `audit_events`. The file's own header states this is the entire guarantee now that the hash chain
  and anchors are gone ("Those are gone now, so this file is the whole of the guarantee"). The
  failure is silent in both channels that could report it: the migrate Job's command exits 0 and the
  runner prints `applied grants: app_privileges.sql`, and the psycopg NOTICE is never surfaced.
  There is no way, from any log or exit code, to tell "single principal on purpose" (the supported
  no-op) from "split principal, role misnamed" (the control switched off).
- **Evidence**: reproduced end to end against the live database. `chemclaw_runtime` stands in for a
  plausibly-named operator role:

  ```
  $ psql -c "CREATE ROLE chemclaw_runtime LOGIN PASSWORD 'x';
             GRANT USAGE ON SCHEMA public TO chemclaw_runtime;
             GRANT ALL ON ALL TABLES IN SCHEMA public TO chemclaw_runtime;"
  BEFORE grants reconciliation:
  DELETE INSERT REFERENCES SELECT TRIGGER TRUNCATE UPDATE

  $ uv run python -m chemclaw.core.grants
  applied grants: app_privileges.sql
  grants exit=0

  AFTER reconciliation, chemclaw_runtime on audit_events:
  DELETE INSERT REFERENCES SELECT TRIGGER TRUNCATE UPDATE

  --- can it actually rewrite the trail? ---
  (as chemclaw_runtime)
    update audit_events set actor='someone-else', outcome='denied' where actor='victim';
    delete from audit_events where actor='someone-else';
  rows left: 0
  ```

  A row inserted under actor `victim` was rewritten and then deleted by the runtime credential,
  after a "successful" grant reconciliation. `tests/test_database_privileges.py` does not catch this
  — by design it compares two files in the repo and touches no database, so it cannot see whether
  the matrix was ever applied to a real role.
- **Fix**: make the no-op conditional on the *intent* the deployment already signals rather than on
  the role's existence alone. Concretely: pass the runtime role name in (`SET chemclaw.app_role`, a
  psql `\set`, or a settings key `postgres_app_role` read by `core/grants.py` and substituted with
  `format(%I)`), and make `apply_grants` raise when a migration DSN distinct from `postgres_dsn` is
  configured but the named role does not exist — the same "an empty directory is an error, not a
  successful no-op" argument that module already makes for missing grant files. At minimum, surface
  the NOTICE: `conn.add_notice_handler(...)` and fail the Job if the block reports it skipped.

---

## Every credential is mounted into every container, including the git push token on connector pods and the two sync sidecars

- **Severity**: medium
- **Location**: `deploy/helm/chemclaw/templates/_helpers.tpl:32-77` (`chemclaw.env`, included unconditionally by every Deployment, init container, sidecar and Job); `deploy/helm/chemclaw/values.yaml:486-532`
- **Trigger**: `helm template chemclaw deploy/helm/chemclaw` with shipped values — no override needed.
- **Consequence**: `CHEMCLAW_LLM_API_KEY`, `CHEMCLAW_HPC_API_TOKEN`, `CHEMCLAW_POSTGRES_DSN`,
  `CHEMCLAW_KNOWLEDGE_REPO_TOKEN` (a **git push** credential for the knowledge repository),
  `CHEMCLAW_NOTE_WEBHOOK_SECRET` (the HMAC key that authenticates `/events/knowledge-merged`),
  `CHEMCLAW_FRAMING_ENVELOPE_SECRET` and the three connector bearer tokens land in the environment of
  **16 containers**, including six connector pods and the four `knowledge-sync` / `note-repo-init`
  containers whose entire job is `git clone` + `rsync`. Any RCE or dependency compromise in the
  `molfp` fingerprint server — a pod with no reason to hold any of them — yields the LLM key, the
  HPC token, the database DSN and a push credential to the knowledge repo.

  The chart *knows* this shape is the exposure: `values.yaml:533-537` justifies `migrationKeys` as a
  separate map precisely because "`chemclaw.env` mounts every key in that map onto every pod, so a
  migration credential listed there would be present on the front door and every worker for the life
  of the deployment — which is precisely the exposure this exists to remove". The same reasoning was
  applied to exactly one credential. And `_helpers.tpl:384-391` states the opposite of what it
  renders: "Every component that can call `propose_note` needs one … but **NOT** a connector's own
  worker — a bundle returns its note in the job envelope and core publishes it, so no connector
  process touches the note repo." The note repo's *volume* is correctly withheld from connector
  pods; its *push token* is not.
- **Evidence**: rendered with `helm template` (helm v3 present in this sandbox), one line per
  container listing its `secretKeyRef` env names:

  ```
  Deployment  chemclaw-connector-molfp      connector-molfp        CHEMCLAW_CALC_TOKEN,CHEMCLAW_CHEM_TOKEN,CHEMCLAW_FRAMING_ENVELOPE_SECRET,
                                                                   CHEMCLAW_HPC_API_TOKEN,CHEMCLAW_KNOWLEDGE_REPO_TOKEN,CHEMCLAW_LLM_API_KEY,
                                                                   CHEMCLAW_NOTE_WEBHOOK_SECRET,CHEMCLAW_POSTGRES_DSN,CHEMCLAW_SAFETY_TOKEN
  Deployment  chemclaw-connector-rxnfp      connector-rxnfp        (identical)
  Deployment  chemclaw-connector-worker-qm  connector-worker-qm    (identical)
  Deployment  chemclaw-service              knowledge-sync-init    (identical)
  Deployment  chemclaw-service              knowledge-sync         (identical)
  Deployment  chemclaw-background-worker    note-repo-init         (identical)
  Job         chemclaw-schedules            schedules              (identical)
  …16 containers, all with the same nine keys
  ```
- **Fix**: split `chemclaw.env` the way `chemclaw.migrationEnv` was already split — a base helper
  with the keys every process needs (`postgresDsn`, `framingEnvelopeSecret`) plus per-role helpers:
  `chemclaw.knowledgeEnv` (`knowledgeRepoToken`, for the note-repo/knowledge-sync containers and the
  two components that call `propose_note`), `chemclaw.llmEnv` (`llmApiKey`, front door + background
  worker), `chemclaw.hpcEnv` (`hpcApiToken`, the `qm` worker), `chemclaw.webhookEnv`
  (`noteWebhookSecret`, front door only), and the three connector bearer tokens only on the pods that
  dial those siblings. `tests/test_helm_chart.py` already pins the key set; extend it to pin the
  key→component matrix so a widening is a deliberate test edit.

---

## `readOnlyRootFilesystem` is disabled fleet-wide for a dependency the image no longer contains

- **Severity**: medium
- **Location**: `deploy/helm/chemclaw/values.yaml:627-631`; `deploy/helm/chemclaw/templates/_helpers.tpl:288-294` and `:301-307` (`chemclaw.containerSecurityContext`)
- **Trigger**: any `helm install`/`upgrade` with shipped values.
- **Consequence**: every container in the chart renders `readOnlyRootFilesystem: false`. The only
  stated reason is provably dead: both comments say *"the calculation workers shell out to xtb/crest,
  which need writable scratch; with no scratch volume provisioned this trades an admission failure
  for a runtime failure"*. `deploy/Containerfile:58-69` records that those binaries were removed
  ("**No calculation binaries** … Nothing in `src/` invokes either binary now"). Verified: the only
  `subprocess.*` call anywhere in `src/` is `src/chemclaw/cli/live_storm.py:233` (a local test
  harness), and no string literal `"xtb"`/`"crest"` is used as an executable name anywhere in `src/`.
  So the chart keeps a hardening control off, on every pod, for a caller that does not exist — and
  the justification will keep reading as current to the next reviewer, since it is written in the
  present tense in two files.
- **Evidence**:

  ```
  $ grep -rnE 'subprocess\.(run|Popen|check_)|os\.exec|os\.system' src/ --include=*.py
  src/chemclaw/cli/live_storm.py:233:    completed = subprocess.run(

  $ grep -rnE '["'\'']((xtb)|(crest))["'\'']' src/ --include=*.py
  src/chemclaw/connectors/calc/server/tools.py:233:  calc_type: Restrict to one kind of calculation, e.g. "xtb", …   # a docstring
  ```

  The processes that *do* write to disk write into mounted volumes (`note-repo` and
  `knowledge-checkout` emptyDirs) or into `/tmp`, not into the image layers.
- **Fix**: flip the default to `readOnlyRootFilesystem: true`, add an `emptyDir` at `/tmp` to the pod
  specs (`git`, `uv`/Python and `knowledge-sync.sh`'s `GIT_ASKPASS` all need it), and delete the
  xtb/crest sentence from both files. If a staged rollout is preferred, at minimum rewrite the
  justification so it names a live caller — a comment asserting a dead dependency is what let this
  survive the physics move.

---

## The four-repo e2e harness binds two mock services to every interface, in a lane whose every other process is pinned to loopback

- **Severity**: low
- **Location**: `infra/live/e2e-full-stack/up.sh:119-120` (`--host 0.0.0.0 --port 8090`) and `:126` (`MOCK_MCP_VENDOR_HOST=0.0.0.0 MOCK_MCP_VENDOR_PORT=8091`); manifest at `infra/live/e2e-full-stack/manifests/mock-vendor/connector.yaml:11,17,29-33`
- **Trigger**: `make live-e2e-full-stack` on any host with a routable interface (a shared dev box, a
  CI runner on a flat network, an agent container with a bridged NIC).
- **Consequence**: two unauthenticated-or-weakly-authenticated services become reachable from the
  network. Port 8091 (`mock-vendor`'s MCP endpoint) "runs no bearer check at all" by the manifest's
  own admission — the manifest declares `auth: mode: none` and justifies it *because the URL is
  loopback*, which the launcher then contradicts. Port 8090 carries the ELN export corpus and the HPC
  mock, gated only by `MOCK_HPC_API_TOKEN` defaulting to the literal `mock-hpc-token`. Everything
  else in the same lane is deliberately loopback-only — `infra/live/processes.sh:44` pins
  `CHEMCLAW_SERVICE_HOST=127.0.0.1` and its comment explains the SEC-2 boot refusal that enforces it
  — so this is an inconsistency inside one harness, not a house style.
- **Evidence**: `up.sh:119-120`

  ```bash
  start mock-hpc-eln bash -c \
    "cd '$MOCK_REPO' && exec '$python' -m uvicorn app.main:app --host 0.0.0.0 --port 8090"
  ```

  against `connector.yaml:17`: *"Deterministic and in-memory — no real vendor, **no network call
  beyond this loopback server**"* and `:29-31`: *"`vendor_server.py` runs no bearer check at all …
  and the URL above is loopback, so `HttpEndpoint`'s 'a networked endpoint carries a credential' rule
  does not fire."* The credential rule is being satisfied by a loopback claim the launcher does not
  honour.
- **Fix**: `--host 127.0.0.1` for 8090 and `MOCK_MCP_VENDOR_HOST=127.0.0.1` for 8091, matching
  `processes.sh`. The manifest's `auth: mode: none` exemption is then true rather than aspirational.

---

## `image.yml` grants `actions: write` to a `pull_request`-triggered job that runs PR-supplied code, with no consumer for the permission

- **Severity**: low
- **Location**: `.github/workflows/image.yml:25-27`, `:23` (`on: pull_request`), `:38` (`actions/checkout@v4`, default `persist-credentials: true`)
- **Trigger**: any pull request against this repository.
- **Consequence**: the job runs code the PR controls (`make deps-audit` resolves and executes
  `uvx pip-audit` against the PR's `uv.lock`; `docker build -f deploy/Containerfile` executes the
  PR's `RUN` steps; the smoke step shell-interpolates `basename` of PR-created directories under
  `src/chemclaw/connectors/*/` straight into `python -c "import ${module}"`). `actions/checkout`
  leaves the `GITHUB_TOKEN` in `.git/config`, so anything running on the runner can read it with the
  workflow's declared scopes. `actions: write` permits deleting artifacts, cancelling and re-running
  workflow runs — i.e. tampering with the very supply-chain evidence this workflow exists to produce
  (the SBOM and digest it uploads). Nothing in the workflow uses the Actions API:
  `actions/upload-artifact@v4` authenticates with `ACTIONS_RUNTIME_TOKEN`, not `GITHUB_TOKEN`.
  GitHub caps fork-PR tokens to read on public repositories, which bounds the exposure but does not
  cover same-repo branch PRs or a private/internal repository.
- **Evidence**: `image.yml:25-27`

  ```yaml
  permissions:
    contents: read
    actions: write
  ```

  and the only token-adjacent step is the upload at `:147-154`. `ci.yml:21-22` correctly declares
  `contents: read` alone for a job doing strictly more.
- **Fix**: drop `actions: write` (leave `contents: read`), and add
  `with: {persist-credentials: false}` to the `actions/checkout@v4` step in both workflows — neither
  runs a git operation that needs the credential after checkout.

---

## CI installs two executables over the network with no integrity check, one of them `curl | sh`

- **Severity**: low
- **Location**: `.github/workflows/ci.yml:165-170` (kubeconform); `.github/workflows/image.yml:132-138` (syft)
- **Trigger**: every CI run.
- **Consequence**: `kubeconform` is fetched from a GitHub release asset, untarred and
  `sudo install`ed with no checksum or signature — release assets are replaceable by anyone with
  write access to that repository, and the tarball is fetched over the network into a job that then
  runs it as root. `syft` is installed by piping a script fetched from `raw.githubusercontent.com` at
  a **git tag** into `sh -s -- -b /usr/local/bin`; a tag is mutable, so the pin names a moving
  target. The workflow's own comment argues at length that the pin *resolves* (`curl -sSfI` first),
  which checks existence, not content. This is the same class of gap the SBOM step exists to close,
  in the step that produces the SBOM.
- **Evidence**: `image.yml:135-138`

  ```bash
  curl -sSfI "https://raw.githubusercontent.com/anchore/syft/${SYFT_VERSION}/install.sh" \
    >/dev/null || { echo "syft pin ${SYFT_VERSION} does not resolve - fix SYFT_VERSION"; exit 1; }
  curl -sSfL "https://raw.githubusercontent.com/anchore/syft/${SYFT_VERSION}/install.sh" \
    | sh -s -- -b /usr/local/bin
  ```

  and `ci.yml:167-170`, which has no verification step at all.
- **Fix**: pin both by SHA-256 and verify before executing (`echo "<sha>  file" | sha256sum -c -`),
  or use the maintained actions (`anchore/sbom-action`, `docker://ghcr.io/yannh/kubeconform`) pinned
  by commit SHA rather than by tag.

---

## Checked and found sound (no finding)

Recording these so a later pass does not re-derive them:

- **SQL injection / dynamic SQL in `infra/sql/`.** All 46 migrations are static DDL. The only
  `format()`/`EXECUTE` is in `grants/app_privileges.sql`, and every interpolation is `%I` over a
  module-level constant — correct identifier quoting, no user input on the path. No
  `SECURITY DEFINER` function, no trigger, no `ALTER DEFAULT PRIVILEGES`, no row-level security
  needed given single-tenant scoping is enforced in the application.
- **Ownership scoping in the schema.** `tool_result_links` is keyed `(session_id, content_hash)` and
  `session_owners` carries `owner` and `profile`, so the read routes have a session to scope by
  rather than a bare content ref. `plan_approvals` is keyed `(session_id, plan_hash)`, which closes
  the "approve a modest plan, then rewrite it" path at the schema level. The schema supports the
  gates; whether the routes use them is another slice's call.
- **The grant matrix itself.** `tests/test_database_privileges.py` really does derive the write verbs
  from the SQL literals in `src/`, folds in the two dynamic table-name sites and the LangGraph tables
  (read off the *installed* distributions, not assumed), and fails in both directions. It runs
  without a database. `4 passed in 0.73s` here. The matrix withholds `UPDATE`/`DELETE` on
  `audit_events` and `DELETE` on the cache/job/campaign tables as claimed. Its weakness is only that
  it cannot see whether the file was ever applied — finding 1.
- **`/metrics` on the public Route.** The chart's claim that a NetworkPolicy does not contain it is
  correct and is stated correctly in all three places now; the compensating control it names
  (`test_metrics_carry_no_identifiers_or_turn_content`, `tests/test_metrics.py:99`) exists.
  `route.ipWhitelist` renders only when set, and renders the HAProxy annotation the router parses.
- **`entra_required=false` on a non-loopback bind.** `api/middleware.py:106-135` raises rather than
  warns, and the bind address the guard reads (`settings.service_host`) is the same variable
  `deploy/entrypoint.sh:34` passes to `--host`, so the check cannot disagree with the actual bind.
- **Explicit `env` vs `envFrom` precedence.** `LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2`,
  `CHEMCLAW_COMPONENT` and the secret refs are all explicit `env` entries, which win over the
  ConfigMap's `envFrom` — so a key smuggled into `.Values.config` cannot re-enable LangSmith egress
  or redirect a component's dispatch.
- **`deps-audit` classification (`Makefile:186-235`).** The "found" pattern is tested first and never
  excused, the output is classified from a shell variable rather than re-read from a file, the scratch
  file is an `mktemp`, and an unreachable advisory database is a hard failure under `CI`. A future
  pip-audit wording change fails closed (`exit $rc`), not open.
- **Egress NetworkPolicy default (`egressDestinations: []` → `to: []` → anywhere).** Genuinely
  permissive, but it is a named knob with the consequence written beside it in both `values.yaml` and
  the template, and a chart cannot invent a deployment's CIDRs. Not a defect in this artifact.
- **`.dockerignore` / build context.** `.env`, `*.pem`, `*.key`, `.git` excluded, and the
  Containerfile uses explicit `COPY` targets rather than `COPY . .`, so nothing secret can ride in.
  `uv sync --frozen` verifies lockfile hashes; `uv cache clean` removes build scratch.
- **`infra/live/bootstrap.sh` credential handling.** The `chmod 0644` password file, the
  `PGPASSWORD=…` on a `su -c` command line (visible in `ps`) and the `chmod 0666` postgres log are all
  real weaknesses in isolation, but the credential is the constant `chemclaw` published in
  `docker-compose.yml`, `.env.example` and the config default — there is nothing to disclose. Not
  reported as findings.
