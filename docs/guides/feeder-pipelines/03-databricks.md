# 03 — The Databricks implementation

Everything here satisfies `01-contract.md`; nothing here adds a requirement. Stages 1 and 2
(acquire, normalise) are `02-acquisition.md` — this file is how they, plus the embed and publish
stages, become a scheduled Databricks job.

**Pick this platform when** the corpus already lives in the lakehouse, the volume is large enough to
want a cluster that scales and then goes away, or the embedding model is one the workspace serves
(which removes the parity problem outright — `01-contract.md` §3.3(a)).

**Do not pick it when** the embedding model lives behind ChemClaw3's internal LLM gateway and the
workspace cannot reach it. Then stage 3 moves to OpenShift (`04-openshift.md` §6) and Databricks keeps
stages 1, 2 and 4.

> Version note: the Vector Search SDK is mid-rename (`databricks.vector_search` →
> `databricks.ai_search`) — ChemClaw3's own adapter tries both. Check the calls below against the SDK
> pinned in your workspace before the first run; the *shape* is what this file is asserting.

---

## 1. The catalog layout

```
<catalog>
└── <schema>
    ├── raw_<source>            append-only, exactly as received          (02 §2)
    ├── raw_<source>_rejected   quarantined rows + reason                 (02 §2)
    ├── releases                one row per consumed release              (02 §6)
    ├── v_reaction              THE CONTRACT RELATION                     (01 §1)
    └── reaction_index          Vector Search index over it               (01 §4.2)  [index shape]
```

Two grants, and they are not the same principal (`01-contract.md` §6):

```sql
-- the feeder's service principal
GRANT USE CATALOG ON CATALOG <catalog> TO `<feeder-sp>`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY, SELECT ON SCHEMA <catalog>.<schema> TO `<feeder-sp>`;

-- ChemClaw3's principal: read only. It must not be able to write the corpus it reads.
GRANT USE CATALOG ON CATALOG <catalog> TO `<chemclaw-sp>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<chemclaw-sp>`;
GRANT SELECT ON TABLE <catalog>.<schema>.v_reaction TO `<chemclaw-sp>`;
```

## 2. The contract relation

```sql
CREATE TABLE IF NOT EXISTS <catalog>.<schema>.v_reaction (
  reaction_id        STRING  NOT NULL COMMENT 'stable across releases; the join key everywhere',
  reaction_smiles    STRING  NOT NULL COMMENT 'reactants>agents>products — agents KEPT',
  patent_number      STRING           COMMENT 'the citation; a row without one is skipped',
  document_id        STRING           COMMENT 'citation fallback',
  title              STRING,
  publication_date   DATE,
  publication_year   INT,
  temperature_c      DOUBLE,
  time_h             DOUBLE,
  yield_pct          DOUBLE,
  workup_text        STRING,
  product_smiles     STRING           COMMENT 'only used by the where: predicate',

  -- what the corpus already classifies; empty is normal, the labeller fills it (01 §1.3)
  namerxn_name          STRING,
  namerxn_class         STRING,
  rxno_id               STRING,
  mapped_reaction_smiles STRING,

  -- the vector, on the scan shape. Omit on the index shape (01 §4).
  reaction_vector    ARRAY<FLOAT>     COMMENT 'CHEMCLAW_EMBEDDING_DIM floats; FLOAT not DOUBLE',
  embedding_model    STRING           COMMENT 'what built it — nothing checks this, 01 §3.2',

  -- freshness. The column ChemClaw3s where: narrows on (01 §5)
  load_date          DATE    NOT NULL,
  release_id         STRING  NOT NULL,
  ingested_at        TIMESTAMP NOT NULL
)
USING DELTA
PARTITIONED BY (load_date)
TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

Four notes on choices that are not free:

- **`ARRAY<FLOAT>`, not `ARRAY<DOUBLE>`.** `vector_cosine_similarity` takes `ARRAY<FLOAT>`, and
  Databricks has no `VECTOR` type. ChemClaw3's driver knows this and will emit SQL the server rejects
  if the column is the other one.
- **Partitioned on `load_date`** so the `where:` window in `01-contract.md` §5 is a partition prune
  rather than a scan. This is the single change that keeps a daily re-walk affordable at ten million
  rows.
- **Column casing is what Spark returns**, and the binding matches exactly (`01-contract.md` §1.5).
  Declare in lower case, write the binding in lower case, and check with `DESCRIBE TABLE`.
- **`reaction_id` is `NOT NULL` and you should enforce uniqueness yourself** — Delta will not. The
  `MERGE` in §3 does it as a side effect only if the source is already deduplicated; a source with
  duplicate keys fails the `MERGE` with a multiple-match error, which is the behaviour you want.

## 3. Stage 4 — publishing, idempotently

`MERGE` on the key. This is what makes a re-run free and a correction land in place.

```sql
MERGE INTO <catalog>.<schema>.v_reaction AS target
USING staged_reactions AS source
  ON target.reaction_id = source.reaction_id
