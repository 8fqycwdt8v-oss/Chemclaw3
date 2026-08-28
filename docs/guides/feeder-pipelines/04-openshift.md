# 04 — The OpenShift implementation

Everything here satisfies `01-contract.md`; nothing here adds a requirement. Stages 1 and 2 are
`02-acquisition.md`.

**Pick this platform when** the site has no lakehouse, the upstream is reachable only from the
cluster's network, the volume is modest (hundreds of thousands of reactions rather than tens of
millions), or — the common hybrid — the embedding model lives behind ChemClaw3's internal LLM gateway
and only a pod in this cluster can reach it (§6).

**A CronJob runs beside ChemClaw3; it is not part of it.** Its own namespace, its own service account,
its own secrets, its own alerts. It must not be added to the Helm chart in `deploy/helm/chemclaw`:
that chart's pods are ChemClaw3, and a feeder sharing their identity or their NetworkPolicy would
make a vendor's outage look like an agent outage — the third rule in
`D-2026-08-28-a-feeder-writes-a-table-and-nothing-else`.

---

## 1. Namespace and identity

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: chemclaw-feeders
  labels:
    app.kubernetes.io/part-of: chemclaw
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: feeder-<source>
  namespace: chemclaw-feeders
```

No `Role` and no `RoleBinding`. The job talks to an upstream and to a database; it has no business
with the Kubernetes API, and a service account with no binding is the cheapest way to say so.

## 2. Secrets

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: feeder-<source>
  namespace: chemclaw-feeders
type: Opaque
stringData:
  UPSTREAM_TOKEN: "<the vendor's>"          # stage 1
  TARGET_DSN:     "<the write DSN>"         # stage 4 — a WRITE principal
  # only if this job also embeds (§6):
  CHEMCLAW_LLM_TOKEN: "<the gateway credential>"
```

ChemClaw3's own credential is not in this namespace. It is named by the manifest's `*_env` keys
(`01-contract.md` §6) and is a **read-only** principal on the same database — two principals, two
secrets, and the reader must not be able to write the corpus it reads.

Where the platform offers external secret management, use it. What matters here is only that the
values are read at process start from the environment, so a rotation is picked up by the next run.

## 3. The CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: feeder-<source>
  namespace: chemclaw-feeders
spec:
  # 05:30 UTC — before the working day, and before ChemClaw3's own daily reaction-corpus drain.
  schedule: "30 5 * * *"
  timeZone: "Europe/Berlin"

  # A daily job that overlaps itself corrupts its own watermark. Refuse; do not queue.
  concurrencyPolicy: Forbid

  # If the controller was down at 05:30, still run — but only within a sane window. Without this a
  # cluster that was down for a day fires the missed run at an arbitrary later moment.
  startingDeadlineSeconds: 3600

  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5      # the failed ones are the evidence; keep more of them

  jobTemplate:
    spec:
      # One attempt, then one retry. A feeder is idempotent (02 §1), so a retry is safe — but a
      # high backoffLimit turns a broken upstream into hours of retries against a rate limit.
      backoffLimit: 1
      # The hard bound. Must be shorter than the schedule interval, or Forbid quietly skips days.
      activeDeadlineSeconds: 18000        # 5h against a 24h period
      ttlSecondsAfterFinished: 604800

      template:
        metadata:
          labels: {app.kubernetes.io/name: feeder-<source>}
        spec:
          serviceAccountName: feeder-<source>
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            seccompProfile: {type: RuntimeDefault}
          containers:
            - name: feeder
              image: <registry>/<org>/chemclaw-feeder-<source>@sha256:<digest>
              imagePullPolicy: IfNotPresent
              args: ["--stage", "all"]
              envFrom:
                - secretRef: {name: feeder-<source>}
              env:
                - {name: FEEDER_SOURCE,        value: "<source>"}
                - {name: FEEDER_PAGE_BUDGET,   value: "500"}
                - {name: FEEDER_LOAD_WINDOW_DAYS, value: "7"}
              securityContext:
                allowPrivilegeEscalation: false
                readOnlyRootFilesystem: true
                capabilities: {drop: ["ALL"]}
              resources:
                requests: {cpu: "500m",  memory: "2Gi"}
                limits:   {cpu: "2",     memory: "8Gi"}
              volumeMounts:
                - {name: scratch, mountPath: /scratch}
          volumes:
            # Sized for the largest artifact stage 1 downloads, plus room to decompress it.
            # readOnlyRootFilesystem above is why this exists at all.
            - name: scratch
              emptyDir: {sizeLimit: 20Gi}
