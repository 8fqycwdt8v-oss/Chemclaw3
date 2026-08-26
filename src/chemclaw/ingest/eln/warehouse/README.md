# `chemclaw.ingest.eln.warehouse` — an ELN whose schema is a file, on any database

Every other adapter here names its source's fields in Python. `json_adapter` knows a payload has
`reaction_smiles`; `ord_adapter` knows ORD's shape. That works because both formats were fixed
before the adapter was written.

A corporate warehouse is the opposite case. Its tables exist before anyone here sees them, they are
site-specific, and they grow. Writing an adapter against them means writing it on the day access
arrives — and editing it every time a column lands.

So the schema moves into data. **A binding** says which relation holds reactions, which columns
carry the sync cursor, which child tables hang off it, and which column becomes which field of
`OrdReaction`. This package executes it. Nothing in it names a table or a column.

## Attaching one

```yaml
# src/chemclaw/ingest/sources/<name>/datasource.yaml
ingest: chemclaw.ingest.eln.warehouse.adapter:WarehouseElnAdapter
retrieve: chemclaw.ingest.eln.warehouse.retriever:WarehouseVectorRetriever
config:
  binding:
    connection: {driver: <module:callable>, <that driver's own keyword arguments>}
    ingest: {entry: ..., related: [...], reaction: {...}, components: [...], provenance: ...}
    vector: {relation: ..., vector_column: ..., content_columns: [...]}
```

`src/chemclaw/ingest/sources/eln-databricks/datasource.yaml` is a complete worked example, exercised
against a fixture row by `tests/test_warehouse_binding.py` so it cannot rot into something that only
looks right. Copy it, replace every name in it, and enable the source.

## The connection block is the driver's signature

