# D-2026-10-15-postgres-is-backed-up-by-wal-archiving-to-an-object-store — Postgres is backed up by WAL archiving to an object store, as a CloudNativePG chart of its own, proved by a restore drill

**Status:** accepted · **Date:** 2026-10-15

## Context

Postgres is the only store for everything stateful: the checkpointer, sessions, limits, claims, the
outbox, the audit trail, `calculation_results`, Temporal's own databases and, with W4, the knowledge
graph (`kg_*`). Backing up Postgres is backing up the system, and after W4.9 there is no git mirror
to rebuild notes from. Until now the chart owned no Postgres, no backup existed, and the backlog said
"no owner of those stores is named". W4.9 is blocked until a restore has run, and the drill must
include the `kg_*` tables.

Measured with `make restore-drill` on the compose stack (pgvector 16, Temporal 1.25) against main
with the `kg_*` migration: 125 migrations, 271 rows over 67 tables (the knowledge tables included);
base backup 53 MB, WAL archive 67 MB; the restore to the marker took about 1.5 s and the whole drill
30 to 45 s. Replaying the same archive with the target removed fails four checks (the marker, the
row counts, the post-target exclusion and the validator's negative control), so the drill tells a
restore that ran too far from one that stopped on time.

## Options

1. **CloudNativePG `Cluster` with the Barman Cloud plugin.** WAL is archived continuously and base
   backups are scheduled; a restore bootstraps a new Cluster to a target time. One more operator and
   a plugin to run (with cert-manager). Chosen.
2. **CloudNativePG with the built-in `barmanObjectStore`.** One object fewer, but upstream deprecated
   it in 1.26 and is phasing it out; a template written against it is a migration waiting.
3. **Ship pgBackRest or WAL-G jobs and sidecars from the application chart.** The chart owns no
   Postgres, so it would configure `archive_command` on a server it does not run.
4. **Document the requirements only.** Right for a site with its own operator or a managed service
   (RDS, Azure Flexible Server), and the runbook states them as the contract any provider owes. But
   the plan asked for something deployable, and a bare contract has no worked example.
5. **Volume snapshots or logical dumps alone.** Neither restores to an instant; a dump also cannot
   carry Temporal's databases consistently with the application's.

Where the objects live follows from option 1.

- **Inside the application chart (opt-in values), or a chart of their own.** Inside, the Cluster is an
  ordinary resource, but the application's migrate Job is a `pre-install,pre-upgrade` hook that dials
  `<release>-pg-rw` before any ordinary resource exists: a fresh install would wait out the hook's
  900 s and fail without ever creating the Cluster, and enabling it on a running release would create
  an empty database nothing carries data into. Making the Cluster a hook too would leave it outside
  the release and re-created on every upgrade. A chart of its own (`deploy/helm/chemclaw-postgres`)
  is installed first, outlives the application, and makes a restore what it is: a second release with
  a new name that the application is repointed to. Chosen.
- **Temporal's databases.** (a) Separate databases in the same Cluster, or (b) a second Cluster. (a)
  gives one WAL stream and one restore instant, so a workflow's history and the rows the application
  wrote for it agree after a restore; (b) isolates load and lets a shared platform Temporal outlive a
  release. The chart renders (a) behind `temporalDatabases.enabled`, off by default, because
  Temporal is platform-owned here; a platform Temporal with its own Postgres owes the same contract
  and is restored to the same instant.
- **Pooler.** The PgBouncer guidance (D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint)
  needs a transaction-mode and a session-mode endpoint, i.e. two CloudNativePG `Pooler` objects, and
  applies only above three replicas. Not templated; the `-rw` Service is the direct endpoint both
  DSNs can name.

## Decision

**Option 1 as a chart of its own, with option 4's contract written down.** The chart renders an
`ObjectStore`, a `Cluster` and a `ScheduledBackup` (and, opt-in, Temporal's `Database` objects and
role). It refuses to render until the operator states the image (stock operator images carry no
pgvector), an `s3://` destination, and the two recovery targets: RPO and RTO are parameters the site
must set, not guarantees the chart invents.

- **RPO** is checked against the one thing the chart controls, the WAL shipping interval
  (`archive_timeout`, which is only settable through `archiveTimeoutSeconds`, so the check cannot be
  bypassed).
- **RTO** is recorded and **not verified by the chart or the drill**. The drill restores a fixture of
  tens of megabytes; its optional `--rto-seconds` is a regression guard on the procedure, labelled
  with the fixture size. RTO is verified by the rehearsal the runbook prescribes: a restore into a
  scratch namespace at the real data volume, timed.
- **Three ways a cluster comes into being**, each a values switch and each render-tested: empty
  (`initdb`), imported from the Postgres a running release already uses (`import`: a logical copy, with
  a stated downtime and the writers stopped), and restored from an archive (`recovery`: a new release
  with a new name that archives under that name, so it never writes into the archive it came from).
  A `rehearsal` flag restores without archiving or backing up, for the scratch namespace.
- **Nothing is deleted with the release.** Every object carries `helm.sh/resource-policy: keep`, so
  uninstall, rollback and an upgrade that stops rendering one cannot delete a primary's volumes.
  Deleting a database is a deliberate `oc delete`.
- **A stable pod label** (`chemclaw/postgres: "true"`) is what the application's NetworkPolicy
  selects, so replacing the cluster does not change the policy.

Restore manifests for a real incident are values of the same chart, not a template to edit under
pressure. The drill (`make restore-drill`) is operator-agnostic: it takes a base backup of a
throwaway Postgres, replays archived WAL to a marker and checks the restore against the primary. It
proves the procedure and the data (schema ledger, indexes, grants, pgvector, the knowledge tables and
validator, Temporal), not the operator.

**Erasure applies on restore.** A backup holds what retention pruned and what `make user-erase`
erased until the backup itself expires. That lifetime is the longest of the plugin's retention, the
bucket's object-lock period and, where versioning is on, the noncurrent-version expiry, all of which
the runbook asks the operator to state. Backups are not rewritten; the operator logs every erasure
outside the database, and after any restore re-applies those since the backup, with the writers
stopped, before the system serves traffic.

## Consequences

- Backup-key and object-store credentials are as sensitive as the database; the runbook says who
  holds them and what is not covered (the object store, the KMS).
- Two releases to install, in order, for a fresh environment; the runbook gives both the fresh and
  the existing-release procedure.
- The drill's SQL lives in `core/restore_check.py`, beside `migrate.py` and `grants.py` (database
  administration, driven by DSN); `cli` stays free of `psycopg`.
- Held by `tests/test_restore_drill.py` (the drill end to end where Docker exists, with a named,
  counted skip where it does not or an image cannot be pulled; a skipped `kg_tables` check is a
  failure where the migrations define the tables) and `tests/test_postgres_cluster_chart.py`;
  `make helm-validate` checks every shape of the chart against the CRD catalogue, strictly.

Revisit when: `make restore-drill`'s checks are run against a real CloudNativePG cluster restored to
a scratch namespace and a result differs from the container drill's (then the container drill stops
being the proxy), or CloudNativePG removes the built-in object-store backup and the plugin is no
longer the only path.
