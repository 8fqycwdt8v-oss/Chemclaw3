"""Postgres/pgvector — fingerprint store (Phase 3) and QM result cache (plan step 1.10).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class StoreSettings(BaseSettings):
    """Postgres/pgvector — fingerprint store (Phase 3) and QM result cache (plan step 1.10).

    Grouped because these are the database-transport knobs every store connection shares: one DSN
    for the whole app plus the connect/statement timeouts.
    """

    postgres_dsn: str = "postgresql://chemclaw:chemclaw@localhost:5432/chemclaw"
    # The credential that owns the schema, as distinct from the one that serves requests
    # (D-2026-08-05-append-only-by-grant-not-by-contract). Empty falls back to `postgres_dsn`, so a
    # single-principal database — dev, CI, `make up`, every test — needs no configuration and
    # behaves exactly as before; splitting is a deployment's opt-in.
    #
    # Why it is worth splitting: `infra/sql/006` calls `audit_events` "append-only by contract",
    # and one DSN with full DDL and DML was mounted on every pod, so the credential running a chat
    # turn could rewrite the GxP trail recording that turn. The chain and the anchors detect that;
    # only a privilege boundary prevents it. This DSN belongs on the migration hook Job and
    # nowhere else — it is mounted for the seconds a release takes, not for the life of a pod.
    postgres_migration_dsn: str = ""
    # The ordered `.sql` migrations `chemclaw.core.migrate` applies. A setting rather than
    # a path derived from `__file__`, which is what it was until D-148: `parent.parent` happened to
    # be the repository root only while the module sat two levels deeper, inside the calc package,
    # and moving it there silently pointed the path inside the package — `make db-migrate` failed
    # in CI with no SQL found. (The module has since moved again, to `core/`, which is the reason
    # this setting is not a depth count: it survived that move without an edit.) The directory is
    # repository/workdir-relative like `knowledge_dir` and `skills_dir` (the image COPYs it to
    # `/app/infra` beside `/app/src`), so it follows the same rule as every other data directory
    # rather than a depth count nothing checks.
    sql_migrations_dir: str = "infra/sql"
    # Fail fast when the database is unreachable instead of hanging until the enclosing
    # activity's start-to-close timeout expires (libpq connect_timeout).
    pg_connect_timeout_seconds: int = Field(default=10, gt=0)
    # Per-statement wall-clock bound for the store connections (libpq statement_timeout). A hung
    # query is cancelled after this instead of consuming the whole enclosing activity's
    # start-to-close budget. Applied by `db.connection()` to every borrowed connection whose caller
    # names no bound of its own, so a store cannot be unbounded by forgetting an argument
    # (D-2026-08-08-a-borrowed-connection-is-bounded-by-default). 0 disables it; migrations
    # deliberately connect without a statement timeout (an index build may be slow), via
    # `db.connect()` rather than by omitting an argument.
    pg_statement_timeout_seconds: float = Field(default=30.0, ge=0)
    # How long a migration's DDL may *wait for a table lock* (libpq `lock_timeout`) before giving
    # up. Deliberately not `statement_timeout`, and the distinction is the whole point: an
    # `ALTER TABLE` needs `ACCESS EXCLUSIVE`, and a lock request that queues behind one long read
    # **blocks every subsequent query on that table behind it**, because Postgres's lock queue is
    # FIFO. So a migration run against a live system does not merely wait — it takes the table down
    # while waiting. `lock_timeout` bounds the wait and leaves the work unbounded, which is exactly
    # right here: a `CREATE INDEX` may legitimately build for minutes once it *has* its lock, and
    # capping that with a statement timeout would break migrations that are behaving correctly.
    # 5 s is the conventional value: long enough to slip between ordinary queries, short enough
    # that a failed attempt costs nothing. The Job's `backoffLimit` is what retries it.
    pg_migration_lock_timeout_seconds: float = Field(default=5.0, gt=0)
    # How long to wait for *another migrator* to finish before giving up (the advisory lock). Much
    # larger than the DDL bound above and for the opposite reason: a concurrent migration is a
    # legitimate event, not a fault — two `helm upgrade`s, or an operator running `make db-migrate`
    # during a deploy — and the right response is to wait for it and then find every file already
    # applied. Bounded all the same, so a migrator that died holding a session lock cannot wedge
    # the next release forever.
    pg_migration_lock_wait_seconds: float = Field(default=300.0, gt=0)
    # Per-process connection pool (`chemclaw.db.pooling`). Connect-per-call was measured at ~2.7
    # TCP+auth handshakes per chat turn, and the cost lands on the event loop rather than on the
    # database — a connect that cannot be scheduled inside `pg_connect_timeout_seconds` fails,
    # which is how a non-fatal correctness guard got silently disarmed under load.
    #
    # `min_size` connections are kept warm so the first request after an idle period does not pay
    # a handshake. `max_size` bounds one process; the deployment total is
    # `max_size × distinct DSNs × processes` (front-door replicas × `service_uvicorn_workers`,
    # plus the workers), which must stay under the server's `max_connections` — see
    # `pg_fleet_max_connections` below, which is what turns that sentence into a check.
    pg_pool_min_size: int = Field(default=2, ge=0)
    pg_pool_max_size: int = Field(default=16, gt=0)
    # The two halves of the fleet connection budget
    # (D-2026-08-05-the-connection-budget-is-a-fleet-number).
    #
    # The sentence above stated the multiplication and nothing computed it, so the shipped chart
    # ran every one of its pods on the default `max_size=16` and the fleet's real ceiling was
    # ~272 connections against the `max_connections=100` D-119 measured against. That is the same
    # shape as the admission cap before `service_fleet_max_concurrent_turns`: a per-process bound
    # that is correct in every pod while the fleet total is not, and no single pod can see it.
    #
    # `pg_fleet_pooled_processes` is how many processes may open a pool at once — every front-door
    # process, every Temporal worker, every connector server. The chart derives it from the same
    # values that render those Deployments (`chemclaw.pooledProcesses`), so it cannot disagree with
    # the pods that exist. `pg_fleet_max_connections` is what the server will actually serve this
    # deployment; 0 means undeclared and the check is inert, matching how the turn ceiling and the
    # artifact-eviction budgets ship off until an operator states a number.
    pg_fleet_pooled_processes: int = Field(default=1, gt=0)
    pg_fleet_max_connections: int = Field(default=0, ge=0)
    # Close a connection idle beyond this, so a burst does not pin `max_size` sockets forever.
    pg_pool_max_idle_seconds: float = Field(default=300.0, gt=0)
    # How long a caller waits for a free pooled connection before the request fails as a
    # transient infrastructure fault (a `ConnectionError`, which Temporal retries).
    pg_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Artifact store (D-124): a calculation's by-products — Hessians, optimized geometries,
    # conformer ensembles — kept past the temporary directory that used to delete them.
    # On by default because the value is immediate (a Hessian reused instead of recomputed) and
    # the cost is bounded by the cap below; a deployment that wants none sets `enabled=false`.
    artifact_store_enabled: bool = True
    # Per-artifact ceiling, checked before the file is read so an outsized one never enters RAM.
    # An artifact over the cap is *skipped with a warning*, never an error: capturing a
    # by-product must not be able to fail the calculation it is a by-product of. 0 disables.
    artifact_max_bytes: int = Field(default=33_554_432, ge=0)
    # zlib level for stored artifacts; 0 stores raw. 6 is zlib's own default — the knee of the
    # ratio/CPU curve on the text formats these artifacts actually are.
    artifact_compression_level: int = Field(default=6, ge=0, le=9)
    # Eviction budget in stored bytes, and the idle window a blob must exceed to be a candidate.
    # Both 0 = off, so the sweep is inert until an operator opts in — matching `retention_*_days`.
    # Eviction targets blobs only; `calculation_results` is never evicted (D-011).
    artifact_store_max_bytes: int = Field(default=0, ge=0)
    artifact_evict_idle_days: int = Field(default=0, ge=0)
    # How often the eviction sweep runs, once either bound above turns it on. A day: eviction is a
    # cost policy, not a correctness one, and a blob that survives an extra few hours costs storage
    # rather than accuracy.
    artifact_eviction_schedule_minutes: float = Field(default=1440.0, gt=0)
    # How stale a blob's access stamp must be before a read bothers to refresh it. Bumping on
    # every hit would turn each read into a write on the reuse hot path; at most one write per
    # blob per window is enough for an idle-based eviction decision.
    artifact_access_stamp_seconds: float = Field(default=3600.0, ge=0)

    # --- Where dense vectors live (D-2026-08-08-a-vector-store-is-not-a-catalogue) ---
    # `pgvector` (the default) keeps embeddings in the same Postgres as everything else and answers
    # a search in one statement — ranking, eligibility and the citation join together. Any other
    # provider means an external vector database, and then only the *dense* half moves: the file
    # table, the fingerprint diff, the mark-and-sweep and the citation stay in Postgres, because
    # they are relational work a vector store has no joins for and no clock to measure.
    #
    # Adding a provider is an adapter module plus a name here — the shape `embedding_provider` and
    # `llm_provider` already have.
    vector_store_provider: Literal["pgvector", "qdrant"] = "pgvector"
    # Where the external store is. Unused by `pgvector`, which reads `postgres_dsn` like every
    # other store here.
    vector_store_url: str = "http://localhost:6333"
    # Registered with the log-redaction inventory at startup by the package validator, so a client
    # echoing its own configuration into a traceback cannot put the key in a log.
    vector_store_api_key: str = ""
    vector_store_timeout_seconds: float = Field(default=30.0, gt=0)
    # The collection the document corpus's chunks live in. Named rather than derived, because a
    # cluster is often shared and "which collection is ours" is a deployment fact, not a constant.
    vector_store_document_collection: str = "chemclaw_document_chunks"

    @model_validator(mode="after")
    def _external_vector_store_is_addressable(self) -> "StoreSettings":
        """An external provider with no URL would fail on the first search, not at startup.

        The same stance `_embedding_provider_config` takes for `openai_compatible`: a provider
        selected without the address it needs is a misconfiguration that can be caught while
        somebody is still looking at the deploy, and the alternative is a client library's
        connection error surfacing from inside a worker hours later.
        """
        if self.vector_store_provider != "pgvector" and not self.vector_store_url:
            raise ValueError(
                f"vector_store_provider={self.vector_store_provider!r} needs `vector_store_url` "
                "to point at the store; only 'pgvector' reads `postgres_dsn` instead"
            )
        return self

    @model_validator(mode="after")
    def _pool_bounds_are_orderable(self) -> "StoreSettings":
        """A pool whose floor exceeds its ceiling cannot be built; say so at startup, not later.

        psycopg_pool raises on construction, which in a worker means a crash on the first query
        rather than at boot — and the misconfiguration is a single reversed pair of numbers.
        """
        if self.pg_pool_min_size > self.pg_pool_max_size:
            raise ValueError(
                f"pg_pool_min_size ({self.pg_pool_min_size}) exceeds "
                f"pg_pool_max_size ({self.pg_pool_max_size})"
            )
        return self
