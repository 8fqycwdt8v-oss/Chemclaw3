# Databricks Migration Assessment — how much of Chemclaw3 could run on Databricks, and what it would cost

> **Scope of this document.** A point-in-time assessment, dated 2026-08-25, answering a question
> that was asked in four escalating steps: (1) how well would this codebase run on Databricks,
> relying on Databricks technology as much as possible for storage and orchestration; (2) the same
> question for the MCP tool fleet and the durable workflows (BO, DFT); (3) where *else* Databricks
> technology could be used; and (4) a concept for a **complete** migration MVP — no Temporal, no
> OpenShift, all Postgres on Databricks, xTB/CREST on Databricks compute, full DFT/HPC dropped.
>
> **Method.** Five parallel read-only surveys of this repository (storage, orchestration,
> infra/identity, the LLM seam plus the light background jobs, and governance/reporting/CI),
> followed by deep web research against current Databricks documentation — because the first pass
> was written from code-reading plus training knowledge, and two of its conclusions turned out to
> be wrong once the live product surface was checked. Every claim below is tagged by its evidence
> class: **[code]** read from this repository, **[docs]** from current vendor documentation,
> **[community]** from a vendor community/practitioner report, or **[unverified]** — a gap that
> needs a live test before anyone relies on it.
>
> **Standing of this document: historical.** It is an assessment, not a decision. No ADR here, no
> backlog rows, nothing in this repository changed as a result of writing it. If a migration is
> ever undertaken, the decisions it requires get their own ADRs, and several of them would have to
> explicitly supersede merged ones (D-043–D-049, F6). Read this for the analysis; do not read it as
> a plan of record.

---

## 1. Verdict in three paragraphs

**Chemclaw3 is architected around two engines Databricks does not provide, on a deployment spine
Databricks does not host.** The engines are Temporal's durable execution (deterministic replay,
exactly-once-effect activities, heartbeat-driven multi-hour polls, resume-mid-workflow) and
LangGraph's per-turn stateful agent graph behind a long-lived SSE connection. The spine is
OpenShift/Kubernetes with Entra identity validated in-cluster. Nothing in the Databricks product
line is a drop-in for any of those four things. That is the honest headline, and no amount of
enthusiasm about the lakehouse changes it.

**Within that constraint, a real and sizeable slice of the system is a strong, low-risk fit.** The
LLM provider swap is genuinely config-only. The two pgvector-backed retrieval indexes map cleanly
onto Databricks Vector Search. Several MCP connectors are already deployment-location-agnostic by
design and could be re-hosted with a one-line manifest change. BoFire BO campaigns are pure Python
with zero orchestration coupling. Two of the scheduled background jobs are trivially portable. The
eval harness would gain a real run-history UI it structurally cannot have today. Snowflake ELN
ingestion has a native federation path. That is not a token list — it is most of the system's
*capability* surface, just not its *spine*.

