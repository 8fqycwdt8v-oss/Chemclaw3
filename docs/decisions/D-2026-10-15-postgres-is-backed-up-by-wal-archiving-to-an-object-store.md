# D-2026-10-15-postgres-is-backed-up-by-wal-archiving-to-an-object-store — Postgres is backed up by WAL archiving to an object store, templated for CloudNativePG and proved by a restore drill

**Status:** accepted · **Date:** 2026-10-15

## Context

Postgres is the only store for everything stateful: the checkpointer, sessions, limits, claims, the
outbox, the audit trail, `calculation_results`, Temporal's own databases and, once W4 lands, the
knowledge graph (`kg_*`). Backing up Postgres is backing up the system, and after W4.9 there is no
git mirror to rebuild notes from. Until now the chart owned no Postgres, no backup existed, and the
backlog said "no owner of those stores is named". W4.9 is blocked until a restore has run, and the
drill must include the `kg_*` tables.

Measured with `make restore-drill` on the compose stack (pgvector 16, Temporal 1.25): 124 migrations,
242 rows over 64 tables; base backup 53 MB, WAL archive 67 MB; the restore to the marker took about
1.5 s and the whole drill 45 to 60 s. Replaying the same archive with the target removed fails four
checks (the marker, the row counts, the post-target exclusion and the validator's negative control),
so the drill tells a restore that ran too far from one that stopped on time.

## Options

1. **CloudNativePG `Cluster` with the Barman Cloud plugin, as an opt-in chart template.** WAL is
   archived continuously and base backups are scheduled; a restore bootstraps a new Cluster to a
   target time. One more operator and a plugin to run (with cert-manager). Chosen.
2. **CloudNativePG with the built-in `barmanObjectStore`.** One object fewer, but upstream deprecated
   it in 1.26 and is phasing it out; a template written against it is a migration waiting.
3. **Ship pgBackRest or WAL-G jobs and sidecars from this chart.** The chart owns no Postgres, so
   it would configure `archive_command` on a server it does not run and own an operator's job.
4. **Document the requirements only.** Right for a site with its own operator or a managed service
   (RDS, Azure Flexible Server), and the runbook states them as the contract any provider owes. But
   the plan asked for something deployable, and a bare contract has no worked example.
5. **Volume snapshots or logical dumps alone.** Neither restores to an instant; a dump also cannot
   carry Temporal's databases consistently with the application's.

Two sub-choices follow from option 1.

- **Temporal's databases.** (a) Separate databases in the same Cluster, or (b) a second Cluster.
  (a) gives one WAL stream and one restore instant, so a workflow's history and the rows the
  application wrote for it agree after a restore; (b) isolates load and lets a shared platform
  Temporal outlive a release. The template renders (a) behind `postgres.cluster.temporalDatabases`,
  off by default, because Temporal is platform-owned here; a platform Temporal with its own Postgres
  owes the same contract and is restored to the same instant.
- **Pooler.** The PgBouncer guidance (D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint)
  needs a transaction-mode and a session-mode endpoint, i.e. two CloudNativePG `Pooler` objects,
  and applies only above three replicas. Not templated; the Cluster's `-rw` Service is the direct
  endpoint both DSNs can name, and a site adds Poolers when it crosses that line.

## Decision

**Option 1, with option 4's contract written down.** `postgres.cluster` renders a `Cluster`, an
`ObjectStore` and a `ScheduledBackup` (and, opt-in, Temporal's `Database` objects and role). It is
off by default and refuses to render until the operator states the image (stock operator images
carry no pgvector), an `s3://` destination, and the two recovery targets: RPO and RTO are
parameters the site must set, not guarantees the chart invents. The chart checks the target against
the WAL shipping interval it can control (`archive_timeout`) and records both as annotations;
whether the real restore time meets the RTO is read from `make restore-drill` at the real data
volume. Restore manifests are documented, not templated: a restore is an incident-time act with a
target chosen by a person, and a values flag would invite pinning one in a release.

The drill (`make restore-drill`) is operator-agnostic: it takes a base backup of a throwaway
Postgres, replays archived WAL to a marker and checks the restore against the primary. It proves the
procedure and the data (schema ledger, indexes, grants, pgvector, the knowledge validator,
Temporal), not the operator.

**Erasure applies on restore.** A backup holds what retention pruned and what `make user-erase`
erased until the backup itself expires, so the retention policy on the object store is part of the
data-protection answer. The standard position is adopted: backups are not rewritten; after any
restore the erasures recorded since the backup are re-applied before the system serves traffic.

## Consequences

- Backup-key and object-store credentials are as sensitive as the database; the runbook says who
  holds them and what is not covered (the object store, the KMS).
- A restore is a new Cluster, not an in-place rewind; the restored Cluster archives under a different
  `archiveServerName`.
- `cli` may import `psycopg` (a row in `tests/test_third_party_layering.py`) because the drill talks
  SQL to a server it restored itself; no application path does.
- Held by `tests/test_restore_drill.py` (the drill, end to end where Docker exists, with a named
  counted skip where it does not) and `tests/test_postgres_cluster_chart.py`; `make helm-validate`
  checks the objects against the CRD catalogue.

Revisit when: `make restore-drill`'s checks are run against a real CloudNativePG cluster restored to
a scratch namespace and a result differs from the container drill's (then the container drill stops
being the proxy), or CloudNativePG removes the built-in object-store backup and the plugin is no
longer the only path.
