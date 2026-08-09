# D-2026-08-08-a-source-is-named-by-its-folder-not-by-its-half — the registry tells a retrieve half which source it is

**Status:** accepted

## Context

An extensibility audit asked, of every surface that changes on a cadence, what adding one actually
costs — measured rather than read. The data-source seam measured well: a second JSON-ELN drop is one
`datasource.yaml` folder in a mounted directory plus its name in `CHEMCLAW_DATA_SOURCES`, and zero
edits to this repository (D-120). Then the same experiment was run with two *mounted shares*, and it
did not hold.

`SourceRetriever.name` is how the rest of the system identifies a corpus. The document index
partitions on it (`document_files` is keyed `(source, path)`), the sweep deletes by it,
`gather_evidence` cites with it, and `retrieval_source_weights` is keyed on it. Nothing supplied it.
`_build_half` called `factory(**manifest.config)`, and `config` carries no name — so the three
*parameterised* halves, the ones where one engine serves many instances, each answered with a
literal default:

| half | default |
| --- | --- |
| `ShareDocumentRetriever` | `"sharedrive"` |
| `WarehouseVectorRetriever` | `"warehouse"` |
| `VendoredDatasetRetriever` | `"vendored"` |

Three docstrings said the opposite — *"name: The retriever id; the registry passes the source's
name."* Reading the ADR, the migration comment and the docstring together produces a confident and
wrong picture of a working two-share deployment. Running it produces this:

```
enabled in config: sharedrive, sharedrive-eu
active_retrieve_sources() -> 'graph', 'sharedrive', 'sharedrive'   # both shares, one name
share_sources() keys      -> ['sharedrive']                        # two collapse to ONE
  sharedrive -> mount /mnt/sharedrive-eu                            # last one wins
```

So the first share is never crawled, silently. And with two real shares and the in-memory index:

```
crawl A -> indexed=1 rows=1
crawl B -> indexed=1 rows=2
sweep after B's drain -> removed=1
rows after sweep: ['Docs/beta.md']       # share A's document is gone
```

`infra/sql/037` describes that failure precisely, as the thing its composite key prevents:

> Keyed by (source, path), not path alone: two mounted shares can hold the same relative path
> (`Projects/report.pdf` is not an unusual name), and a global key would silently let the second
> share's crawl overwrite the first's row and then sweep it.

The key was right. The value handed to it was not. And `make datasource-validate` passed the
configuration, so nothing anywhere objected.

## Decision

**The registry passes `name=manifest.name` to every retrieve half.** A source's identity is the
folder name `CHEMCLAW_DATA_SOURCES` enables, and a half does not get an opinion about it.

Three things follow, and each was a choice with a live alternative:

**Passed, not stamped.** The obvious alternative was to assign `half.name = manifest.name` after
construction, which needs no change to any factory. It was rejected because it accepts a half that
refuses to be named — which is exactly how this defect survived: nothing objected to a retriever
that named itself. Passing it makes "a retrieve half is told which source it is" a *contract*, and a
half that does not accept it fails when the source is built, naming the source, both at startup and
at `make datasource-validate`.

**Passed to every half, not only the parameterised ones.** The four class-attribute retrievers
(`graph`, `vector`, `lexical`, `vendored`) do not need one. They get it anyway, because a
conditional pass is a rule the next half added can fall outside of. Each keeps its own name as the
parameter's *default*, so direct construction in a test or a script is unchanged.

**Required where a default could only ever be right once.** `ShareDocumentRetriever` and
`WarehouseVectorRetriever` now require `name`. Their old defaults were correct for the first
instance and wrong for every subsequent one, which is the worst shape a default can have: it works
in every test and fails in exactly the deployment that motivated the feature.

`_check_half` in the datasource validator binds against what the registry actually passes — config
for an ingest half, config plus `name` for a retrieve half. Binding config alone would have called a
correct retriever broken and a broken one correct, in both directions the opposite of the truth.

## Consequences

- **Two shares are now two corpora.** Both are crawled, each under its own name, and the
  `(source, path)` key partitions what it was written to partition.
- **The `eln-snowflake` source's retrieve half is now named `eln-snowflake`, not `warehouse`.** This
  is a visible change to citations and to any `retrieval_source_weights` key — but it is what the
  enable token always said, nothing in the repository referenced the old literal, and the warehouse
  ELN has no live tenant, so no stored data is keyed on it. The shipped share keeps the name
  `sharedrive` because its folder is `sharedrive`, so no migration is needed for the one deployment
  shape that exists.
- **A retrieve half added later must accept `name`.** That is the point; the failure is loud, at
  build time, and names the source.
- **No collision is representable.** `_source_dirs` dedupes on folder name, so two enabled sources
  cannot share one — the invariant no longer needs a validator rule because it cannot be violated.
- `tests/test_datasource_seam.py` holds three regressions, each verified to fail without the fix:
  a half's default losing to the manifest, two instances of one engine getting distinct names, and a
  half that refuses a name being rejected by build.