**A complete migration is nonetheless conceivable, and the research changed the odds twice.**
Lakebase (Databricks' GA managed Postgres) ships pgvector 0.8.0 with HNSW, which means every
current Postgres table — including the bit-vector fingerprint indexes this assessment initially
wrote off — can move without a query rewrite. And DBOS, a checkpoint-to-Postgres durable-execution
library partnered with Databricks since April 2026, is the first credible substitute for Temporal's
core guarantee. Together they make a no-Temporal, no-OpenShift MVP a coherent design rather than a
wish. It remains a large bet on a young dependency, it drops full DFT/HPC entirely, and it
contradicts several standing ADRs — but it is no longer hand-waving, and §7 sets it out concretely.

---

## 2. What the web research corrected

The first version of this assessment was written from code-reading plus training knowledge. Four
of its conclusions did not survive contact with current vendor documentation. Recording the
corrections rather than quietly fixing them, because *which* things a code-only reading gets wrong
is itself the useful finding:

| First-pass claim | Corrected finding | Consequence |
| --- | --- | --- |
| "Fingerprint bit-Jaccard similarity has no Databricks home — needs a UDF brute force" | **[docs]** Lakebase ships **pgvector 0.8.0 with HNSW**, including bit-vector operators | Fingerprint tables lift **unmodified**. Rated Weak → Direct lift. |
| "No Databricks primitive hosts a stateful LangGraph agent behind SSE" | **[docs]** Databricks Apps is an officially recommended LangGraph host, with a reference async-FastAPI chat template and Lakebase-backed session persistence; Model Serving is now called the *legacy* path for agents | Rated None → Moderate, pilot-worthy. |
| "Databricks has no durable execution; Temporal is irreplaceable" | **[docs]** Still true natively — but **DBOS** (checkpoint-to-Postgres durable execution) partnered with Databricks/Lakebase in April 2026 | Not a native feature, but a credible substitute exists. Rated None → pilot-worthy for a narrow slice. |
| "Streamed token usage may be unreported, silently zeroing the budget guard" | **[docs]** Foundation Model APIs return `usage` **in every streamed chunk** | Risk resolved for FMAPI; still worth a smoke test for a *custom* Model Serving endpoint. |

Two product-naming facts that matter for anyone reading older material: **Databricks Workflows is
now Lakeflow Jobs**, and **Delta Live Tables is now Lakeflow Declarative Pipelines** (existing DLT
code runs unmigrated). Ingestion is **Lakeflow Connect**. This document uses current names.

---

## 3. Fit matrix

Ratings are about *this* codebase, not about the products in the abstract. "Effort" is relative
migration cost, not calendar time.

| Component | Today **[code]** | Databricks target | Fit | Effort |
| --- | --- | --- | --- | --- |
| LLM inference (F0 seam) | Generic OpenAI-compatible endpoint | Model Serving / Foundation Model APIs | **Strong** | Config-only |
| `note_index`, `document_chunks` | Postgres + pgvector, HNSW | Vector Search (or Lakebase-pgvector) | **Strong** | Low–Medium |
| Postgres OLTP core (~30 tables) | Self-hosted Postgres | **Lakebase** (not Delta/UC) | **Moderate–Strong** | Medium |
| Stateless MCP connectors (`molfp`, `rxnfp`, calc cache reads) | In-cluster MCP-over-HTTP pod | Databricks Apps | **Good** | Medium |
| BoFire BO campaigns (`science/bo/`) | Temporal activity, `connector-bo` queue | Lakeflow Jobs; engine unchanged | **Good** | Medium |
| `note_index.py`, `retention.py` | Temporal scheduled workflow | Lakeflow Jobs, cron trigger | **Strong** | Low |
| `memory_jobs.py`, `report_workflow.py` | Temporal child-workflow fan-out | Lakeflow Jobs for-each | **Moderate** | Medium–High |
| LangGraph agent + SSE front door | FastAPI, per-turn compiled graph | Databricks Apps | **Moderate** | High |
| Eval harness run history | Committed `baseline.json` | MLflow 3 (GenAI eval/tracing) | **Moderate** | Low–Medium |
| Snowflake ELN ingestion | Custom warehouse adapter | Lakehouse Federation foreign catalog | **Good** | Medium |
| ECFP4/DRFP fingerprints | Postgres bit-vector HNSW | **Lakebase-pgvector** (direct lift) | **Good** | Low |
| Calculation artifact blobs | Postgres `BYTEA` (S3 explicitly declined) | Unity Catalog Volumes | **Good** | Low–Medium |
| SMB/CIFS document share | Mounted PV, POSIX crawl | *No native path* — see §7.4 | **Weak natively** | Redesign |
| Markdown knowledge graph in Git | Git + NetworkX, PR-gated | — | **None** | N/A |
| Report harness (LLM synthesis + PR-gate) | `retrieval/harness.py` | — | **None** | N/A |
| Audit trail / provenance | Append-only Postgres, INSERT-only grant | Unity Catalog lineage | **None — declined** | N/A |
| Temporal durable-execution core | Self-hosted in-cluster | DBOS is adjacent, not equivalent | **None natively** | Rewrite |
| `eln_sync.py`, `document_sync.py` worst case | `continue_as_new` + heartbeat | — | **None** | Stays |
| Nextflow/Seqera HPC (DFT/QM) | Seqera REST → Slurm/AWS Batch | — | **None** | Orthogonal |
| OpenShift Helm (Route, NetworkPolicy, PSA) | Kubernetes-native chart | — | **None** | Discarded |
| Entra identity / WIF | Azure-specific OIDC + WIF | Databricks SSO + OBO | **Moderate** | See §8 |
| Secrets | K8s Secret + ExternalSecret | Databricks secret scopes | **Lateral** | Only if compute moves |

---

## 4. Detailed analysis

### 4.1 Data storage

**Postgres OLTP core.** **[code]** Roughly 30 tables under 48 forward-only migrations
(`infra/sql/`, `chemclaw.core.migrate`): the calculation cache (`calculation_results`, D-011,
which refuses eviction), content-addressed artifact blobs as `BYTEA`
(`science/calc/postgres_artifacts.py` — which explicitly *declined* an S3-compatible bucket to
avoid a fourth secret and an RWX-volume dependency), conversation transcripts (`session_messages`,
`session_events`, `session_turns`, `session_owners`), the append-only `audit_events`,
`note_proposals`, `observations`, `job_records`, `bo_campaigns`, the `predictions`/`measurements`
calibration ledger, `document_files`/`document_chunks`, `note_index`, and LangGraph's own
`checkpoints`/`checkpoint_blobs`/`checkpoint_writes` created by `AsyncPostgresSaver.setup()`
rather than by a migration file (`agent/checkpointer.py`).

The schema is deliberately idiosyncratic: forward-only-additive migrations with checksums
(`tests/test_migrations_are_additive.py`), retention policy encoded in `durable/retention.py`, a
bespoke lease table (`session_turns`) standing in for advisory locks, heavy JSONB payload
interpretation, and only three real foreign keys in the whole schema.

*Fit.* Delta Lake is the wrong target and this is not close: no row-level locks or leases, weak
support for high-frequency single-row upserts, and `AsyncPostgresSaver` requires a real Postgres
wire protocol (`CREATE INDEX CONCURRENTLY`, autocommit transactions). **[docs]** **Lakebase** is
the right target — GA, compute/storage separated, autoscaling, instant branching, Unity Catalog
integration, and positioned by Databricks explicitly as an agent state store and online feature
store. Because it is real Postgres, this becomes a connection-string migration rather than a
redesign. The append-mostly analytical tables (calc cache, audit log, indexes) *could* also live in
Delta if read patterns ever shift to OLAP, but nothing in the system needs that today.

**pgvector.** **[code]** Two vector-bearing tables — `note_index` (`012_note_index.sql`) and
`document_chunks` (`037_document_index.sql`) — both `vector(1536)` plus `tsvector`, HNSW and GIN
indexed, both explicitly derived-and-rebuildable from their source of truth (Git notes, mounted
files). A real abstraction seam exists (`retrieval/vectors/base.py|memory.py|qdrant.py|registry.py`,
`D-2026-08-08-a-vector-store-is-not-a-catalogue`), but the pgvector default path deliberately does
**not** route through it: it is a
single fused SQL statement doing rank, filter and citation-join together, chosen over the abstract
seam for performance.

*Fit.* The cleanest mapping in the system, with one real cost. **[docs]** Databricks Vector Search
is near-drop-in for "derived, rebuildable index," and the `VectorStore` Protocol contains the
change to one new module plus a registry entry. The cost is that the fused query must be
re-decomposed into a similarity call plus a join — undoing the optimization that made pgvector the
default. **[docs]** Note also that Vector Search's index is **L2 internally**; cosine is achieved
by pre-normalizing embeddings, and the metric is not configurable at index creation.

**Fingerprints.** **[code]** `molecule_fingerprints`/`reaction_fingerprints` are Postgres
`bit(2048)` columns with HNSW `bit_jaccard_ops` (pgvector ≥0.7), one shared generic store
(`science/fingerprints/store.py`) computing Tanimoto in SQL.

*Fit.* **[docs]** Vector Search has no bit-vector Jaccard/Tanimoto operator — it is cosine/L2 over
float embeddings — so it is the wrong engine for this specific metric. But **Lakebase ships
pgvector 0.8.0 with HNSW**, so these tables lift **unmodified**. This is the single largest
correction in this assessment: the "weak fit" verdict was an artifact of assuming Delta/Vector
Search were the only Databricks-native options.

**Knowledge graph.** **[code]** Pure filesystem plus Git: Markdown notes with YAML frontmatter,
`[[wikilinks]]` as edges, indexed in-process by NetworkX (`kg/graph.py`), validated by
`kg/validate.py`, written only via the PR-gate (`kg/pr_gate.py`, `kg/git_submitter.py`). No
database at all.

*Fit.* Resists structurally. Delta and Unity Catalog have no PR-review concept and no
graph-traversal primitive; forcing this into tables would mean reconstructing node/edge tables plus
a Spark equivalent of NetworkX traversal, discarding the Git-native record of who approved which
diff. Leave it in Git; optionally mirror derived embeddings into a vector index for the dense
entry points, which is what `note_index` already is.

**ELN / DataSource seam / Snowflake / SMB.** **[code]** `ingest/sources/` is a manifest-driven
plugin seam (a new source is a folder plus an env-var entry, zero core edits, D-120).
`ingest/eln/warehouse/` is a generic SQL-warehouse adapter naming no table and no column, with the
site's schema as a *binding* in the manifest (`D-2026-08-04-the-schema-is-a-file`) and
`snowflake.py` the only vendor-aware module. `ingest/documents/` mounts an SMB/CIFS share as a
`PersistentVolume` — POSIX path only, no client, no credential, no egress
(`D-2026-08-06-a-share-is-mounted-not-called`) — and crawls/chunks/embeds into
`document_files`/`document_chunks` via mark-and-sweep (`ingest/documents/sync.py`).

*Fit.* **[docs]** The seam maps well onto **Lakehouse Federation** — Snowflake registers as a
foreign catalog via OAuth, mirroring its schema into Unity Catalog with query pushdown; note it is
**read-only**, which suits ingestion. The SMB share has **no** native path: Unity Catalog Volumes
and Auto Loader work against cloud object storage only (S3/ADLS Gen2/GCS). §7.4 addresses this.

**Object storage.** **[code]** None exists — deliberately declined in favor of Postgres `BYTEA`.
*Fit.* The one place Databricks is a straightforward upgrade rather than a fight: Volumes is
exactly the object-storage layer this design avoided for infra-minimalism reasons, so moving
artifact blobs there **removes** a documented compromise.

### 4.2 Orchestration and compute

**Temporal.** **[code]** `src/chemclaw/durable/` plus per-connector `workflows.py`/`worker.py`. A
core `background-jobs` queue for light work and one `connector-<name>` queue per bundle that owns
heavy work — `connectors/bo/`, `connectors/qm/` (which took the former core `hpc-jobs` queue),
`connectors/calc/`. `durable/registry.py` provides self-registering decorators; `job_record.py`
persists result envelopes because Temporal's event history is retention-bound and not an archive.
The guarantees are load-bearing, not incidental: deterministic replay, exactly-once-effect
activities with idempotency keys (`qm/hpc/nextflow.py` scopes its `Idempotency-Key` to
`workflow_run_id`), and heartbeat-polls that run for hours.

*Fit.* **[docs]** Lakeflow Jobs is a DAG/cron batch orchestrator with branching, for-each loops,
table-update and file-arrival triggers, and per-task timeouts and retries. It is genuinely good at
what it does and it is **not** a durable-execution engine: no replay, no resume-from-history, no
mid-workflow suspension. Under an incremental program, Temporal stays and Databricks compute is
something Temporal activities *call*. Under a full migration, see DBOS in §7.

**The light `background-jobs` workflows — a split verdict, not a uniform one.** **[code]** The
differentiator is whether a job leans on resume/heartbeat for its *worst case*, not its
steady-state shape:

| Workflow | Shape | Verdict |
| --- | --- | --- |
| `note_index.py` | One bounded pass plus embedding batch | **Strong fit** — trivial idempotent ETL |
| `retention.py` | Per-table SQL sweep | **Strong fit** — literally a scheduled SQL task |
| `memory_jobs.py` | Detect, then fan out N publish children | **Good** — for-each approximates it; coarser per-child isolation |
| `report_workflow.py` | Fan out per section, degrade-not-fail | **Good** — DAG-shaped, but event-triggered and needs failure semantics rebuilt |
| `eln_sync.py` | Self-cursoring, chunked, heartbeats on large drains | **Keep on Temporal** — backfill resume is a real guarantee |
| `document_sync.py` | `continue_as_new`, multi-generation state, TB-scale first crawl | **Keep on Temporal** — built around exactly these primitives |

**[code]** `durable/schedules.py` uses Temporal's `ScheduleIntervalSpec` with a `SKIP` overlap
policy and a deterministic per-job jitter offset. **[docs]** Lakeflow Jobs' cron triggers and
concurrency policies are conceptually equivalent; the jitter is replicable by staggering cron
minutes.

**LangGraph agent layer.** **[code]** `build_langgraph_agent` compiles a fresh graph *per turn* via
`deepagents.create_deep_agent`. Seven `@wrap_tool_call` middlewares nest in a load-bearing order;
`checkpointer.py` implements `SchemaStampedSaver(AsyncPostgresSaver)` on its own autocommit pool.
The coupling to upstream internals is deep and deliberate — `PrivateStateAttr`, `UntrackedValue`,
middleware `.name`-splice ordering — and is pinned by `tests/test_middleware_order.py` and
`tests/test_upstream_surface.py`.

*Fit.* **[docs]** Databricks Apps is an officially supported host for exactly this class of
application: Python and Node runtimes, LangGraph named as a first-class framework alongside the
OpenAI Agents SDK and LlamaIndex, a reference async-FastAPI chat template with an `AgentServer`
and a `/responses` endpoint, MLflow tracing, and **Lakebase-backed chat persistence** so
conversations survive refreshes and redeploys. Databricks explicitly now calls Model Serving the
*legacy* path for agents and recommends Apps. This is a much better fit than a code-only reading
suggested. What does **not** come free: **[code]** the front door's in-process concurrency
admission ledger (`_SlotBoundEventStream` in `api/routes/streams.py`) and per-turn MCP session
pinning assume a multi-pod process model that Apps' single-app compute does not reproduce.

**FastAPI + SSE front door.** **[code]** `GET /sessions/{id}/events` is an unbounded SSE stream
polling the database for the process's lifetime, with per-user/per-pod in-memory admission
ledgers. **[docs]** Databricks is explicit that "a workload that needs to hold a long-lived
connection, listen on a port, or respond to incoming HTTP requests is not a streaming pipeline and
should not run on a serverless job" — but that is guidance about *Jobs*, and Apps is the intended
home for exactly this. Serverless streaming also caps at 7 days, which is irrelevant to Apps but
worth knowing.

**BoFire BO.** **[code]** `science/bo/` is pure Python declared "No Temporal, no MCP" in
`ARCHITECTURE.md`; `connectors/bo/` wraps it as Temporal activities. *Fit.* The best-fitting
compute in the system — the engine runs unmodified as a Lakeflow Jobs task, and only the wrapper
changes. `campaign_record_store.py` already checkpoints campaign state independently, so Lakeflow
Jobs' own task retry is sufficient.

**Nextflow/Seqera HPC.** **[code]** `connectors/qm/hpc/nextflow.py` is an httpx adapter
(`launch_run`/`poll_run`/`fetch_artifacts`) against the Seqera Platform REST API, chosen because a
plain GET-pollable surface survives durable heartbeat-polls without a persistent SSH session
(D-048). *Fit.* Orthogonal. Nextflow targets HPC schedulers; Databricks clusters are not a
first-class Nextflow executor target, and Databricks offers nothing that substitutes for the HPC
bridge. Under the MVP concept this capability is **dropped**, not ported (§7).

**Observability.** **[code]** `core/tracing.py` opens turn/tool spans; `CHEMCLAW_OTEL_LLM_SPANS`
attaches OpenInference's LangChain instrumentation emitting vendor-neutral OTLP, with content
suppressed by default. **[docs]** **MLflow 3** captures prompts, retrievals, tool calls, responses,
latency and token counts, is OTel-compatible, offers built-in and custom LLM judges, and stores
traces in Unity Catalog with no storage cap and SQL queryability. The trace half could ingest with
no first-party change. Prometheus operational counters have no MLflow equivalent and stay separate.

**LLM provider seam (F0).** **[code]** `agent/llm_provider.py`'s `build_chat_model()` is the only
place a chat-client class is imported; the `openai_compatible` branch builds a plain `ChatOpenAI`
from `llm_base_url` + `llm_api_key` + generation options, with `embedding_provider` reusing the
same transport for `/embeddings`, TLS pinnable via `llm_tls_ca_bundle`, and
`llm_fallback_base_url` catching connection/5xx errors. Anthropic-specific prompt-cache breakpoints
already return `[]` for any non-Anthropic provider, so nothing new is lost.

*Fit.* **[docs]** Foundation Model APIs are OpenAI-compatible, support streaming, and return
`usage` **in every streamed chunk** — which resolves the first-pass worry that streamed token
accounting might silently meter zero and disarm the budget guard. Pointing the seam at Databricks
is three settings and no code. The residual risk is tool-calling reliability, which D-039 already
names as the project's top risk independent of provider.

### 4.3 MCP connector fleet

**[code]** Each capability is a bundle: `connectors/<name>/connector.yaml` declares MCP tools,
endpoint, durable jobs with their own task queue, skills and note types; discovery is by folder and
validation by pydantic manifest (`make connector-validate`). Deployment is one Deployment plus
Service per bundle serving MCP-over-HTTP, plus an optional separate Temporal worker. Process
isolation is architecturally load-bearing — it keeps `tblite`, `bofire`/`botorch` and RDKit out of
the chat-service image (D-118) — and is asserted in a subprocess by
`tests/test_connector_isolation.py`.

The decisive detail: the manifest already carries a `url:` escape hatch, proven in production by
`chem` and `safety`, which are hosted by the sibling `Chemclaw3-mcp` repo. "Hosting is a deployment
fact, not a capability fact" (D-118/D-120). That is precisely the seam a Databricks migration
would use.

*Fit.* Split cleanly in two. **Stateless, tool-call-shaped connectors** (`molfp`, `rxnfp`, the
`calc` cache-read path) become Databricks Apps endpoints with a one-line manifest change and a
bearer token — no core-repo code change by design. The **Temporal-worker half** (BO, QM, calc's
CREST/xTB jobs) is a poor fit for the same reason Temporal is: Lakeflow Jobs is triggered batch,
not a long-poll worker with mid-flight cancellation and heartbeat semantics. Note also that today
all in-repo connectors ship in **one image and one Helm chart** (D-117, which deliberately
consolidated a previously fragmented CI), so splitting one out is architectural surgery, not a
flag flip.

### 4.4 Infrastructure, deployment, identity

**[code]** `deploy/` is explicitly OpenShift-native: one rootless UBI9 image with a single
entrypoint dispatching four process roles via `CHEMCLAW_COMPONENT`; a Helm chart declaring
Deployments, Services, an OpenShift `Route` (not an Ingress — not even portable to vanilla
Kubernetes), a default-deny egress `NetworkPolicy`, `PodDisruptionBudget`,
ServiceMonitor/PodMonitor/PrometheusRule, and a pre-deploy migration Job. `values.yaml`'s
`secrets.keys`/`optionalKeys` name exactly six plain Secret refs, pinned by
`tests/test_helm_chart.py` so a seventh cannot appear silently. Temporal is **self-hosted
in-cluster** (D-049), deliberately declining Temporal Cloud to keep workflow payloads carrying the
Entra `oid` inside the OIDC trust boundary.

*Fit.* Direct mismatch. Databricks exposes no Kubernetes surface for customer workloads — no Helm,
no Route, no NetworkPolicy, no PSA, no multi-process-per-pod. Every template in
`deploy/helm/chemclaw/templates/` would be discarded rather than ported. **[docs]** Databricks
Asset Bundles could replace the CI pipeline's *intent* — declarative YAML, multi-workspace and
multi-environment promotion, GitHub OIDC auth, `bundle validate → plan → deploy` — but only if the
connector fleet were actually split into independently deployed units first. Databricks secret
scopes replace the six Secrets one-for-one, but only for Databricks-hosted compute; against the
existing ExternalSecret/SealedSecret plus `pydantic-settings` pipeline that is a lateral move.

Local dev (`infra/docker-compose.yml` — Postgres/pgvector plus a Temporal dev server) is the most
portable piece of the whole stack and neither benefits from nor resists any of this.

Identity gets its own section (§8) because it is the one genuinely unresolved point.

### 4.5 Report harness, evals, governance, CI

**Report harness.** **[code]** `durable/report_workflow.py` fans a report out section-by-section
as durable child workflows over `retrieval/harness.py`'s decompose → retrieve → verify → cite →
synthesize core, pulling from the knowledge graph, hybrid vector/lexical retrieval and
reaction-fingerprint search, and emitting a Markdown note through the PR-gate. *Fit.* **None.**
This is LLM-driven prose synthesis with citation and human review, not tabular BI. Databricks SQL
and AI/BI Dashboards have no analog for "decompose a question, retrieve evidence, verify it,
propose a PR."

**Eval/metric layer.** **[code]** `evals/metric.py`'s `@metric` registry produces
`MetricResult{metric, value, unit, passed, uncertainty, provenance}`; `evals/harness.py`'s
`run_eval()` scores versioned Markdown cases from `data/evals/cases/` into a citable Markdown
table; persistence is `data/evals/baseline.json`, a single committed snapshot, compared by
`evals/baseline.py` and re-run periodically by `durable/eval_drift.py`; CI gates on
`make eval-strict`.

*Fit.* **Moderate and genuinely worthwhile.** The `(case, metric, value, provenance, pass/fail)`
shape maps onto MLflow runs and metrics directly, and **[docs]** MLflow 3 adds run-to-run
comparison, custom scorers and Unity-Catalog-governed trace storage. The payoff is structural: the
current design can only ever compare against *one* prior snapshot. The bespoke gate logic
(`expect_pass`, `regressions()`, `inert_demonstrations()`) is domain logic MLflow does not replace.

**Audit trail and governance.** **[code]** `audit_events` is append-only, enforced by an
INSERT-only grant. The hash chain (`011_audit_hash_chain.sql`, `032_audit_anchors.sql`) was built
and then deliberately **removed** by
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` once GxP
stopped being a layer-1 constraint. The calculation cache keys by
`(calc_type, calc_version, input_hash, params_hash)` with a `provenance` column — lineage by
construction (D-011) — as do `predictions`/`measurements`.

*Fit.* **None, and deliberately so.** Unity Catalog lineage would add a platform dependency for a
problem this codebase examined and consciously declined to solve more heavily. Adopting it would
cut against a merged decision, not extend one. This is a closed question, not a gap.

---

## 5. Databricks capability reference

Condensed from the research, current as of 2026-08. **[docs]** throughout.

| Product | What it is | Relevance here |
| --- | --- | --- |
| **Lakebase** | GA fully-managed Postgres; compute/storage separated, autoscaling, instant branching, UC integration, scale-to-zero closing idle connections. **Postgres 17, pgvector 0.8.0 with ivfflat and hnsw.** Positioned as an agent state store and online feature store. | The linchpin. Makes a full Postgres migration a connection-string change. |
| **Lakeflow Jobs** | Native orchestrator (formerly Databricks Workflows). Quartz cron, table-update and file-arrival triggers, if/else branching, for-each loops, job- and task-level timeouts and retries, UC lineage. | Replaces Temporal Schedules and simple fan-out. Not durable execution. |
| **Lakeflow Declarative Pipelines** | Formerly Delta Live Tables; existing DLT code runs unmigrated. | Available for ingestion transforms; not needed by the MVP. |
| **Databricks Vector Search** | HNSW ANN, **L2 internally** (cosine via pre-normalized embeddings, metric not configurable), Delta Sync Index. | Target for the two retrieval indexes. No bit-Jaccard operator. |
| **Foundation Model APIs / Model Serving** | OpenAI-compatible chat and embeddings; **usage returned in every streamed chunk**; custom-model containers; >25K QPS, <50 ms overhead claimed. Now the *legacy* path for agents. | Config-only LLM swap. |
| **Databricks Apps** | Serverless-architecture app hosting; Python (FastAPI/Streamlit/Dash/Gradio) and Node; OAuth with app **and** user identity; Lakebase resource binding injects Postgres credentials automatically; reference LangGraph chat template. | Host for the front door and stateless connectors. |
| **Lakehouse Federation** | Governed **read-only** foreign catalogs via JDBC pushdown; Snowflake via OAuth. | Snowflake ELN without ETL. |
| **Unity Catalog Volumes / Auto Loader** | File governance and incremental ingestion over **cloud object storage only** (S3/ADLS Gen2/GCS). | Artifact blobs. **No SMB/CIFS.** |
| **MLflow 3** | GenAI tracing (prompts, tools, latency, token counts), built-in and custom LLM judges, UC-governed traces with no storage cap. | Eval history and LLM observability. |
| **Databricks Asset Bundles** | Declarative IaC for jobs, pipelines and Apps; multi-workspace/region/cloud; GitHub OIDC. | CI/CD, but only post-split. |
| **DBOS** *(third-party)* | Durable execution as a library: `@DBOS.workflow`/`@DBOS.step` checkpoint to Postgres; **each step exactly once**, results cached, completed steps never re-executed; workflow ID **is** the idempotency key; scheduled workflows supported. Databricks/Lakebase partnership announced **April 2026**. | The only credible Temporal substitute. Young. |

---

## 6. Incremental adoption program (Temporal and OpenShift retained)

The low-risk reading of the assessment. Every phase is independently reversible; nothing here
touches Temporal, LangGraph, the SSE front door or the OpenShift spine.

**Phase 0 — prerequisites.** Egress `NetworkPolicy` entries for the Databricks workspace endpoints
(Model Serving, Vector Search and the Jobs REST API are distinct); one scoped service principal per
capability rather than a shared token, each declared in `values.yaml` `secrets.keys` following the
existing pattern; a Unity Catalog namespace decision up front, since indexes are named by path and
renaming later is disruptive. *Done when* a smoke request from inside the cluster reaches a trivial
endpoint over the new rule and credential.

**Phase 1 — LLM cutover (config-only).** Point the F0 seam at Foundation Model APIs. Verify
streamed `usage` is non-zero against the actual endpoint chosen (documented for FMAPI; still worth
checking for a custom Model Serving endpoint) and run `make eval-strict` before any real traffic.
Keep `llm_fallback_base_url` on the incumbent through a soak. *Done when* evals pass and the
fallback has been exercised by killing the primary.

**Phase 2 — Vector Search.** Two Delta Sync Indexes; a `databricks_vector_search.py` implementing
the existing `VectorStore` Protocol; decompose the fused rank/filter/citation-join into a
similarity call plus a join and **benchmark it against the fused baseline** — the fusion was a
deliberate performance choice, so regression is the real risk; backfill; cut over via
`retrieval/vectors/registry.py` with pgvector live as instant rollback for a release cycle.

**Phase 2.5 — Lakebase pilot.** Point a non-production `AsyncPostgresSaver` and the `session_*`
tables at Lakebase. Validate leases (`session_turns`) and high-frequency upserts under
compute/storage separation and scale-to-zero connection closing before trusting it.

**Phase 3 — stateless connectors.** `molfp`, `rxnfp`, calc cache reads to Databricks Apps via the
`connector.yaml` `url:` field. Run `make connector-validate` and the isolation suite. *Done when*
the in-cluster pod can be scaled to zero without incident.

**Phase 3.5 — Apps pilot for the front door.** Non-production, single-user, no concurrency ledger —
prove the per-turn compiled graph and per-turn MCP session work inside Apps' compute model before
investing in porting the admission ledger.

**Phase 4 — simple scheduled jobs.** `note_index.py` and `retention.py` to Lakeflow Jobs cron
triggers; disable the Temporal Schedules only after a full production cycle at parity, and disable
rather than delete. Then, separately, `memory_jobs.py`/`report_workflow.py` as for-each DAGs,
accepting coarser per-branch failure isolation — which is a behavior change and deserves its own
ADR. Explicitly do **not** migrate `eln_sync.py` or `document_sync.py`.

**Phase 5 — BO.** `science/bo/` unchanged; replace the Temporal wrapper with a Jobs trigger.
*Done when* a fixed-seed campaign reproduces the Temporal baseline suggestion-for-suggestion.

**Phase 6 — evals to MLflow 3.** Log runs alongside `baseline.json` for a release cycle, then move
drift comparison onto MLflow's run history. *Done when* a real regression has been caught through
the new path before the old one is retired.

**Phase 7 — Snowflake federation (optional).** Only if the warehouse adapter is a maintenance
burden. The manifest-as-binding pattern survives; validate against the existing fake driver first.

---

## 7. The complete-migration MVP concept

The scope the question ultimately asked for: **no Temporal, no OpenShift, all Postgres on
Databricks, xTB/CREST on Databricks compute, full DFT/HPC dropped entirely.** This is a
from-scratch target architecture, not a patch on §6 — where the two disagree, this section is the
one that was asked for.

### 7.1 Substitution principles

1. Temporal's durable execution → **DBOS** decorators checkpointing to Lakebase. The mapping is
   direct: a Temporal workflow ID is an idempotency key, and so is a DBOS workflow ID; a Temporal
   activity is retried until it succeeds and never re-run after it does, and so is a DBOS step.
2. Temporal Schedules and fan-out → **Lakeflow Jobs** cron triggers and for-each tasks, calling
   DBOS-decorated Python.
3. All Postgres → **Lakebase**, as a wire-compatible lift including pgvector.
4. OpenShift → **Databricks Apps** for anything that serves HTTP; Lakeflow Jobs for anything that
   runs on a schedule. No residual cluster.
5. HPC/DFT → **out of scope**, not bridged.

### 7.2 Target mapping

| Today | MVP target |
| --- | --- |
| Temporal workflow / activity | `@DBOS.workflow` / `@DBOS.step`, checkpointed to Lakebase |
| Task queues and worker fleet | Lakeflow Jobs tasks (or Apps background processes) running DBOS code |
| Temporal Schedules | Lakeflow Jobs scheduled triggers |
| Child-workflow fan-out | Lakeflow Jobs for-each calling DBOS workflows |
| Heartbeat + `continue_as_new` | DBOS per-step checkpointing: decompose into idempotent steps, resume replays only what did not complete |
| `interrupt()` / plan-approval suspension | **Open** — needs a spike against DBOS's workflow messaging primitives |
| Temporal mTLS between workers | No separate worker fleet to secure; service principals plus UC grants |
| ~28 Postgres tables incl. fingerprints | **Lakebase**, direct lift (pgvector 0.8.0 covers bit-Jaccard) |
| `note_index`, `document_chunks` | **Databricks Vector Search** (Delta Sync Index) — see 7.3 |
| LangGraph checkpointer | Same `AsyncPostgresSaver`, repointed at Lakebase |
| FastAPI + SSE front door | **Databricks Apps**, reference LangGraph template as scaffolding |
| Stateless connectors | Databricks Apps via the `url:` seam |
| **xTB/CREST** | **Databricks compute directly.** Short xTB single-point/optimization calls stay the stateless primitives they already are, behind an Apps or Model Serving endpoint. Longer CREST conformer searches run as a Lakeflow Jobs task on a cluster carrying the binaries, DBOS-wrapped if a single search runs long enough to want crash-resume. No scheduler handoff anywhere. |
| **Full DFT/QM via Nextflow/Seqera** | **Dropped.** Not migrated, not bridged, not deferred-with-a-plan. If DFT is needed later it lives entirely outside this architecture, wherever HPC access already exists. |
| SMB/CIFS share | **Push-based uploader → Lakebase** — see 7.4 |
| Snowflake ELN | Keep the existing adapter for MVP; federation is later polish |
| Knowledge graph in Git | Unchanged — orthogonal to all of this |
| OpenShift/Helm | Retired entirely |
| Entra identity | See §8 |

### 7.3 Why Vector Search for the two indexes but Lakebase-pgvector for fingerprints

Both could run on Lakebase-pgvector, and that is the lower-effort option. The MVP nonetheless
splits them, using each engine for what it is actually good at: `note_index` and `document_chunks`
are large, growing, embedding-driven retrieval that benefits from a managed ANN service decoupled
from OLTP write traffic; the fingerprint tables are fixed-shape bit-vector Jaccard search tightly
coupled to point lookups already happening on the same OLTP connection — and Vector Search has no
bit-Jaccard operator at all. The cost of the split is the fused-query decomposition described in
§6 Phase 2, which is the one real engineering item in this substitution.

### 7.4 SMB/CIFS — solved by reversing the direction

Databricks cannot reach into an SMB share, and with OpenShift retired there is no sidecar left to
mount one. Reversing the flow makes the problem trivial: instead of Databricks pulling, something
on the share's own side of the network **pushes**.

1. A small uploader — a scheduled script on any host that already has SMB access today, not a
   service needing its own platform — walks the share, diffs against a content-addressed manifest
   reusing the dedup logic `document_files` already implements, and pushes new or changed files to
   **Lakebase** over outbound Postgres-over-TLS as `BYTEA` rows, the same pattern
   `calculation_artifacts` already uses. Only outbound connectivity is required; nothing reaches
   back into the share's network.
2. A Lakeflow Jobs cron task — plain batch, no DBOS needed — reads newly landed rows, extracts,
   chunks and embeds exactly as `ingest/documents/sync.py` does today, and writes `document_chunks`
   in Lakebase, which syncs into the Vector Search index.
3. This also dissolves the `document_sync.py` durability problem: the TB-scale first crawl now
   happens locally against the mount with a simple local checkpoint, so **nothing on the Databricks
   side needs durable-execution guarantees for this workflow** — only a scheduled batch job over
   already-landed rows.
4. Honest trade-off: this is periodic sync at the same mark-and-sweep cadence as today, not
   real-time ingestion. That matches existing behavior rather than regressing from it.

### 7.5 Build sequence

1. **Foundations** — provision Lakebase (confirm pgvector 0.8.0), provision Apps, and prototype
   DBOS with a deliberate mid-step crash to prove resume-not-repeat *before* anything depends on it.
2. **Data** — lift all Postgres tables to Lakebase; repoint every connection string including the
   checkpointer; re-run the vector and fingerprint queries unmodified and confirm parity.
3. **Agent and front door** — LangGraph inside Apps from the reference template; port per-turn graph
   compilation and MCP session handling; validate SSE streaming and Lakebase-backed sessions.
4. **Lowest-risk durable workflow** — `note_index.py` as a DBOS workflow on a Lakeflow Jobs cron
   trigger, with real failure injection.
5. **BO campaigns** — `science/bo/` unmodified under a DBOS wrapper; fixed-seed parity check.
6. **xTB/CREST** — a cluster (or Apps/Model Serving endpoint for short calls) carrying the
   binaries; numerical validation against current `Chemclaw3-mcp` results on a fixed test set.
7. **Stateless connectors** — via the `url:` seam.
8. **SMB uploader and chunk/embed job** — any time after steps 2 and 3; blocks nothing.
9. **Identity spike** — §8; sequenced last because nothing above depends on it.
10. **Retire OpenShift** — only after everything above has run in production for a full cycle with
    no fallback traffic. This is the one step with no rollback.

### 7.6 Honest risk summary

Three concentrations of risk. **DBOS is a young dependency** for a codebase that has already been
burned once by trusting a framework's documented behavior over measuring it — the Microsoft Agent
Framework rewrite (`D-2026-08-10-langgraph-rebuild-of-the-conversation-layer`) turned on two
*silent* defects, and
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` reverted an upstream
adoption a day after making it. The prototype-with-induced-crash in step 1 is not ceremony. **The
identity redesign has no fallback** once the cluster is gone. And **every headline change here
contradicts a standing ADR** — D-049 (self-hosted Temporal inside the OIDC trust boundary), F4
(Entra throughout), F6 (OpenShift delivery) — each of which would need to be formally superseded
by a new ADR rather than quietly bypassed, per this repository's own append-only decision
discipline.

---

## 8. Identity — the one genuinely open point, investigated

### 8.1 What exists today

**[code]** `api/auth.py::require_principal` reads `Authorization: Bearer`, validates the RS256
signature against the tenant JWKS via a per-endpoint cached `PyJWKClient`, checks **audience**
(`entra_audience` — explicitly the confused-deputy guard, because the front door is both an OAuth
client and a protected resource) and **issuer**, requires `exp`, and builds
`Principal(oid, upn, roles)`. Roles merge Entra app-role claims with security-group claims,
namespaced by `GROUP_ROLE_PREFIX` because the same set gates privileged tools and skills — with an
explicit warning path for the group-claim *overage* case where Entra emits `_claim_names` instead
of `groups`. An unknown `kid` may force a JWKS refresh at most once per cooldown, so an
unauthenticated caller cannot choose how much outbound work the service does. Validation runs in a
worker thread because a blocking fetch on the event loop would freeze every SSE stream.
`agent/authz.py::require_actor()` then enforces reject-if-absent before any durable work starts.

### 8.2 What Databricks offers

**[docs]** Two complementary mechanisms:

- **Apps OBO forwarding.** An app combines its own service-principal identity with the signed-in
  user's, forwarded into the runtime as `x-forwarded-access-token`. Apps declare minimum required
  scopes (`sql`, `files`, `genie`, …) which the user consents to, and **Databricks blocks anything
  outside the approved scopes even if the user has the underlying permission**. Every app gets
  `iam.current-user:read` and `iam.access-control:read` by default.
- **Entra as the account SSO IdP, with SCIM.** Entra supports OIDC and SAML 2.0 for Databricks
  account SSO, and account-level SCIM syncs users and groups automatically.

### 8.3 Recommended design

Keep Entra as the account SSO identity provider and source of truth; have the front door validate
the Databricks-forwarded token instead of a raw Entra JWT. Entra still decides who exists and what
groups they are in — only the point of validation moves.

### 8.4 What the research resolved

1. **The forwarded token is a self-contained, locally-verifiable JWT — not opaque.** **[docs]** It
   is RS256/ES256-signed, and Databricks' own guidance is to validate audience and issuer claims.
   That is the *same shape* as today's `validate_token()`, which means `api/auth.py`'s cached-JWKS
   architecture — including the `kid`-refresh cooldown defense — is **largely reusable**, repointed
   at a different JWKS endpoint, audience and issuer. A materially smaller redesign than feared.
2. **Cross-app replay is prevented, and more tightly than today.** **[docs]** Audience and issuer
   tie the token to Databricks; per-app declared-and-consented scopes then bound what it can do at
   all. This is *stronger* than the single audience check currently guarding the confused-deputy
   case.
3. **Identity and group resolution is a built-in API call, not a token claim.** **[docs]**
   `iam.current-user:read` and `iam.access-control:read` are default scopes on every app. So
   `_principal_from_claims`' single-decode design splits in two under Databricks: decode the JWT
   for subject/audience/issuer, then a follow-up call for entitlements — an extra request per turn
   (or per session, if cached) that today's design does not pay. Worth budgeting deliberately,
   since the whole point of the cached JWKS client was keeping the hot path off the network.
4. **Licensing is a real prerequisite.** **[docs]** Group provisioning requires **Entra ID
   Premium**; user provisioning works on any edition. Databricks needs the **Premium plan or
   above**. A procurement item, not an implementation detail.

### 8.5 New constraint surfaced

**[docs]** The Entra→Databricks SCIM connector **does not support nested groups** — Microsoft
Entra automatic provisioning does not sync them, and the documentation explicitly says not to try.
If any current privileged-role or file-share security group is nested rather than carrying direct
user members, it will silently fail to sync. **This needs an audit of the actual Entra group
structure before cutover** — the failure mode is quiet, and the affected users would be exactly
those with the most access.

### 8.6 Operational gotcha for the runbook

**[community]** Enabling on-behalf-of authorization on an **already-deployed** app does not take
effect on redeploy — the app must be fully **stopped and started**. The OAuth consent screen can
additionally be masked by browser caching; test in a private window. Recorded here so that a
missing `x-forwarded-access-token` during a pilot is not mistaken for a design flaw.

### 8.7 What remains unverified

**[unverified]** Whether `iam.access-control:read` resolves group membership at the same
granularity as today's Entra app-role plus AD-security-group model, or whether Databricks' own
permission vocabulary requires a remapping. Documentation alone does not settle this; it needs a
live test against a real workspace. Until it is settled, identity is a de-risked and concretely
testable design — not a closed one.

---

## 9. Open questions, consolidated

| # | Question | Class | Blocks |
| --- | --- | --- | --- |
| 1 | Does `iam.access-control:read` group granularity map onto Entra app-roles plus AD groups? | **[unverified]** | Identity cutover |
| 2 | Are any current Entra security groups nested (SCIM will not sync them)? | **[unverified]** | Identity cutover |
| 3 | Does DBOS provide a workable analog to `interrupt()`-style human-in-the-loop suspension for the plan-approval gate? | **[unverified]** | Full-migration MVP |
| 4 | Does DBOS's step-checkpoint model actually survive a multi-hour job with induced mid-run crashes at this codebase's scale? | **[unverified]** | Full-migration MVP |
| 5 | Does decomposed Vector Search retrieval stay within latency tolerance of the fused pgvector query? | **[unverified]** | Vector Search cutover |
| 6 | Do Lakebase's scale-to-zero connection semantics interact safely with the `session_turns` lease pattern? | **[unverified]** | Lakebase cutover |
| 7 | Does a *custom* Model Serving endpoint honor `stream_options` for usage accounting? (Confirmed for FMAPI.) | **[unverified]** | LLM cutover |
| 8 | Is Databricks Premium plus Entra ID Premium licensing available? | **[docs]** — confirmed required | Identity cutover |

Every one of these is answerable by a bounded experiment. None is answerable by argument, which is
the point of listing them separately from the analysis.

---

## 10. Bottom line

Databricks is a strong fit for a specific, identifiable subset of this codebase: pure BO and
ML-shaped batch compute, vector-search-backed retrieval, the LLM provider itself, the stateless MCP
connectors, several simple scheduled jobs, Snowflake federation, artifact blob storage, and — as a
real if non-architectural win — the eval harness's run history. It is a weak-to-nonexistent fit for
what structurally defines the system: Temporal's durable-execution semantics, the LangGraph agent
and its stateful SSE front door, the Kubernetes-native deployment and connector-hosting model, the
Nextflow HPC bridge, and an audit layer this codebase has deliberately kept light.

Under an incremental program, that means running Databricks *alongside* Temporal and Kubernetes as
an additional compute, storage and model backend — a worthwhile program with a hard ceiling.

Under the complete-migration MVP, Lakebase and DBOS remove the two blockers that made "no Temporal,
no Postgres, no OpenShift" incoherent a year ago, and dropping DFT/HPC removes the third. What
remains is a large bet on a young durable-execution dependency, one unresolved identity question,
and three merged ADRs that would have to be formally superseded. That is a legitimate thing to
prototype. It is not a thing to start by deleting the Helm chart.

---

## Sources

Vendor documentation and announcements consulted 2026-08-25.

- [Azure Databricks Lakebase is Generally Available](https://www.databricks.com/blog/azure-databricks-lakebase-generally-available)
- [Lakebase Postgres | Databricks on AWS](https://docs.databricks.com/aws/en/oltp/projects/)
- [Postgres extensions (Lakebase)](https://learn.microsoft.com/en-us/azure/databricks/oltp/projects/extensions)
- [PostgreSQL compatibility (Lakebase)](https://docs.databricks.com/aws/en/oltp/instances/query/postgres-compatibility)
- [Databricks AI Search / Vector Search](https://docs.databricks.com/aws/en/ai-search/ai-search)
- [Foundation model REST API reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)
- [Databricks Foundation Model APIs](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/)
- [Lakeflow Jobs](https://docs.databricks.com/aws/en/jobs)
- [Configure and edit tasks in Lakeflow Jobs](https://docs.databricks.com/aws/en/jobs/configure-task)
- [Control the flow of tasks within Lakeflow Jobs](https://docs.databricks.com/aws/en/jobs/control-flow)
- [From Apache Airflow to Lakeflow Jobs](https://www.databricks.com/blog/from-airflow-to-lakeflow-data-first-orchestration)
- [What happened to Delta Live Tables (DLT)?](https://docs.databricks.com/aws/en/ldp/where-is-dlt)
- [Streaming on serverless compute](https://docs.databricks.com/aws/en/compute/serverless/streaming)
- [Run federated queries on Snowflake (OAuth)](https://docs.databricks.com/aws/en/query-federation/snowflake)
- [Connect to external databases and catalogs](https://docs.databricks.com/aws/en/query-federation)
- [What are Unity Catalog volumes?](https://docs.databricks.com/aws/en/volumes/)
- [Using Auto Loader with Unity Catalog](https://learn.microsoft.com/en-us/azure/databricks/ingestion/cloud-object-storage/auto-loader/unity-catalog)
- [MLflow 3 for GenAI](https://docs.databricks.com/aws/en/mlflow3/genai/)
- [MLflow 3.0: Build, Evaluate, and Deploy Generative AI with Confidence](https://www.databricks.com/blog/mlflow-30-unified-ai-experimentation-observability-and-governance)
- [Key concepts in Databricks Apps](https://docs.databricks.com/gcp/en/dev-tools/databricks-apps/key-concepts)
- [Configure authorization in a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)
- [Author an agent and deploy it on Databricks Apps](https://docs.databricks.com/aws/en/agents/custom-agents/author-agent)
- [Migrate an agent from Model Serving to Databricks Apps](https://docs.databricks.com/gcp/en/generative-ai/agent-framework/migrate-agent-to-apps)
- [databricks/app-templates — e2e-chatbot-app-next](https://github.com/databricks/app-templates/blob/main/e2e-chatbot-app-next/README.md)
- [Implement fine-grained permissions for Databricks Apps with on-behalf-of-user authorization](https://community.databricks.com/t5/technical-blog/implement-fine-grained-permissions-for-databricks-apps-with-on/ba-p/116884)
- [Databricks Apps — X-Forwarded-Access-Token not available (community)](https://community.databricks.com/t5/administration-architecture/databricks-apps-x-forwarded-access-token-not-available/td-p/126074)
- [Authenticate with an identity provider token (OAuth token federation)](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-exchange)
- [Configure a federation policy](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-policy)
- [SSO to Databricks with Microsoft Entra ID](https://docs.databricks.com/aws/en/security/auth/single-sign-on/azure-ad)
- [Configure SCIM provisioning using Microsoft Entra ID](https://docs.databricks.com/aws/en/admin/users-groups/scim/aad)
- [Sync users and groups from your identity provider using SCIM](https://docs.databricks.com/aws/en/admin/users-groups/scim)
- [Databricks Asset Bundles CI/CD guide](https://kanerika.com/blogs/databricks-asset-bundles/)
- [Simplify AI agent orchestration with Lakebase Postgres](https://www.databricks.com/blog/simplify-ai-agent-orchestration-lakebase-postgres)
- [DBOS announces technology partnership with Databricks](https://www.prnewswire.com/news-releases/dbos-inc-announces-technology-partnership-with-databricks-to-increase-agentic-ai-reliability-and-trust-302735341.html)
- [Building Durable Agents with DBOS and Databricks](https://www.dbos.dev/blog/building-durable-agents-dbos-databricks)
- [DBOS Workflows documentation](https://docs.dbos.dev/python/tutorials/workflow-tutorial)
- [dbos-inc/dbos-transact-py](https://github.com/dbos-inc/dbos-transact-py)
