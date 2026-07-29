# `sources/` — attaching a corpus

A **data source** is somewhere records come from or evidence is retrieved from: the knowledge
graph, an ELN drop directory, a warehouse table. It is the sibling of `connectors/`, and the line
between them is worth stating because it decides where new work goes:

- A **connector** contributes *capability* — work whose result is a value (a computed energy, a
  hazard screen). It runs in its own process and is reached over MCP or Temporal.
- A **data source** contributes *corpus* — records to ingest, or evidence to retrieve. It runs
  in-process, in whichever process needs that half of it.

## Adding one

Three steps. None of them is an edit to core Python.

**1. Write the adapter** (only if no existing one fits). An ingest half implements `ElnAdapter`
(`fetch_new_entries`, `map_to_ord`); a retrieve half implements `SourceRetriever` (`name`,
`retrieve`). Both protocols are re-exported from `sources/base.py`. Put it wherever it belongs —
a warehouse client belongs beside its peers, not in this folder.

**2. Declare it** as `sources/<name>/datasource.yaml`:

```yaml
name: eln-snowflake            # must equal the folder name — it is the enable token
description: >-
  The corporate ELN, read from the Snowflake reactions view.
ingest: eln.snowflake_adapter:SnowflakeElnAdapter    # module:callable
config:                        # keyword arguments for that callable
  warehouse: RND_WH
  schema: ELN_PROD
```

Declare `ingest:`, `retrieve:`, or both. A source with neither is rejected.

**3. Enable it**: add the name to `CHEMCLAW_DATA_SOURCES`. Discovery is not enablement — the repo
ships every source, a deployment runs the subset it has validated.

Then run `make datasource-validate`.

## Two properties this layout exists to hold

**A half is imported only where it is used.** `ingest:` and `retrieve:` are strings, resolved when
that half is about to be built — so the ELN sync worker never imports a retriever and the chat
process never imports a database driver. Measured: asking which sources to ingest went from 836
modules (including `rdkit`, `drfp`, `numpy`, `psycopg`) to 292 with no heavy third-party at all.
This is the reason the manifest exists, and `tests/test_datasource_isolation.py` is what keeps it
true. **Corollary for an adapter author:** import whatever you need at module scope. Only the
processes that use your half will ever load it.

**Configuration lives with the thing it configures.** A deployment adds a source by mounting a
directory of manifests and putting it first in `CHEMCLAW_DATA_SOURCES_DIR` (an OS-pathsep list,
earlier wins) — no image rebuild, no config-model change. This replaced a discriminated union of
pydantic models in `chemclaw/config.py`, where a second ELN drop directory cost a new model, a new
arm of the union and a new branch in core (D-076 → D-120).

## What is *not* a data source

**Anything that writes to the knowledge graph.** Notes enter through the PR-gate, where a human
signs off before a merge — that is the GxP line. `graph` is retrieve-only for this reason, not by
omission, and a source cannot acquire a write path by declaring one.

**An ELN's retrieve half, when its records already become notes.** `eln-json` and `eln-ord` are
ingest-only: reactions flow in and become graph notes, which the `graph` source then retrieves.
Carrying a retriever as well would surface every ingested reaction twice.
