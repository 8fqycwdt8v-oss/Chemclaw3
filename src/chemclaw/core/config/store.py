"""Postgres/pgvector — fingerprint store (Phase 3) and QM result cache (plan step 1.10).

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

# Qdrant's own default, and the value `vector_store_url` ships with. Named because the
# addressability validator has to compare against it: the field is non-empty by default, so
# "is it set" cannot be an emptiness test for any provider that is not Qdrant.
_QDRANT_DEFAULT_URL = "http://localhost:6333"

# The vector databases this repository ships an adapter for. Names rather than references, because
# `core` imports no sibling and the mapping onto `module:callable` belongs beside the adapters, in
# `retrieval.vectors.registry`. `tests/test_vector_store.py` holds the two declarations in step — a
# name this setting accepts and the registry cannot resolve would fail at the first search.
_SHIPPED_VECTOR_STORES = ("qdrant", "databricks")


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
    # turn could rewrite the audit trail recording that turn. Only a privilege boundary prevents
    # that. This DSN belongs on the migration hook Job and
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
    # When a unit of work on a borrowed connection is slow enough to be worth a line in the log.
    # A *warning threshold*, not a bound — `pg_statement_timeout_seconds` is the bound, and it
    # cancels; this only says so. The two are deliberately far apart: the timeout is the point at
    # which a query has failed, and this is the point at which one is still succeeding while
    # costing a pooled connection long enough to matter to everything else waiting for one.
    # `chemclaw_db_query_duration_seconds` is the distribution; this is the line naming the call
    # site, because a histogram bucket cannot say *which* `operation` was slow on the run that a
    # human is reading the log of. 0 disables it.
    pg_slow_query_seconds: float = Field(default=2.0, ge=0)
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
    # Per-process connection pool (`chemclaw.core.db.pooling`). Connect-per-call was measured at
    # ~2.7
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
    # How many of those pooled processes are *front doors*, which is the correction that turns the
    # budget above from a plausible number into the real one.
    #
    # A pool is keyed on `(loop, dsn, options)` (`core/db._pool_for`), so "one process, one pool"
    # was never what the code did — it was what the arithmetic assumed. Measured against a live
    # server by opening the three call shapes a turn-serving process actually opens, `db._POOLS`
    # holds **two** (the stores' default statement timeout, and `/readyz`'s own two-second one at
    # `api/routes/ops.py`) and `_FOREIGN_POOLS` holds the checkpointer's
    # (`agent/checkpointer.py`), for `_process_max_connections()` = 3 x `pg_pool_max_size`. The
    # shipped chart therefore declared 112 against `maxConnections: 136` and passed while opening
    # ~208. The *runtime* alert was honest the whole time, because
    # `chemclaw_pg_pool_max_size` reads `_process_max_connections()` and so sums all three; only
    # the startup check under-counted, and it is the one that fires before the pods are up.
    #
    # Every other pooled process — a Temporal worker, a connector server, the MCP face — builds no
    # agent and serves no readiness route, so it holds the one stores pool. That is why this is a
    # separate count rather than a multiplier on the whole fleet: charging every worker three pools
    # would overstate the budget by as much as the old arithmetic understated it.
    #
    # `PG_POOLS_PER_FRONT_DOOR_PROCESS` is the measured constant beside the validator that uses it,
    # pinned by `tests/test_config_pools.py` against a live database rather than restated here, for
    # the reason this repository states everywhere: a number in prose is a claim about a commit.
    # 1 is the honest default — a CLI, a test and a single-pod dev run are one front door — and the
    # check is inert anyway until `pg_fleet_max_connections` is declared.
    pg_fleet_front_door_processes: int = Field(default=1, ge=0)
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
    # **A shipped name, or `module:callable` naming any other adapter.** Attaching a vector
    # database this repository has never heard of — Milvus, Weaviate, LanceDB, pgvector on somebody
    # else's server — is one module implementing `retrieval.vectors.base.VectorStore` plus this
    # string, with no branch added to any registry and no core edit. That is the same rule the
    # database seam runs on (`D-2026-08-26-the-driver-s-signature-is-the-schema`): a vector database
    # is a database this system does not own, attached the same way as any other.
    vector_store_provider: str = "pgvector"
    # Where the external store is. Unused by `pgvector`, which reads `postgres_dsn` like every
    # other store here.
    vector_store_url: str = _QDRANT_DEFAULT_URL
    # A `SecretStr` and a member of `core/logging.py`'s `_SECRET_SETTINGS`, like every other
    # credential on this object (`D-2026-08-26-a-credential-is-a-type-not-a-convention`). It was
    # neither until 2026-08-27, and this comment used to say the read-site `register_secret_env`
    # call covered it instead. That was true only when the value came from the process environment:
    # `register_secret_env` stores a *name*, `Settings` reads `.env` without exporting anything, so
    # on the documented `.env` posture the registered name resolved to nothing. The mechanism is
    # fixed (`logging._configured_by`) and the registrations stay, but a credential that is a field
    # belongs in the field inventory — that is the one that cannot depend on where the value came
    # from.
    vector_store_api_key: SecretStr = SecretStr("")
    vector_store_timeout_seconds: float = Field(default=30.0, gt=0)
    # The collection the document corpus's chunks live in. Named rather than derived, because a
    # cluster is often shared and "which collection is ours" is a deployment fact, not a constant.
    vector_store_document_collection: str = "chemclaw_document_chunks"
    # Databricks only: the Vector Search *endpoint* serving the index. An index is addressed by a
    # pair — the endpoint that serves it and its three-level Unity Catalog name — and only the
    # second of those is a "collection" in the sense the setting above means. Empty for every other
    # provider, which is why it is validated against the provider rather than given a default that
    # would be wrong everywhere.
    vector_store_endpoint_name: str = ""
    # The collection the knowledge graph's note vectors live in, the twin of the document one above.
    # Named rather than derived for the same reason: a cluster is often shared, and "which
    # collection is ours" is a deployment fact. Both corpora follow one `vector_store_provider` —
    # there is deliberately no way to keep notes in Postgres while documents are elsewhere, because
    # a per-corpus provider would be two selections to keep consistent and no deployment has asked.
    vector_store_note_collection: str = "chemclaw_note_index"
    # How many eligible keys a filtered search over an index-ranked warehouse source may send as its
    # scope. Eligibility has to reach the index *before* its top-k or a narrow filter over a wide
    # corpus returns nothing, so it travels as a set of keys — and a set is a set: a broad filter
    # over ten million reactions would build a filter payload no client will carry. Exceeding this
    # is refused with the filter named, rather than truncated, because a silently truncated
    # eligibility set is a wrong answer that reads as a thin corpus.
    vector_store_max_scope_keys: int = Field(default=10_000, gt=0)

    @field_validator("vector_store_provider")
    @classmethod
    def _vector_store_is_resolvable(cls, value: str) -> str:
        """A shipped name, or a `module:callable` — never a name nothing can resolve.

        The field used to be a `Literal`, which caught a typo and also made a fourth vector database
        a change to this file. What replaced it still catches the typo: a bare word that is not one
        of the shipped adapters cannot name anything, because a custom adapter is addressed by its
        import path. The path itself is resolved when the store is first built, by the same
        late-binding `core.connect` gives every other database driver, so selecting a provider still
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
        """An external provider with no URL would fail on the first search, not at startup.

        The same stance `_embedding_provider_config` takes for `openai_compatible`: a provider
        selected without the address it needs is a misconfiguration that can be caught while
        somebody is still looking at the deploy, and the alternative is a client library's
        connection error surfacing from inside a worker hours later.
        """
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
            # **Every provider but Qdrant's own, not just the shipped second one.** The emptiness
            # check above cannot catch this: the field has a non-empty default, so a deployment that
            # selected another store and forgot its address passes startup and fails inside a
            # worker. This used to name `databricks` literally, which reopened the same hole the
            # moment the provider became any `module:callable` — a site's own adapter would have
            # inherited Qdrant's localhost default and validated clean.
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
