# D-2026-08-26-the-driver-s-signature-is-the-schema — the Snowflake integration goes, and the connection block stops being one vendor's shape

**Status:** accepted

**Supersedes:** the `ConnectionBinding` model of
[`D-2026-08-04-the-schema-is-a-file`](D-2026-08-04-the-schema-is-a-file.md) (§the binding document).
Everything else in that decision stands: the binding *is* the site's schema, the transform
vocabulary is closed, credentials are named and never carried, and the engine names no table and no
column. This narrows one block of it — the one that named a vendor's words.

## Context

The first database this system actually integrates is **Pistachio on Databricks**. Snowflake was the
warehouse the seam was designed against a month ago, and it never had a tenant: no site, no
credential, no table. Carrying a driver for a database nobody will connect to is the shape this tree
deletes on sight (`D-2026-08-15`), and here it cost more than its own 231 lines.

What it cost was the *shape of the connection block*. `ConnectionBinding` enumerated
`account_env`, `user_env`, `password_env`, `private_key_env`, `warehouse`, `database`, `schema` and
`role` — Snowflake's vocabulary, frozen into a model with `extra="forbid"`. Three consequences,
each measurable in the tree before this change:

1. **The second driver had to lie.** `DatabricksWarehouse` read `account` as a workspace hostname,
   `password` as a personal access token and `warehouse` as an HTTP path, and its class docstring
   carried a translation table saying so. It then had to *refuse* `private_key_env` and `role`
   explicitly, because the shared model accepted them from anyone and silently dropping them would
   leave a deployment believing an access restriction was in force.
2. **The publish seam refused to reuse it at all.** `publish/connect.py`'s own docstring gave the
   reason in full: the model "is Snowflake-shaped: it has an `account`, a `warehouse` and a `role`,
   and **no host or port**", so pointing a Postgres results store at it meant abusing `account` as a
   hostname or teaching one model to describe two products. It solved that correctly — validate the
   block against *the driver's own signature* — and paid for it with ~90 lines duplicating the
   resolver, the credential reading and the redaction registration.
3. **A vector database could not use either.** `retrieval/vectors` had a third mechanism again: a
   closed `Literal["pgvector", "qdrant", "databricks"]` and an `if`-chain, so a fourth store was two
   edits inside `core` before a line of adapter existed.

Three seams reach a database this system does not own — inbound (the ELN and the reaction corpora),
outbound (the result store), and the dense half of retrieval. They had three different answers to
one question, and only one of the three was general.

## Decision

### 1. The connection block is the driver's keyword arguments, and nothing else

`ConnectionBinding` declares `driver:` and is `extra="allow"`. Every other key is passed to the
`module:callable` it names. A key ending `_env` holds the *name* of an environment variable, read at
connect time — the one convention that survives, because it is a rule about secrets rather than
about a vendor.

`DatabricksWarehouse` consequently takes `server_hostname`, `access_token`, `warehouse_id` /
`http_path`, `catalog`, `schema`, `user_agent_entry` and `query_timeout_seconds`: Databricks' own
words, with nothing translating in between and no field to refuse. Attaching a Postgres, a DuckDB
export, a ClickHouse or a vector database is a module beside it with *its* words, and no shared
model to widen.

**What replaces `extra="forbid"` is a check against the callable rather than against a model.**
`make datasource-validate` resolves the driver and binds the block against its signature, offline,
with nothing connected — the rule the sink seam already applied to its own `connection:` block. A
`role:` copied over from another vendor's manifest now fails in CI naming the keyword, where before
it was accepted by the model and refused by a hand-written list inside one driver.

### 2. One implementation of "attach a database", in `chemclaw.core.connect`

Late-binding a `module:callable`, reading `*_env` values at connect time, registering each name with
the log-redaction inventory before reading it. All three seams use it; the two copies are gone.

**The exception type is a parameter of that function**, which looks odd until you know what it
decides: Temporal matches `non_retryable_error_types` by exact class name, so a shared error class
would make the outbound seam retry a failure the inbound one gives up on. `BindingError` and
`SinkConnectionError` stay each seam's own.

### 3. A vector database is a database this system does not own

`vector_store_provider` accepts a shipped name (`pgvector`, `qdrant`, `databricks`) **or** a
`module:callable` building anything else, resolved through the same late-binding resolver. The
typo check the `Literal` bought is kept as a field validator: a bare word that is not shipped
cannot name anything, because a custom adapter is addressed by its import path.

Two declarations now have to stay in step — `core.config.store._SHIPPED_VECTOR_STORES` names the
words, `retrieval.vectors.registry.SHIPPED` maps them — because `core` imports no sibling. A test
holds them together; the alternative was `core` importing `retrieval`, which is the one edge that
would make the layer graph a cycle.

### 4. The Snowflake integration is deleted, not deprecated

`ingest/eln/warehouse/snowflake.py`, `ingest/sources/eln-snowflake/`, the `snowflake.*` mypy
override and the `("chemclaw.ingest", "crypto")` layering row (its only site was that driver's
key-pair auth) are gone. `eln-databricks` becomes the shipped worked example the binding tests
resolve every path of; `pistachio` beside it is the corpus this is all for.

## Consequences

- **Attaching a database this repository has never heard of is a module plus a manifest.** Proven
  rather than asserted: `tests/test_warehouse_adapter.py` builds a half whose connection block says
  `dsn`, `api_key_env` and `collection` — a vector database's vocabulary, belonging to no shipped
  driver — and asserts the block reached the driver verbatim with only the named secret resolved.
  `tests/test_vector_store.py` does the same for the dense seam with an adapter defined in the test
  module itself.
- **A pasted secret is still caught, for a keyword nobody has written yet.** The check is on the
  `_env` suffix rather than on a list of known credential fields, so a driver inventing
  `service_account_key_env` is covered on the day it is written.
- **The engine tests stopped pinning one vendor's SQL.** `tests/warehouse_fake.py` brings its own
  `FakeVectorDialect`, so what `sql.py` contributes is asserted without a vendor's words in it;
  the shipped driver's real spelling is pinned once against the real dialect and once end-to-end
  through the retriever. That is more coverage of Databricks than existed before, not less.
- **Both `docs/planning/DEFERRED.md` warehouse rows change what they are waiting for**: a Databricks
  tenant rather than a Snowflake one. The seam question they were never about is now settled twice
  over.
- **The `Warehouse` Protocol did not move.** Neither did `VectorStore`. That was the claim
  `D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one` said was worth checking on the next
  vendor; removing one and generalising the block around it is the check.