WHEN MATCHED AND (
       target.reaction_smiles IS DISTINCT FROM source.reaction_smiles
    OR target.yield_pct       IS DISTINCT FROM source.yield_pct
    OR target.embedding_model IS DISTINCT FROM source.embedding_model
  ) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
```

**The `WHEN MATCHED AND (...)` guard is load-bearing**, not an optimisation. An unconditional
`UPDATE SET *` rewrites `load_date` on every row it touches, every day — which re-presents the entire
corpus to ChemClaw3's `where:` window and turns the daily incremental walk back into a full one. Only
rows that actually changed should get a new `load_date`.

Conversely, when you *do* correct a row, bump `load_date` — that is the only mechanism that makes
ChemClaw3 re-read it (`01-contract.md` §5).

## 4. Stage 3 — the embedding

Three routes, and `01-contract.md` §3.3 is where the choice is argued. In Databricks:

### 4.1 Warehouse-owned model, scan shape — the one with no parity problem

Embed with a function the workspace serves, and set the *same* function in the binding:

```sql
UPDATE <catalog>.<schema>.v_reaction
SET reaction_vector = CAST(<your_embedding_function>(reaction_smiles) AS ARRAY<FLOAT>),
    embedding_model = '<the model that function serves>'
WHERE reaction_vector IS NULL AND load_date >= current_date() - INTERVAL 7 DAYS;
```

```yaml
# the ChemClaw3 side
vector:
  relation: v_reaction
  key: reaction_id
  vector_column: reaction_vector
  content_columns: [reaction_smiles, title, patent_number]
  metric: cosine
  embedding: server
  server_embed_function: <your_embedding_function>
  server_embed_model: <its model argument, if it takes one>
```

Now one model produces both the corpus vectors and the query vector, and there is nothing to keep in
step. **This is the recommended shape wherever the corpus is small enough to scan** (~10⁶ rows).

### 4.2 Feeder calls ChemClaw3's embedding endpoint

Correct at any scale, and it is the only route that gives an *index-shaped* corpus true parity,
because the index path always embeds the query locally. It requires the job's compute to reach
`CHEMCLAW_LLM_BASE_URL`. Batch, and respect the endpoint's concurrency:

```python
# a Python task; one call per batch, not per row
import os, requests

BASE  = os.environ["CHEMCLAW_LLM_BASE_URL"].rstrip("/")
MODEL = os.environ["CHEMCLAW_EMBEDDING_MODEL"]
TOKEN = dbutils.secrets.get("chemclaw", "llm_token")  # noqa: F821 — Databricks builtin

