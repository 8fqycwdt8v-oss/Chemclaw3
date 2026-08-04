# `chemclaw.ingest.eln.warehouse` — an ELN whose schema is a file

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
    connection: {driver: ..., account_env: ..., database: ..., schema: ...}
    ingest: {entry: ..., related: [...], reaction: {...}, components: [...], provenance: ...}
    vector: {relation: ..., vector_column: ..., content_columns: [...]}
```

`src/chemclaw/ingest/sources/eln-snowflake/datasource.yaml` is a complete worked example, exercised
against a fixture row by `tests/test_warehouse_binding.py` so it cannot rot into something that only
looks right. Copy it, replace every name in it, and enable the source.

**Put your copy in your own folder.** `CHEMCLAW_DATA_SOURCES_DIR` is an OS-pathsep search path where
the earlier entry wins, so a deployment mounts a directory holding its own manifest and never edits
this repository. Then `make datasource-validate` — and `--construct` on top of it, which builds the
halves and so validates the binding itself rather than just its keyword name.

## The parts

| Module | What it holds |
|---|---|
| `binding.py` | The document's schema. `extra="forbid"`, validated when a half is built. |
| `expr.py` | Paths (`root.COL`, `charges[0].COL`) and the closed transform vocabulary. |
| `sql.py` | Statement construction. Checked identifiers written, every value bound. |
| `driver.py` | The `Warehouse`/`WarehouseCursor` Protocols. No third-party import. |
| `connect.py` | Late-binds the driver, reads credentials from the named variables. |
| `adapter.py` | The ingest half — an `ElnAdapter`. |
| `retriever.py` | The retrieve half — a `SourceRetriever`. |
| `snowflake.py` | The only module that knows a vendor exists. |

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
are all asserted with no tenant, no credentials and no client installed.

That last point is why `snowflake.py` imports its client inside a function rather than at module
scope — the one place in this package that departs from the seam's "import whatever you need at the
top" rule. That rule is about which *process* pays for an import; this is about a package that is
not installed in any of them.
