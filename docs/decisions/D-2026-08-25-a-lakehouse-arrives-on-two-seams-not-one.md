# D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one — Databricks as a vector store and a warehouse driver, and the four things that were wrong underneath

**Status:** accepted

**Supersedes:** the `note_index` bullet of
[`D-2026-08-08-a-vector-store-is-not-a-catalogue`](D-2026-08-08-a-vector-store-is-not-a-catalogue.md)
(§Consequences). Everything else in that decision stands and is built on here.

## Context

The deployment wants Databricks to hold its data: the ELN, a Pistachio patent-reaction corpus, the
document share and the knowledge graph — searchable by embedding, and written to.

Before this, `grep -ri databricks` over the tree, `docs/` and `git log --all -S` included, returned
nothing; so did `pistachio`. Dense vectors lived in pgvector with one alternative (Qdrant), and the
ELN warehouse engine spoke to one vendor (Snowflake).

The interesting part is that almost none of that needed new architecture. Two seams already existed
for precisely this, each with one implementation and a documented "add another" path:
`chemclaw.retrieval.vectors` (D-2026-08-08) and `chemclaw.ingest.eln.warehouse` (D-2026-08-04). A
lakehouse is not one thing to this system — it is a vector index *and* a SQL warehouse, and those
are two different seams. What made the work worth recording is what attaching the *second*
implementation to each seam exposed about the first.

## Decision

### 1. Two adapters, on the two seams that were built for a second vendor

`retrieval/vectors/databricks.py` implements the three-method `VectorStore` against Mosaic AI Vector
Search. `ingest/eln/warehouse/databricks.py` implements `Warehouse`/`WarehouseCursor` over
`databricks-sql-connector`. Both late-bind their client and neither is in `pyproject.toml`, on the
established terms: a store nobody configured must not weigh on every pod, and this repository's own
suite must not depend on a client only a real deployment has.

Neither seam's interface moved to accommodate them. That is the claim, and it is the one worth
checking on the next vendor.

### 2. A Databricks score is not a cosine, and the adapter converts

`VectorMatch.score` is contractually a cosine in [0, 1]; `retrieval/hybrid.py` fuses on it and both
Postgres indexes apply a `> 0` floor to it. Databricks ranks by `1 / (1 + d²)` over **Euclidean**
distance. Passing that through would mis-rank silently — the same hazard `qdrant.py` records for a
collection built with `Distance.DOT`, arrived at from a different direction.

The conversion is exact **iff both vectors are unit length**:

```
unit vectors -> d² = 2 - 2cos -> score = 1/(3 - 2cos) -> cos = 1.5 - 0.5/score
```

identical → `score 1` → `1.0`; orthogonal → `score 1/3` → `0.0`; opposing → `score 0.2` → `-1.0`,
dropped by the floor. So the adapter L2-normalises on write *and* on query and then inverts the
transform, and the floor is pushed to the server as `score_threshold = 1/3` — the cosine-zero point
expressed in the store's own units.

Normalising is not a rounding nicety. Without it Databricks' L2 *ordering* is not cosine ordering at
all, so the same corpus would disagree with pgvector about which document is nearest, and nothing
anywhere would fail.

### 3. `sql.py` claimed to be dialect-neutral and was not; the dialect moves to the driver

`warehouse/sql.py`'s docstring argues that `placeholder` belongs on the connection *because
parameter style is a dialect fact*. Two functions above it, it hardcoded `VECTOR_COSINE_SIMILARITY`
and `?::VECTOR(FLOAT, n)` — Snowflake's names, in the module that contributes "structure".

Both move onto `Warehouse.vector_dialect`. `SnowflakeVectorDialect` returns the old strings verbatim,
so every exact-statement assertion in `tests/test_warehouse_binding.py` is unchanged — that is the
regression check. A driver answering `None` cannot serve a `vector:` block and says so, naming
itself, instead of emitting another vendor's function for the server to reject.

The sharper half is not the function name but **how a query vector is bound**. Snowflake has a
native `VECTOR` type and binds the list. Databricks has neither a `VECTOR` type nor an array
*parameter*: native parameters are scalars, so a 1536-float vector cannot be bound as a list at all.
It goes as one JSON scalar parsed by `from_json(?, 'ARRAY<FLOAT>')` — which keeps it a bound *value*
rather than statement text, the invariant that module is built on. `ARRAY<FLOAT>` and not
`ARRAY<DOUBLE>`, because `vector_cosine_similarity` accepts only the first.

Only `cosine` is offered. Databricks' names for L2 and inner product are unverified here, and a
guessed function fails on the server rather than naming the metric.

### 4. The knowledge graph joins the seam, and the gap that exposed

D-2026-08-08 left `note_index` on pgvector, reasoning that "generalizing the seam to a second
consumer before the first has run against a live server would be designing against a guess". That
was right about the risk and it is answered from the other direction rather than waived: the seam
did not have to move. `ExternalVectorNoteIndex` uses `upsert`, `search` and `delete` exactly as
`ExternalVectorDocumentIndex` does, and is smaller — two of `NoteIndex`'s five methods overridden,
three inherited — because a note is embedded whole, so the point id *is* the note id and there is no
resolve step.

What the second consumer did find is a gap in `NoteIndex`, not in the seam: **there was no delete,
and `reindex_notes` never pruned.** A note deleted from disk left its row behind. That is genuinely
harmless in Postgres — the retrievers drop a hit whose note no longer loads — and stops being
harmless the moment the vectors live in a store that bills per point and that no other sweep
reaches. `retire_absent(keep)` closes it, phrased as "keep exactly these" because that is what the
caller knows, guarded twice against an empty `keep` so a mis-pointed notes directory cannot wipe an
index that costs one embedding call per note to rebuild, and run *before* the "nothing changed" exit
so a run whose only news is a deletion still acts on it.

