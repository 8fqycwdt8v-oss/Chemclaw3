# Pistachio on Databricks: remove the Snowflake integration, generalise the database seam

## The ask

> The first database integrated will be pistachio using databricks. Remove the specific snowflake
> integration but leave the generic database connector for easy connection of pistachio and
> additional databases later on. Keep this connector that general that any modern database can be
> easily integrated. Make sure to not only focus on classical databases like Postgres, snowflake or
> databricks but as well vector databases.

## What is already true (checked, not assumed)

- `chemclaw.ingest.eln.warehouse` is already a schema-in-a-file engine naming no table, no column.
- `servers`: two drivers exist — `snowflake.py` and `databricks.py`; `pistachio/datasource.yaml`
  and `eln-databricks/datasource.yaml` already bind the Databricks one.
- `chemclaw.retrieval.vectors` already has a `VectorStore` Protocol with three adapters
  (`pgvector` = absence of one, `qdrant`, `databricks`).

## What is *not* general, and is the actual work

1. `ConnectionBinding` is **Snowflake-shaped**: `account_env`, `user_env`, `private_key_env`,
   `warehouse`, `role`. `publish/connect.py` says so in its own docstring and refuses to reuse it.
   The Databricks driver is contorted by it (`account` means hostname, `password` means token) and
   has to *refuse* two fields that have no analogue.
2. `vector_store_provider` is a closed `Literal` with an `if`-chain — a new vector database is a
   core edit, which is exactly what the warehouse seam refuses to be.
3. Two copies of "resolve `module:callable`, read `*_env` from the environment" exist
   (`ingest/eln/warehouse/connect.py`, `publish/connect.py`).

## Plan

- [x] P0 — read the seam, the two drivers, both connect modules, the vector registry, the manifests
- [x] P1 — **The driver's signature is the schema.** `ConnectionBinding` keeps `driver:` and becomes
      `extra="allow"`; every other key is the driver's own keyword, `*_env` naming an env var.
      One shared implementation in `core/connect.py`, used by ingest, publish and the vector seam.
- [x] P2 — `DatabricksWarehouse` takes its **own** vocabulary (`server_hostname`, `access_token`,
      `http_path`/`warehouse_id`, `catalog`, `schema`); the three fields that "mean something
      different here" stop meaning anything different.
- [x] P3 — delete `warehouse/snowflake.py` and `ingest/sources/eln-snowflake/`; move the fixture
      role onto `eln-databricks`; drop the mypy override and the key-material sanction.
- [x] P4 — **A vector database is a driver too.** `vector_store_provider` accepts a shipped name or
      a `module:callable`, late-bound through the same resolver. No core edit for a fourth store.
- [x] P5 — manifests updated (`eln-databricks`, `pistachio`), READMEs, `.env.example`, CLAUDE.md,
      the concept guide, DEFERRED rows that named the Snowflake tenant.
- [x] P6 — ADR, `make lint type test` green with Postgres up (say what skipped otherwise).

## Verification plan

- `make lint type test` with `dockerd` + `make up` + `make db-migrate` running, so the ~157
  Postgres tests actually run rather than skip.
- `make datasource-validate --construct` — builds the halves, so it validates the *binding*.
- New tests: a binding against a stub driver with an invented vocabulary proves "any modern
  database"; a `module:callable` vector store proves the same for the vector half.

## Review

**What shipped.**

1. **Snowflake is gone**: `warehouse/snowflake.py`, `ingest/sources/eln-snowflake/`, the
   `snowflake.*` mypy override, and the `("chemclaw.ingest", "crypto")` layering row whose only site
   was that driver's key-pair auth. `eln-databricks` is now the manifest the binding tests resolve
   every path of, and `pistachio` beside it is the first integration.
2. **`ConnectionBinding` declares `driver:` and nothing else** (`extra="allow"`). Every other key is
   a keyword argument of the callable it names. `DatabricksWarehouse` consequently took its own
   vocabulary (`server_hostname`, `access_token`, `warehouse_id`/`http_path`, `catalog`, `schema`)
   and lost both the translation table in its docstring and the hand-written refusal of two fields
   that had no analogue.
3. **One implementation of "attach a database"** in `core/connect.py`, used by ingest, publish and
   the vector registry. The ~90 duplicated lines in `publish/connect.py` are gone; its own docstring
   had said the duplication existed only because the model was one vendor's shape.
4. **The offline check moved to where it can be right**: `make datasource-validate` resolves the
   driver and binds the block against its signature, sharing `signature_mismatch` with
   `make sink-validate` so the two cannot drift on the `_env` stripping rule.
5. **A vector database is a driver too**: `vector_store_provider` takes a shipped name or a
   `module:callable`. The `Literal`'s typo check survives as a field validator.

**Measured, not argued.**

- `make lint type test` green with `dockerd` + `make up` + `make db-migrate` running:
  **4805 passed, 3 skipped**. The three skips are the shallow-clone migration-history checks, not
  Postgres skips — the ~157 Postgres tests ran (a bare run before starting the daemon showed 3515).
- Every validator green: `datasource-validate` (and `--construct`), `sink-validate`,
  `connector-validate`, `skill-validate`, `prose-validate`, `eln-validate`, `template-validate`.
- The new gate demonstrated rather than asserted: a `role: ANALYST` added to `pistachio`'s
  connection block failed `make datasource-validate` naming the keyword; a `hostt:` typo in the
  sink manifest failed `make sink-validate` the same way. Both reverted.

**One thing deliberately not done.** The engine tests no longer pin a vendor's SQL — the fake brings
its own dialect — because with the connection vocabulary now the driver's, borrowing a shipped
dialect would pin one vendor's spelling into every test of a module whose whole claim is that it has
none. The shipped spelling is pinned twice instead: against the real dialect, and end to end through
the retriever. That is more Databricks coverage than existed before, not less.