```

Four of those settings correspond to a specific failure, and are the ones worth not copying blindly:

- **`concurrencyPolicy: Forbid`** — two runs advancing one watermark is how rows go missing.
- **`activeDeadlineSeconds` < the schedule period** — otherwise a hung run makes `Forbid` skip every
  subsequent day, silently, and the corpus simply stops growing.
- **`backoffLimit: 1`** — a feeder is idempotent, so retrying is safe; retrying *many* times against a
  429 is how a licence gets suspended.
- **`startingDeadlineSeconds`** — without it, a missed schedule fires at an unpredictable later time,
  which is exactly when nobody is watching.

## 4. Network policy

The namespace is default-deny; the feeder is the one thing in it that legitimately reaches the
internet.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: feeder-<source>
  namespace: chemclaw-feeders
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: feeder-<source>}
  policyTypes: [Ingress, Egress]
  ingress: []        # nothing calls a feeder. Ever.
  egress:
    - to: [{ipBlock: {cidr: <upstream CIDR or the egress gateway>}}]
      ports: [{protocol: TCP, port: 443}]
    - to: [{ipBlock: {cidr: <the target database>}}]
      ports: [{protocol: TCP, port: 443}]
    - to:                                   # DNS
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: openshift-dns}}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
```

`ingress: []` is the half worth stating explicitly: a feeder is not an API, has no readiness probe and
serves nothing. Anything that would call it is a design that has gone wrong.