`vector_store_provider` remains **one** switch for both corpora. A per-corpus provider would be two
selections to keep consistent, and no deployment has asked for split backends.

### 5. Pistachio is a third-party corpus, and the D-089 line still holds

`tests/test_no_egress.py` pins the shipped source set by hand, and sanctions the warehouse ELN on
the grounds that it is *the deployment's own system* — "not a third-party corpus, which is what
D-089 was about". Pistachio is a licensed third-party corpus, so that row's argument does not cover
it and a new one is owed.

D-089's subject is a **runtime dependency on somebody else's service**: a corpus fetched from a
vendor at question time, whose availability, licensing and content sit outside the deployment. This
is not that. It is the site's own licensed copy, loaded into the site's own lakehouse by the site,
reached with the same credential class and through the same network peer as the ELN beside it,
adding no egress destination that connector does not already have.

Two things hold it there, and both are structural rather than promised. It is **retrieve-only with
no ingest half** — a patent reaction did not happen in this lab and nobody here can vouch for it, so
it is cited as precedent and never becomes a knowledge-graph note. And it ships **disabled**, like
every other source, so a cluster reaches it only by naming it in `CHEMCLAW_DATA_SOURCES`.

### 6. Pistachio ranks in an index, because a scan of it is a scan of ten million rows

`vector_cosine_similarity` is evaluated per row. That is the right shape for an ELN and a full
corpus scan per question at patent scale. So `VectorBinding` gains an optional `index:`, and when it
is set the retriever ranks in the `VectorStore` and queries the relation only to resolve the winning
keys into content — the split `ExternalVectorDocumentIndex` already makes, with Databricks SQL
standing in for Postgres as the catalogue.

Eligibility travels as a set of keys computed in SQL and applied **before** the index's top-k,
because filtering afterwards makes a narrow filter over a wide corpus return nothing at all. A set
is a set, so `vector_store_max_scope_keys` bounds it and an over-broad filter is **refused with the
filter named, never truncated** — a silently cut eligibility set is a wrong answer that reads as a
thin corpus. The scanned and index-ranked paths share one retriever and one branch, because resolve,
rendering, suppression and the entitlement check are identical and a second copy is how they stop
agreeing.

## Consequences

- Adding a vector backend is still one module, one registry branch, one literal value. Adding a
  warehouse is still one module and a manifest. Both claims are now checked by a second
  implementation rather than asserted by a docstring.
- **A second adapter on the vector seam has two silent failure modes, and they are worth stating for
  the third one:** the vendor's score may not be the cosine the contract promises, and a blocking
  client on the event loop stalls every other leg of a `gather`. Neither raises. Both are now in
  `retrieval/vectors/README.md` under "adding another store".
- `note_index` rows are deleted when their note is. That is a behaviour change for pgvector
  deployments too, and the right one: the old behaviour was an accumulation nothing ever reclaimed.
- Two `mypy --strict` errors unrelated to this work were failing `make type` on the base branch and
  are fixed here rather than worked around.
- **Nothing has run against a real Databricks workspace.** Three vendor facts are pinned against
  documentation and a fake rather than a tenant: the `1/(1 + d²)` score formula, the
  `{"column": [values]}` filter form, and `similarity_search`'s response shape (read tolerantly for
  exactly this reason). That is the same position the Snowflake connector has held since
  D-2026-08-04, and it gets the same treatment — a `DEFERRED.md` row naming a workspace as the
  trigger. The score formula is the one to verify first, because it is the one that fails quietly.
- The vendor clients stay out of `pyproject.toml`, so a deployment that reaches Databricks installs
  `databricks-vectorsearch` and `databricks-sql-connector` into its own image. This is the Qdrant
  precedent followed rather than improved on; an extras group was considered and dropped, because
  `deploy/Containerfile` installs with `--no-dev` and an extra nobody installs is a declaration.

## Alternatives rejected

**A driver-specific `options:` pass-through on `ConnectionBinding`.** The obvious way to give
Databricks its own connection vocabulary. Rejected because the five existing fields already name the
concepts — which tenant, which credential, which compute, which namespace — and only the vendor's
word for each differs; a pass-through would add a field to a shared model that exactly one driver
reads. The mapping is documented on the driver instead, and the two fields with *no* analogue
(`private_key_env`, `role`) are refused rather than dropped, so a binding copied from
`eln-snowflake` cannot leave a deployment believing an access restriction is in force.

**A `similarity_function:` field on `VectorBinding`.** Would have let a site point at its own UDF and
avoided touching the engine. Rejected: the closed transform vocabulary exists so that mounting a
manifest directory is not mounting an execution surface, and a free-form SQL expression in a
manifest is exactly that.

**Scanning Pistachio with `vector_cosine_similarity`.** Zero new code — the manifest alone. Rejected
on arithmetic: a per-row similarity over ~10⁷ reactions is a full corpus scan per question. The
scanned path remains the default for a corpus small enough to want it.

**Entries in `tests/test_upstream_surface.py` for the two vendor shapes.** That file's assertions
import their package unconditionally and its version floor calls `version(package)`, so entries
there would make the suite depend on clients this repository deliberately does not carry — the exact
failure `snowflake.py`'s docstring records. The Protocol slice in each adapter is the pinned shape
instead, and the fake is what holds it.

**A Helm secret key for the Databricks credentials.** `DATABRICKS_HOST` and `DATABRICKS_TOKEN` are
not `CHEMCLAW_*` and not `Settings` fields — deliberately, since a binding names its own client's
variables — so `test_chart_config_keys_have_a_consumer` would reject them, and the Snowflake
credentials are outside the chart for the same reason. Both prior external attachments shipped with
no chart wiring at all; this one follows.