Everything under `connection:` except `driver:` is a keyword argument of the callable `driver:`
names, in that database's own words — `server_hostname`/`warehouse_id`/`catalog` for a lakehouse,
`host`/`port`/`sslmode` for a Postgres, `uri`/`collection` for a vector database. There is no shared
model enumerating them, deliberately: those vocabularies have no union worth writing, and the one
that was written here (Snowflake's) made the *second* driver redefine three of its fields and refuse
two more (`D-2026-08-26-the-driver-s-signature-is-the-schema`).

A key ending `_env` holds the **name** of an environment variable, read at connect time. That is the
only convention the block keeps, because it is a rule about secrets rather than about a vendor.

What checks a key is real is the callable itself: `make datasource-validate` resolves the driver and
binds the block against its signature, offline, before anything connects. So attaching a database
this repository ships no driver for is a module exposing a `Warehouse` — see `driver.py` for the
three methods — plus a manifest naming it, and no edit anywhere in this package.

**Put your copy in your own folder.** `CHEMCLAW_DATA_SOURCES_DIR` is an OS-pathsep search path where
the earlier entry wins, so a deployment mounts a directory holding its own manifest and never edits
this repository. Then `make datasource-validate` — and `--construct` on top of it, which builds the
halves and so validates the binding itself rather than just its keyword name.

## The parts

| Module | What it holds |
|---|---|
| `binding.py` | The document's schema. `extra="forbid"` everywhere but `connection:`, which is the driver's. Validated when a half is built. |
| `expr.py` | Paths (`root.COL`, `charges[0].COL`) and the closed transform vocabulary. |
| `sql.py` | Statement construction. Checked identifiers written, every value bound. |
| `driver.py` | The `Warehouse`/`WarehouseCursor`/`VectorDialect` Protocols. No third-party import. |
| `connect.py` | The `Warehouse` contract over `chemclaw.core.connect`, under this seam's error. |
| `adapter.py` | The ingest half — an `ElnAdapter`. |
| `retriever.py` | The retrieve half — a `SourceRetriever`. |
| `databricks.py` | The one module that knows a vendor exists: Databricks SQL over Unity Catalog. |

## The similarity search is the driver's, not the engine's

`placeholder` was always on the connection, because parameter style is a dialect fact. The
similarity *call* is one too, and until D-2026-08-25 it was not treated as one: one vendor's
function names and its `?::VECTOR(FLOAT, n)` cast sat in `sql.py`. Both now come from
`Warehouse.vector_dialect`, and a driver that offers none cannot serve a `vector:` block — it says
so, naming itself, rather than emitting SQL another server will reject.

The sharper half is how a query vector is *bound*, not what the function is called. A warehouse with
a native vector type binds the list against a cast. Databricks has no array parameter type at all,
so the vector goes as one JSON scalar that `from_json(?, 'ARRAY<FLOAT>')` parses server-side — still
a bound value, which is the invariant `sql.py` exists to hold.

## Two ways to rank, and the corpus size decides

`vector_column:` ranks by scanning the relation: a similarity function per row, right for an ELN.
`index:` ranks in a Mosaic AI Vector Search index through `chemclaw.retrieval.vectors` and queries
the relation only to resolve the winning keys into content — the split
`ingest/documents/external_index.py` makes, with the warehouse as the catalogue. Use it when a scan
is not affordable: at ~10⁷ rows a per-row similarity is a full corpus scan per question.

A binding declares exactly one of the two; naming both is refused rather than silently resolved. On
the index path a filter becomes a set of eligible keys computed in SQL and sent *before* the top-k
(filter after the cut and a narrow filter over a wide corpus returns nothing), bounded by
`CHEMCLAW_VECTOR_STORE_MAX_SCOPE_KEYS` and refused rather than truncated when it overflows.

## Four decisions worth knowing before you write a binding

**The transform vocabulary is closed.** `number`, `scale`, `value_map`, `iso_date`, `iso_datetime`,
`regex`, `strip`, `upper`, `lower`, `default`, `clamp` — and nothing else. A binding is a
configuration file; if a transform name could reach arbitrary code, mounting a manifest directory
would mean mounting an execution surface. An unknown name fails when the binding loads, not when a
row reaches it.

**A path that does not resolve is silence, not an error.** A NULL column, an absent optional child
table and a view that dropped a column are the same thing to a binding. Whether silence is
acceptable is the *field's* question: `reaction_id` and a component's `smiles` refuse it, everything
else records that the source said nothing.

**Credentials are named, never carried.** `*_env` fields hold the name of an environment variable,
read at connect time — so a rotated key is picked up by the next connection, and the document is
safe to keep in a repository. The names are deliberately not `CHEMCLAW_`-prefixed: these are the
warehouse client's own credentials, not settings of this application.

**Unmapped columns survive.** `attributes:` carries whatever the row held that no field took, into
`OrdReaction.attributes` and the end of the note body. That is what makes a column added next
quarter visible without anyone editing this package — and it is bounded, because a wide view would
otherwise put a hundred unmodelled lines into every note.

## Why the ingest half and the retrieve half are both here

`eln-json` and `eln-ord` are ingest-only, and `ingest/sources/README.md` gives the reason: a
file-drop ELN ingests everything it sees, so carrying a retriever as well would surface every
reaction twice.

A warehouse ELN is a different shape. It ingests a curated slice — the reactions worth a reviewed,
merged note — of something much larger, and the rest has no other way in. It also already holds an
embedding per reaction, so searching it in place costs one query, while copying those vectors into
this system's index would mean re-embedding a bigger corpus and keeping the copy fresh forever.

The original rule survives intact through `suppress_ingested`, which drops a hit whose reaction
already became a note. What reaches the agent is: reviewed knowledge for the curated part, raw
warehouse rows for the rest, and never both for the same reaction.

## Testing without a warehouse

`driver.py` is Protocols and nothing else, which is what lets `tests/warehouse_fake.py` exist. It
serves canned rows and records the exact statement it was sent, so the cursor predicate, the
child-table fan-out, unit conversion, vocabulary mapping, attribute bounding and similarity ordering
are all asserted with no tenant, no credentials and no client installed. Its dialect is the fake's
own, so those assertions carry no vendor's spelling; the shipped driver's real one is pinned in
`tests/test_databricks_warehouse.py` and end to end in `tests/test_warehouse_retriever.py`.

That last point is why `databricks.py` imports its client inside a function rather than at module
scope — the one place in this package that departs from the seam's "import whatever you need at the
top" rule. That rule is about which *process* pays for an import; this is about a package that is
not installed in any of them.