**ChemClaw3's own egress is a separate change**, in its chart: `networkPolicy.egressDestinations` must
name the target database, or the release must state `allowAnyDestination: true`. The chart refuses to
render until one of the two is set (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`), so this
is a decision a release makes rather than one it inherits.

## 5. The image

One rootless image, the way `deploy/Containerfile` is built here:

- pinned base, pinned dependencies, a lockfile;
- **no network access at run time other than what §4 allows** — and no model downloaded at start-up.
  A dependency that fetches weights on first use turns a NetworkPolicy denial into a job that hangs
  rather than one that fails;
- `USER` non-root, `readOnlyRootFilesystem: true`, everything written under `/scratch`;
- one entrypoint with a `--stage` argument (`acquire`, `normalise`, `embed`, `publish`, `verify`,
  `all`), so an operator can re-run one stage after a partial failure without editing the CronJob.

## 6. The hybrid: OpenShift embeds, Databricks stores

The common shape when the corpus lives in a lakehouse but `CHEMCLAW_EMBEDDING_MODEL` is served by the
internal gateway, which the workspace cannot reach.

```
  Databricks job (03)          OpenShift CronJob (this file)
  ─────────────────────        ─────────────────────────────
  acquire, normalise    ──►    read rows WHERE reaction_vector IS NULL
  publish (MERGE)              embed via CHEMCLAW_LLM_BASE_URL   (01 §3.3(b))
  sync_index          ◄──      write the vectors back, L2-normalised (01 §4.2)
```

Two rules keep this honest:

- **The embedder writes only the vector columns**, never the reaction fields. One writer per column.
- **It bumps `load_date` only if the vector changed a row ChemClaw3 has already seen** — a newly
  landed row already carries today's `load_date`, and re-stamping every embedded row re-presents the
  corpus to the drain (`03-databricks.md` §3).

Run it on a schedule offset from the Databricks job — 30–60 minutes later — and make it a no-op when
there is nothing to embed. A no-op run that exits 0 is what lets the alert in §8 mean "the feeder
stopped", which is the thing you actually want to know.

## 7. Target: a Postgres instead of a lakehouse

`01-contract.md` is database-neutral — `connection:` is the driver's own keyword arguments, and
attaching a database this repository ships no driver for is "one module exposing a `Warehouse` plus a
manifest naming it, and no edit anywhere in this package".

**Be clear-eyed about what that costs.** This repository ships exactly one driver, for Databricks.
A Postgres target means writing:

- a callable satisfying `chemclaw.ingest.eln.warehouse.driver.Warehouse` — three methods, plus
  `placeholder`;
- a `VectorDialect` for it, because the similarity call is a dialect fact and a driver that offers
  none **cannot serve a `vector:` block** and says so, naming itself, rather than emitting SQL the
  server will reject. For pgvector that is the cosine-distance operator and the cast that binds a
  query vector.

That is a small module and a real one — plan a day, plus its tests against
`tests/warehouse_fake.py`'s pattern. `driver.py` imports no third party precisely so that this is
testable with no database running.

The relation itself is the same contract:

```sql
CREATE TABLE v_reaction (
  reaction_id      TEXT PRIMARY KEY,
  reaction_smiles  TEXT NOT NULL,
  patent_number    TEXT,
  publication_date DATE,
  yield_pct        DOUBLE PRECISION,
  reaction_vector  vector(1536),        -- == CHEMCLAW_EMBEDDING_DIM
  embedding_model  TEXT,
  load_date        DATE NOT NULL,
  release_id       TEXT NOT NULL
);
CREATE INDEX ON v_reaction (load_date);
CREATE INDEX ON v_reaction USING hnsw (reaction_vector vector_cosine_ops);
```

Publishing is `INSERT ... ON CONFLICT (reaction_id) DO UPDATE`, with the same guard the Databricks
`MERGE` carries: only bump `load_date` for rows that actually changed (`03-databricks.md` §3).

**This is the scan shape only.** There is no index shape without a vector index product, and at
Postgres scale that is the right answer anyway.

**And it must not be ChemClaw3's own Postgres.** That database is `infra/sql/`'s, its migrations are
this repository's, and the runtime principal deliberately does not hold DDL privileges on stores this
system does not own (`schema/` exists for exactly that distinction). A feeder writing into it would be
a second definition of what those tables mean.

## 8. Alerting

The one alert that matters is **"the feeder stopped"**, and it is not the one you get for free. A
CronJob that has not run produces no failed job, no error log and no metric — it produces nothing,
which is indistinguishable from a corpus nobody added to.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: feeder-<source>
  namespace: chemclaw-feeders
spec:
  groups:
    - name: feeder
      rules:
        # The important one: success is a positive statement, and its absence is the alert.
        - alert: FeederHasNotSucceeded
          expr: |
            time() - max(kube_job_status_completion_time{namespace="chemclaw-feeders",
                                                         job_name=~"feeder-<source>.*"}) > 108000
          for: 30m
          labels: {severity: warning}
          annotations:
            summary: "feeder-<source> has not completed successfully in 30h"
            runbook: "docs/guides/feeder-pipelines/05-operations.md#3-failure-modes"

        - alert: FeederFailing
          expr: |
            max(kube_job_status_failed{namespace="chemclaw-feeders",
                                       job_name=~"feeder-<source>.*"}) > 0
          for: 15m
          labels: {severity: warning}
```

Add the corpus-side probes from `05-operations.md` §2 as a `verify` stage that **fails the job**, so a
run that lands zero usable rows is a red job rather than a green one with a bad number in its logs.

## 9. The ChemClaw3 side

Identical to `03-databricks.md` §8, with `connection:` naming your driver and its keyword arguments
instead of the Databricks one. The rest — `corpus:`, `vector:`, `labels:`, the two Schedules — does
not change, which is the point of the seam.