def embed(texts: list[str]) -> list[list[float]]:
    """One /embeddings call. Returns unit-length vectors, in input order."""
    response = requests.post(
        f"{BASE}/embeddings",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": MODEL, "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    return [normalise(item["embedding"]) for item in response.json()["data"]]

def normalise(vector: list[float]) -> list[float]:
    """L2-normalise. Required for the index shape (01 §4.2); harmless on the scan shape."""
    magnitude = sum(component * component for component in vector) ** 0.5
    return vector if magnitude == 0.0 else [c / magnitude for c in vector]
```

If the workspace cannot reach that endpoint, **do not substitute a different model.** Move stage 3 to
OpenShift (`04-openshift.md` §6) or take route 4.1.

### 4.3 A model the feeder hosts

Only with the `embedding_model` column, a written-down expected value, and the probe in
`05-operations.md` §2.4. Nothing else will ever tell you the two sides diverged.

## 5. The vector index (index shape only)

Skip this section entirely on the scan shape.

```python
from databricks.vector_search.client import VectorSearchClient

client = VectorSearchClient()
client.create_delta_sync_index(
    endpoint_name="<the endpoint CHEMCLAW_VECTOR_STORE_ENDPOINT_NAME names>",
    index_name="<catalog>.<schema>.reaction_index",
    source_table_name="<catalog>.<schema>.v_reaction_index_source",
    pipeline_type="TRIGGERED",
    primary_key="id",
    embedding_dimension=1536,                  # == CHEMCLAW_EMBEDDING_DIM
    embedding_vector_column="embedding",       # self-managed: we supply the vectors
)
```

The source table exists **only** to give the index the three columns the adapter requires, with the
values `01-contract.md` §4.2 requires:

```sql
CREATE OR REPLACE VIEW <catalog>.<schema>.v_reaction_index_source AS
SELECT
  reaction_id           AS id,          -- must equal the relation's key:
  reaction_vector       AS embedding,   -- unit length (01 §4.2)
  reaction_id           AS group_key    -- ALSO the key. Not a typo; see below.
FROM <catalog>.<schema>.v_reaction
WHERE reaction_vector IS NOT NULL;
```

**`group_key = reaction_id` is the requirement most likely to be got wrong**, because the name invites
you to put a category in it. `DatabricksVectorStore.search` always requests
`columns=[id, group_key]`, and a filtered search sends eligibility as `{group_key: [<key values>]}` —
key values resolved from the warehouse. An index whose `group_key` holds a project code or a year
returns **zero hits for every filtered search** and full results for every unfiltered one, which reads
as "the filter is strict" rather than as a fault.

`pipeline_type="TRIGGERED"` and a sync at the end of the job keeps the index a function of a committed
table rather than of a mid-run one. `CONTINUOUS` is viable and costs a always-on pipeline.

**Direct Vector Access instead** (`create_direct_access_index`) if you would rather upsert the index
than sync it — `01-contract.md` §4.3 has the trade. Either works because ChemClaw3 only *reads* a
corpus index. What does **not** work is a Delta Sync index with Databricks-managed embeddings: it
would embed with its own model while ChemClaw3 sends a vector built by `CHEMCLAW_EMBEDDING_MODEL`.

## 6. The job — a Databricks Asset Bundle

```yaml
# databricks.yml — deploy with `databricks bundle deploy -t prod`
bundle:
  name: chemclaw-feeder-<source>

variables:
  catalog:   {default: <catalog>}
  schema:    {default: <schema>}
  warehouse: {default: <sql-warehouse-id>}

resources:
  jobs:
    feeder_<source>:
      name: "chemclaw-feeder-<source>"

      # 06:00 in the site's timezone: after the upstream's nightly publish, before the working day.
      # ChemClaw3's own reaction-corpus Schedule then picks it up within its 1440-minute cadence.
      schedule:
        quartz_cron_expression: "0 0 6 * * ?"
        timezone_id: "Europe/Berlin"

      # A daily job that overlaps itself corrupts its own watermark. Refuse, do not queue.
      max_concurrent_runs: 1
      queue: {enabled: false}

      timeout_seconds: 21600          # 6h. A run that needs longer needs a smaller page budget.

      email_notifications:
        on_failure: ["<the-team-alias>"]
        # A feeder that stops running is indistinguishable from a corpus nobody added to.
        on_duration_warning_threshold_exceeded: ["<the-team-alias>"]
      health:
        rules:
          - metric: RUN_DURATION_SECONDS
            op: GREATER_THAN
            value: 10800

      job_clusters:
        - job_cluster_key: feeder
          new_cluster:
            spark_version: "<LTS runtime>"
            node_type_id: "<a memory-tier node>"
            autoscale: {min_workers: 2, max_workers: 8}
            data_security_mode: SINGLE_USER
            runtime_engine: PHOTON

      tasks:
        - task_key: acquire            # 02 §8 — fetch, verify, land raw
          job_cluster_key: feeder
          python_wheel_task:
            package_name: chemclaw_feeder
            entry_point: acquire
            parameters: ["--catalog", "${var.catalog}", "--schema", "${var.schema}"]

        - task_key: normalise          # 02 §9 — raw -> staged, reject to quarantine
          depends_on: [{task_key: acquire}]
          job_cluster_key: feeder
          python_wheel_task: {package_name: chemclaw_feeder, entry_point: normalise}

        - task_key: embed              # §4 — only rows whose vector is null or stale
          depends_on: [{task_key: normalise}]
          job_cluster_key: feeder
          python_wheel_task: {package_name: chemclaw_feeder, entry_point: embed}

        - task_key: publish            # §3 — the guarded MERGE, then the release row
          depends_on: [{task_key: embed}]
          job_cluster_key: feeder
          python_wheel_task: {package_name: chemclaw_feeder, entry_point: publish}

        - task_key: sync_index         # §5 — index shape only
          depends_on: [{task_key: publish}]
          job_cluster_key: feeder
          python_wheel_task: {package_name: chemclaw_feeder, entry_point: sync_index}

        - task_key: verify             # 05 §2 — the probes, as a task that fails the run
          depends_on: [{task_key: sync_index}]
          sql_task:
            warehouse_id: ${var.warehouse}
            file: {path: sql/verify.sql}
```

Three properties of that job worth stating, because each corresponds to a failure this shape prevents:

- **`max_concurrent_runs: 1` with the queue disabled.** Two runs advancing one watermark is how rows
  go missing. Refusing is recoverable; queueing behind a hung run is not.
- **`publish` is a separate task from `embed`.** A run that dies after embedding has cost money and
  written nothing; re-running it re-embeds only the rows whose vector is still null.
- **`verify` fails the run.** A feeder whose verification is a dashboard is a feeder nobody checks.

## 7. Secrets

```sh
databricks secrets create-scope chemclaw
databricks secrets put-secret chemclaw upstream_token   # the vendor's, for stage 1
databricks secrets put-secret chemclaw llm_token        # only if taking route 4.2
```

ChemClaw3's own credential is **not** here. It lives wherever ChemClaw3 runs, named by the manifest's
`access_token_env` (`01-contract.md` §6), and it is a read-only principal.

## 8. The ChemClaw3 side

Once the relation exists, attaching it is a manifest and a name — no code change (D-120):

```yaml
# <your manifest dir>/<source>/datasource.yaml   (mount the dir FIRST on CHEMCLAW_DATA_SOURCES_DIR)
name: <source>
description: >-
  <what this corpus is, and what it is not>. Precedent and prior art — cited as evidence, never
  ingested as knowledge.

retrieve: chemclaw.ingest.eln.warehouse.retriever:WarehouseVectorRetriever

labels:
  provides: [named-reaction]      # only what a column actually supplies (01 §1.3)
  override: []

config:
  binding:
    connection:
      driver: chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse
      server_hostname_env: DATABRICKS_HOST
      access_token_env: <SOURCE>_DATABRICKS_TOKEN
      warehouse_id: <sql-warehouse-id>
      catalog: <catalog>
      schema: <schema>
      query_timeout_seconds: 60

    vector:
      index: <catalog>.<schema>.reaction_index     # or vector_column: reaction_vector
      relation: v_reaction
      key: reaction_id
      content_columns: [reaction_smiles, title, patent_number, publication_year]
      metric: cosine
      embedding: local
      filter_columns: {since: publication_date, until: publication_date}
      suppress_ingested: false

    corpus:
      relation: v_reaction
      key: reaction_id
      order_by: reaction_id
      # the freshness window — 01 §5. Widen it for a backfill, put it back afterwards.
      where: "product_smiles IS NOT NULL AND load_date >= current_date() - INTERVAL 7 DAYS"
      fetch_limit: 2000

      smiles:       {path: root.reaction_smiles}
      citation:     {path: root.patent_number, fallback: {path: root.document_id}}
      published_on: {path: root.publication_date, transform: [{iso_date: {}}]}
      temperature_c: {path: root.temperature_c, transform: [{number: {}}]}
      time_h:        {path: root.time_h, transform: [{number: {}}]}
      yield_percent: {path: root.yield_pct, transform: [{number: {}}, {clamp: {min: 0, max: 100}}]}
      workup_text:   {path: root.workup_text}
      named_reaction: {path: root.namerxn_name}
```

Then:

```sh
export CHEMCLAW_DATA_SOURCES_DIR=/etc/chemclaw/sources:/app/src/chemclaw/ingest/sources
export CHEMCLAW_DATA_SOURCES=<source>,<whatever else was enabled>
make datasource-validate                                   # offline; no connection
uv run python -m chemclaw.cli.validate_datasources --construct   # ... and the binding really builds
make schedules-apply                     # picks up reaction-corpus for the new source
```

The chart also has to let the pods reach the workspace: `networkPolicy.egressDestinations` must name
it, or the release must state `allowAnyDestination: true`. The chart **refuses to render** until one
of the two is set (`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob`).

## 9. Sizing, and what it costs

The three costs, in the order they surprise people:

| Cost | Driver | What moves it |
| --- | --- | --- |
| Embedding | one call per **new or corrected** reaction | the `WHERE reaction_vector IS NULL` guard in §4; without it you re-embed the corpus daily |
| The daily re-walk | ChemClaw3 reading your relation | the `load_date` partition + `where:` window (§2, `01-contract.md` §5) |
| The index | rows × dimension, plus the endpoint | `TRIGGERED` over `CONTINUOUS` unless freshness inside the day matters |

The drain reads pages of `min(fetch_limit, CHEMCLAW_CORPUS_PAGE_SIZE)` rows — 1000 by default — up to
`CHEMCLAW_CORPUS_SYNC_MAX_ITERATIONS` (100) per workflow run before `continue_as_new`, with a
900-second activity budget per page. Ten million rows is ~10 000 pages: fine as a resumable drain,
ruinous as a daily one. That is the whole argument for the window.
