# D-2026-08-28-a-feeder-writes-a-table-and-nothing-else — recurring corpus pipelines run outside ChemClaw3, and the contract between them is a relation

## Status

Accepted. Does not change any code. It fixes a boundary that four existing decisions imply and none
of them states, and it names the documentation that carries the contract:
`docs/guides/feeder-pipelines/`.

Related: D-120 (a source is a manifest folder),
`D-2026-08-04-the-schema-is-a-file`, `D-2026-08-26-the-driver-s-signature-is-the-schema`,
`D-2026-08-25-a-corpus-is-evidence-not-an-eln`, `D-2026-08-25-a-cache-is-not-a-record` (the sink
seam, whose reasoning this mirrors in the opposite direction), and D-089 (no runtime dependency on a
third-party service).

## Context

Every seam this system has for *getting a corpus* assumes the corpus is already somewhere it can
read. `chemclaw.ingest.eln.warehouse` executes a binding against a relation that exists; the
`corpus:` block walks it by keyset; the `vector:` block searches "the embedding the warehouse already
holds" (`binding.py`, `VectorBinding`'s own docstring). `pistachio/datasource.yaml` is explicit that
what it describes is "the site's own licensed copy, loaded into the site's own lakehouse **by the
site**".

**Nothing in this tree loads it.** That was correct while the only corpora were an ELN this
organisation already runs and a vendor release an operator drops in once a quarter. It stops being
sufficient the moment a site wants a *daily* feed — new reaction SMILES and their identifiers,
pulled from an upstream database over a URL, with an embedding computed for each one — because that
is recurring work with a schedule, a watermark, failure modes and a cost, and recurring work with no
owner gets built wherever somebody is standing.

Where somebody is standing is a connector. The shape is seductive: a connector already has a
manifest, a Temporal queue, a worker and a bundle directory, and `background-jobs` already carries
periodic work. A `feed` bundle with a daily Schedule and an `httpx` client would work on the first
day and be wrong on every day after it.

Three rules already in the tree say why, and each of them was written for a different reason:

- **`Chemclaw3-mcp` forbids request-time egress outright**, at four independent layers, because
  production is air-gapped. A fetcher cannot live in the tool fleet at all.
- **D-089 forbids a runtime dependency on a third-party service** here. The ELN and warehouse
  sources are sanctioned against it precisely because they are *the deployment's own systems*,
  reached with the deployment's own credential. A nightly pull from a vendor's URL is the thing that
  rule is about.
- **Durability lives in Temporal, and Temporal's queue is this system's** (D-002, and stricter since
  layer 1 gained a checkpointer). A feeder's failures — a 502 from a vendor, a licence expiring, a
  release with a changed column — are not this system's failures, and putting them on
  `background-jobs` makes an outage in somebody else's infrastructure look like an outage in the
  agent.

There is also a plain operational argument. A backfill of ten million patent reactions is a
compute-shaped, restartable, hours-to-days job that wants a cluster that scales and then goes away.
The `background-jobs` worker is a long-lived pod sized for light periodic work; the ADR that added
the parent ceiling to those Schedules and the one that gave every activity a
`schedule_to_start_timeout` are both about keeping that queue from wedging.

## Decision

**A feeder runs outside ChemClaw3, and the entire contract between it and this system is a relation
in a database ChemClaw3 does not own — plus, where the corpus is large enough to need one, a vector
index beside it.** No API call, no queue, no shared library, no callback.

That is deliberately the *same* shape the two existing outward seams already have, and stating it as
a third case is what keeps the three from collapsing into each other:

- a **connector** produces a value, and ChemClaw3 calls it;
- a **data source** supplies a corpus, and ChemClaw3 reads it;
- a **sink** consumes what this system produced, and ChemClaw3 writes to it;
- a **feeder** fills what a data source reads, and ChemClaw3 **never knows it exists**.

The last clause is the whole decision. A feeder has no manifest here, no name in any settings list
and no row in any registry, because every one of those would be a coupling that buys nothing: the
binding already names the relation, and a relation that is fresh and a relation that is stale are
the same relation to everything downstream. What replaces the coupling is a written contract with
the properties a reader of it can check, and the two validators that already exist
(`make datasource-validate`, and the `--construct` half's binding build) as the offline
gate on the ChemClaw3 side of it.

**The document is `docs/guides/feeder-pipelines/`**, and it is normative where it states a
requirement: `01-contract.md` is the file a site's data engineer is handed, and every requirement in
it cites the module that enforces it, so it can be checked rather than believed.

## Consequences

**What a site gains.** The feeder is written in whatever the site's data platform already is — a
Databricks job, an OpenShift CronJob, an existing Airflow — with its own release cadence, its own
on-call and its own budget, and attaching it to ChemClaw3 remains what D-120 promised: a manifest
folder and one name in `CHEMCLAW_DATA_SOURCES`.

**What this repository owes in exchange**, and did not before: the contract has to be *exact*,
because there is no shared type to get it wrong against. Three properties turned out to be
invisible from the ChemClaw3 side and are now written down where the person building the feeder will
read them:

1. **Embedding parity is unenforced on this path.** `document_chunks` and `note_index` carry an
   `embedding_key` and self-heal when the model changes (038/039). A warehouse corpus carries no
   such column, and nothing compares the model that built the corpus vectors against
   `embedding_config_key()`. A mismatch is not an error — it is a silently meaningless cosine. Where
   the corpus is scanned rather than indexed, `embedding: server` removes the problem by
   construction, and the guide says so.
2. **A Databricks corpus index must declare `group_key`, equal to the binding's `key:`.**
   `DatabricksVectorStore.search` always requests `columns=[id, group_key]`, and a filtered search
   sends eligibility as `{group_key: [...]}` holding warehouse key values.
3. **Vectors a feeder writes must be L2-normalised**, because the adapter inverts Databricks'
   `1/(1 + d²)` back to a cosine assuming unit length on both sides.

**What it costs.** Two systems now have to be operated to keep one corpus fresh, and the seam
between them is a table rather than a type — so a column renamed upstream is caught by
`validate_datasources --construct` and a *value* gone wrong is caught by nobody. That is the
trade the binding already makes ("a path that does not resolve is silence, not an error"), extended
one hop further out. `05-operations.md` is where the probes that close it live.

**What it forbids.** A connector bundle, a data source or an MCP server whose job is to fetch. If a
future decision wants one, it supersedes this ADR rather than adding an exception — and it has to
answer the three rules in the Context first.
