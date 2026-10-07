"""Settings for the Postgres/pgvector stores, the artifact store and external vector stores.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

from chemclaw.core.config.dsn import DatabaseDsn

# Qdrant's own default URL, which `vector_store_url` ships with; named so the addressability
# validator can tell "left at default" from "set".
_QDRANT_DEFAULT_URL = "http://localhost:6333"

# Vector databases with a shipped adapter. Names only (`core` imports no sibling); the mapping lives
# in `retrieval.vectors.registry`, and `tests/test_vector_store.py` keeps the two in step.
_SHIPPED_VECTOR_STORES = ("qdrant", "databricks")


class StoreSettings(BaseSettings):
    """Postgres/pgvector — fingerprint store (Phase 3) and QM result cache (plan step 1.10).

    Grouped because these are the database-transport knobs every store connection shares: one DSN
    for the whole app plus the connect/statement timeouts.
    """

    # `DatabaseDsn`, not `str`, so the userinfo password is masked in renderings
    # (`core/config/dsn.py`).
    postgres_dsn: DatabaseDsn = "postgresql://chemclaw:chemclaw@localhost:5432/chemclaw"
    # The schema-owning credential, distinct from the serving one, so a request-serving credential
    # cannot rewrite the append-only audit trail. Empty falls back to `postgres_dsn`. Mounted only
    # on the migration hook Job.
    postgres_migration_dsn: DatabaseDsn = ""
    # Directory of ordered `.sql` migrations `chemclaw.core.migrate` applies; workdir-relative like
    # `knowledge_dir` (the image copies it to `/app/infra`).
    sql_migrations_dir: str = "infra/sql"
    # libpq `connect_timeout`: fail fast on an unreachable database.
    pg_connect_timeout_seconds: int = Field(default=10, gt=0)
    # libpq `statement_timeout` applied by `db.connection()` to every borrowed connection whose
    # caller names no bound, so a hung query cannot consume an activity's budget. 0 disables.
    # Migrations use `db.connect()` without one, since an index build may be slow.
    pg_statement_timeout_seconds: float = Field(default=30.0, ge=0)
    # Duration above which a unit of work on a borrowed connection is logged with its call site; a
    # warning, not a bound. `chemclaw_db_query_duration_seconds` is the distribution. 0 disables.
    pg_slow_query_seconds: float = Field(default=2.0, ge=0)
    # libpq `lock_timeout` for migration DDL. An `ALTER TABLE` queued behind a long read blocks
    # every later query on that table (FIFO lock queue), so the lock wait is bounded while the work
    # is not (an index build may run for minutes). The Job's `backoffLimit` retries.
    pg_migration_lock_timeout_seconds: float = Field(default=5.0, gt=0)
    # How long to wait for another migrator's advisory lock. A concurrent migration is legitimate,
    # so wait and then find files applied; bounded so a dead migrator cannot wedge the next release.
    pg_migration_lock_wait_seconds: float = Field(default=300.0, gt=0)
    # Per-process connection pool (`chemclaw.core.db.pooling`). `min_size` is the warm floor;
    # `max_size` bounds one pool, and a process holds one pool per distinct `(dsn, libpq options,
    # max_size)` key. A call site may narrow its pool through `db.connection`'s pool-size argument.
    # The deployment total is `Settings.fleet_connections_per_server`.
    pg_pool_min_size: int = Field(default=2, ge=0)
    pg_pool_max_size: int = Field(default=16, gt=0)
    # The fleet connection budget. `pg_fleet_pools` is the number of pools the fleet opens (the
    # chart derives it in `chemclaw.fleetPools`); a front door holds three (stores, readiness probe,
    # checkpointer), and a split `session_store_dsn` adds one per pooled process.
    # `fleet_connections_per_server` charges readiness pools one connection and others
    # `pg_pool_max_size`. `pg_fleet_max_connections` is what `postgres_dsn`'s server serves, and
    # `pg_session_fleet_max_connections` the same for a split session store; 0 = undeclared (a split
    # with no ceiling is warned, not refused).
    pg_fleet_pools: int = Field(default=1, gt=0)
    # Pools at a rolling update's peak (chart's `chemclaw.fleetPoolsAtRolloutPeak`), paired with
    # `service_fleet_replicas_at_rollout_peak`. 0 = undeclared; the steady pair is used.
    pg_fleet_pools_at_rollout_peak: int = Field(default=0, ge=0)
    pg_fleet_max_connections: int = Field(default=0, ge=0)
    pg_session_fleet_max_connections: int = Field(default=0, ge=0)
    # Close a connection idle beyond this, so a burst does not pin `max_size` sockets forever.
    pg_pool_max_idle_seconds: float = Field(default=300.0, gt=0)
    # Wait for a pooled connection before failing with `ConnectionError`, which Temporal retries.
    pg_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Retries of a transaction Postgres aborted as a deadlock victim. Session deletion and the
    # retention pass take `session_owners` and `session_turns` in opposite orders that cannot be
    # changed; the guarded statements are idempotent deletes, so a retry is safe. 2 so the retry's
    # own collision is not the answer. 0 disables.
    pg_deadlock_retries: int = Field(default=2, ge=0)
    # Keep a calculation's by-products (Hessians, geometries, ensembles) for reuse; bounded by the
    # cap below.
    artifact_store_enabled: bool = True
    # Per-artifact ceiling, checked before reading the file. Oversized artifacts are skipped with a
    # warning, never failing the calculation. 0 disables.
    artifact_max_bytes: int = Field(default=33_554_432, ge=0)
    # Largest by-product `GET /calc-artifacts/content` serves (413 before reading, since it
    # decompresses into memory); matches the write cap.
    calc_artifact_max_download_bytes: int = Field(default=33_554_432, ge=1)
    # zlib level for stored artifacts; 0 stores raw.
    artifact_compression_level: int = Field(default=6, ge=0, le=9)
    # Eviction budget in stored bytes, and the idle window a blob must exceed. Both 0 = off. Only
    # blobs are evicted; `calculation_results` never is.
    artifact_store_max_bytes: int = Field(default=0, ge=0)
    artifact_evict_idle_days: int = Field(default=0, ge=0)
    # Eviction sweep cadence once either bound is set.
    artifact_eviction_schedule_minutes: float = Field(default=1440.0, gt=0)
    # Minimum age of a blob's access stamp before a read refreshes it, so reads are not writes.
    artifact_access_stamp_seconds: float = Field(default=3600.0, ge=0)

    # --- Where dense vectors live ---
    # `pgvector` keeps embeddings in Postgres and answers a search in one statement. Any other
    # provider moves only the dense half; files, diffs, sweeps and citations stay in Postgres. A
    # shipped name or a `module:callable` naming an adapter implementing
    # `retrieval.vectors.base.VectorStore`, with no registry edit.
    vector_store_provider: str = "pgvector"
    # Where the external store is; unused by `pgvector`.
    vector_store_url: str = _QDRANT_DEFAULT_URL
    # A `SecretStr` in `core/logging.py`'s `_SECRET_SETTINGS`, like every credential here.
    vector_store_api_key: SecretStr = SecretStr("")
    vector_store_timeout_seconds: float = Field(default=30.0, gt=0)
    # Collection holding document chunks; a deployment fact on a shared cluster.
    vector_store_document_collection: str = "chemclaw_document_chunks"
    # Databricks only: the Vector Search endpoint serving the index (an index is endpoint plus Unity
    # Catalog name). Validated against the provider.
    vector_store_endpoint_name: str = ""
    # Collection holding note vectors. Both corpora follow one `vector_store_provider`.
    vector_store_note_collection: str = "chemclaw_note_index"
    # Most eligible keys a filtered search over an index-ranked warehouse source may send as scope
    # (eligibility must reach the index before its top-k). Exceeding it is refused with the filter
    # named, never truncated.
    vector_store_max_scope_keys: int = Field(default=10_000, gt=0)

    @field_validator("vector_store_provider")
    @classmethod
    def _vector_store_is_resolvable(cls, value: str) -> str:
        """A shipped name, or a `module:callable` — never a name nothing can resolve.

        The path is resolved when the store is first built (`core.connect`), so selecting a provider
        does not import its client.
        """
        if value == "pgvector" or value in _SHIPPED_VECTOR_STORES or ":" in value:
            return value
        raise ValueError(
            f"vector_store_provider={value!r} names neither a shipped adapter "
            f"('pgvector', {', '.join(repr(n) for n in _SHIPPED_VECTOR_STORES)}) nor a "
            "'module:callable' building one (e.g. 'acme.vectors:MilvusVectorStore')"
        )

    @model_validator(mode="after")
    def _external_vector_store_is_addressable(self) -> "StoreSettings":
        """An external provider with no URL would fail on the first search, not at startup."""
        if self.vector_store_provider != "pgvector" and not self.vector_store_url:
            raise ValueError(
                f"vector_store_provider={self.vector_store_provider!r} needs `vector_store_url` "
                "to point at the store; only 'pgvector' reads `postgres_dsn` instead. A custom "
                "adapter is held to the same rule: this is the one address the seam exposes, and a "
                "store selected without it fails on the first search rather than at startup"
            )
        if (
            self.vector_store_provider not in ("pgvector", "qdrant")
            and self.vector_store_url == _QDRANT_DEFAULT_URL
        ):
            # Every provider but Qdrant: the field defaults non-empty, so a forgotten address would
            # pass the emptiness check and fail in a worker.
            raise ValueError(
                f"vector_store_provider={self.vector_store_provider!r} still has the shipped "
                f"default vector_store_url={_QDRANT_DEFAULT_URL!r}, which is Qdrant's; set the "
                "address of the store you selected"
            )
        if self.vector_store_provider == "databricks" and not self.vector_store_endpoint_name:
            raise ValueError(
                "vector_store_provider='databricks' needs `vector_store_endpoint_name`: a Vector "
                "Search index is addressed by the endpoint serving it as well as by its Unity "
                "Catalog name, and the client cannot resolve one from the other"
            )
        return self

    @model_validator(mode="after")
    def _pool_bounds_are_orderable(self) -> "StoreSettings":
        """A pool whose floor exceeds its ceiling cannot be built; say so at startup, not later."""
        if self.pg_pool_min_size > self.pg_pool_max_size:
            raise ValueError(
                f"pg_pool_min_size ({self.pg_pool_min_size}) exceeds "
                f"pg_pool_max_size ({self.pg_pool_max_size})"
            )
        return self
