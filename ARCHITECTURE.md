# Architecture and repository map

Start here if you are trying to find something. `README.md` is how to run it; this is what the
directories are and why they are separate.

**The one rule that makes the tree navigable: `src/` is all the code. Everything beside it is
data, configuration or documents.**

## The four layers

Four layers, each with one responsibility. The rule that matters is that their concerns never
merge — `CLAUDE.md` states it, and `tests/test_layering.py` enforces the parts a machine can check.

1. **LangGraph** — conversation orchestration and short reasoning steps.
2. **Temporal** — durable execution of long or expensive jobs. Durability for that work lives here
   and *only* here, never in the conversation layer's own ad-hoc stores; layer 1's checkpointer
   holds turn state and nothing else, which is the line D-2026-08-10 §3 draws once layer 1 has a
   checkpointer at all. Two kinds of task queue: `background-jobs` (light: sync, re-index, reports)
   and one per connector bundle that owns durable work, each sized for that work. Once a result is
   persisted it is never recomputed. Concurrent misses on the *same* key in one process share one
   computation — `cached_compute` single-flights them behind an in-flight future, eight together
   measured one compute — while misses in different processes still each compute; that
   cross-process half is a `docs/planning/DEFERRED.md` row, not a bug the code hides.
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data and relations).

Skills hold judgment; connectors hold capability (deterministic tools). Anything the agent
generates enters the graph **directly**, labelled `created_by: agent`, and is corrected rather than
pre-approved (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). What still waits for a human
is a change to the agent's own behaviour — a skill — reviewed by an admin.

## The code: `src/chemclaw/`

| Subpackage | Layer | What it is |
| --- | --- | --- |
| `core/` | — | The shared kernel: config, database, HTTP, ids, logging, errors, embeddings, the Temporal client, the one way to attach a database this system does *not* own (`connect.py` — resolve a `module:callable` driver, read its `*_env` credentials, used by ingest, publish and the vector-store registry alike), the process metrics registry, the bounded-LRU (`bounded.py`, the one eviction policy the R3.2 consolidation gave the four call sites that used to hand-roll it — the front door's session/budget/rate-limiter state and the agent's attachment store), and the ambient-turn primitives every layer stamps or reads (identity, session id, turn signals, the capability-tool registry). Everything imports it; it has zero module-scope import of a sibling, and exactly one declared *lazy* (function-scope) exception — `core/logging.py`'s redaction filter, which resolves connector bearer-token env-var names by importing `connectors.registry` inside a function so the value can be redacted from logs. `tests/test_layering.py` enforces both halves. |
| `agent/` | 1 (LangGraph) | Conversation orchestration: the compiled graph (`agent/langgraph_agent.py`), its tool surface, the Postgres turn-state checkpointer, sessions, identity, authorization, the plan/execute harness, and the scratchpad and durable memories a turn writes through the filesystem tools. |
| `api/` | 1 (LangGraph) | The FastAPI + SSE front door that serves `agent/` over HTTP, behind OIDC. `create_app` (`api/app.py`) is the sole composition root; the routes themselves live one level down in `api/routes/`, one module per resource (R3.2 split of a ~1100-line closure — see `api/routes/README.md`). |
| `durable/` | 2 (Temporal) | Workflows, activities, and the `background-jobs` worker that hosts them. |
| `connectors/` | 2 + 3 | The capability seam. One bundle per capability (`chem`, `calc`, `bo`, `results`, `safety`, `molfp`, `rxnfp`, `rxnpredict`), each colocating its `connector.yaml` manifest, its Temporal worker, its own `skills/`, and — for the ones this release still runs — its MCP tool server. Adding a capability is adding a directory here — no core edit. **A bundle without a `server/` is a declaration we do not run**: `chem`'s, `safety`'s and `rxnpredict`'s capabilities are `Chemclaw3-mcp`'s, and what stays here is the manifest four validators resolve tool names through — plus, for `safety`, its `skills/` tree, because judgment is layer 3 and does not follow the engine — and the chart's `connectors.<name>.url` saying where to dial it (D-2026-08-09). |
| `science/` | — | The pure-computation engines: `bo` (BoFire), `fingerprints` (ECFP4/DRFP + Tanimoto), `labels` (the derived reaction-label index every faceted precedent question is asked of — roles, named reactions, conditions, and the substructure screen), and what is left of `calc` — the D-011 result cache, the calibration ledger, the shapes a calculation is stored in, and the statistical mechanics over what comes back (RRHO, Boltzmann populations). The xTB/CREST engines themselves moved to `Chemclaw3-mcp` in `D-2026-08-16-the-physics-leaves-the-cache-stays`, because a cache and an engine want to live on opposite sides of a wire. No Temporal, no MCP. |
| `kg/` | 4 (Graph) | The graph indexer, the schema and link validators, and `record.py`/`git_writer.py`, the one path that writes a note. |
| `ingest/` | — | Getting records in: `sources` is the generic `DataSource` seam (three optional halves since F4 — ingest, retrieve and **commitments**, the entity-shaped one a portfolio export needs), `commitments` the mirror of a programme's committed work and the join between a slipping milestone and the chemistry slipping it, `eln` the ELN adapters hosted behind it plus the transcription tier they write (`ingest/eln/records.py` — Postgres rows rather than notes, D-2026-08-25), `documents` a mounted file share read as cited evidence (and the one home of this system's document parsers, which `agent/attachments.py` reuses), `labels` the I/O half of reaction labelling — the record-phase builder, the client for the out-of-release labelling server, and the drains that fill `science/labels`. |
| `retrieval/` | — | Reading back out: the retrievers, hybrid search, the vector index, the report harness, and `vectors/` — the seam that lets dense embeddings live outside Postgres (`pgvector` by default, Qdrant behind a late-bound client). |
| `memory/` | — | The memory layers over past campaigns, interactions and failures, plus the ungated observations tier (D-161). |
| `protocols/` | — | The **prescriptive** tier: what to run, as a revisable object. Every other reaction shape here is descriptive — `ingest/eln/ord.py` and `reaction_records` hold what a chemist *did*, and the ORD schema they borrow says a record must describe what was actually done "and not an idealized protocol or instruction set". This is that other half: one envelope for a single experiment and for an HTE plate (a single experiment is a design with one arm and no factors), the deterministic checks a draft has to survive, the plate arithmetic, the revision history, and the diff that is what an expert changed about the first shot. No judgment lives here — that is `skills/protocol-generation` and `skills/hte-campaign-design` — and no chemistry engine either. |
| `templates/` | — | Step templates: the manifest, registry and resolver. |
| `evals/` | — | The eval harness and metrics. |
| `operations/` | — | The operational read model: what this system *did*, read back out of the record it already keeps — tool use by outcome, durable-job runs, what the agent proposed for the graph and how humans decided it, and turn-level spend. Five tables that had writers and no reader; `PostgresAuditSink` still exposes only `record` and `flush`. Counts and identifiers only, never a caller's free text, because one shared corpus means an aggregate is visible to everyone who can reach the agent. Writes nothing. |
| `deliver/` | — | The outbound **delivery** seam: where a message leaves for a *person*. The fourth attachment seam beside `connector.yaml`, `datasource.yaml` and `sink.yaml` — a sink takes a typed record to a database nobody reads, and a channel takes a digest, a report or an escalation to somebody's inbox. Off until `CHEMCLAW_DELIVERY_CHANNELS` names a channel, because a discovered connector serves a tool and a discovered channel sends something out of the building. Every message is redacted once, in the registry, before any driver sees it. Write-only: nothing reads *from* a channel. |
| `publish/` | — | The outbound result seam: every computed value, projected into a typed scientific record and delivered to a database this system does not own. The counterpart of `ingest/` — that brings a corpus in, this sends results out — and the reason it is neither a connector nor a data source is that both refuse it by rule: `connector-validate` bans a mutating tool on an endpoint, and a source "cannot acquire a write path by declaring one". A sink is a folder plus a `sink.yaml`, discovered like the other two seams and **enabled by nobody unless `CHEMCLAW_RESULT_SINKS` names it** (D-2026-08-25). |
| `cli/` | — | Every terminal entrypoint in one place: `chat` (the admin CLI, and the `chemclaw` console script) plus the validator and verifier entrypoints `make` invokes — `eln-validate` is the exception, living with the code it checks. `kg-validate` moved here once half of it needed a database: the graph checks stay pure in `kg/validate.py` and `cli/validate_kg.py` adds the citation-existence half, because `ingest` depends on `kg` and the check may not invert that. |

Layer 3 has no code: it is `SKILL.md` files — the global ones in `skills/`, the bundled ones
inside each connector.

## Everything else

| Directory | What it is |
| --- | --- |
| `knowledge/` | **Layer 4's data**: the graph itself, Markdown notes with frontmatter, one directory per note type. Written directly by `kg/git_writer.py`; `CHEMCLAW_NOTE_REPO_DIR` can point it at a dedicated checkout, which is the tree readers scan. |
| `skills/` | **Layer 3**: the global `SKILL.md` files — judgment not tied to one connector. |
| `data/` | Every corpus the code reads at runtime, each behind a `CHEMCLAW_*` setting: `evals/` (the case-set, retrieval corpus and committed baseline), `templates/` (the shipped step templates), `profiles/` (agent profiles), `vendored/` (build-time datasets with their provenance), `eln-exports/` (sample ELN drops). |
| `tests/` | The suite. Also the gate for several *declarations*: the packaging lists, the Containerfile COPY set, the Helm chart's kinds, the ADR ledger, the layering rules. |
| `infra/` | The local dev stack (`docker-compose.yml`) and the SQL migrations — **this** system's own database. |
| `schema/` | Schemas ChemClaw3 ships for databases it does **not** own, published so a DBA can apply them. Distinct from `infra/sql/` for the reason the two must never merge: nothing here holds DDL privileges on these stores, and the runtime principal is deliberately not the one that can define their tables. Today: `result-store/`, the canonical computed-results schema. |
| `deploy/` | OpenShift delivery: one rootless multi-target image (`Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and the Helm chart. |
| `docs/` | The record and the reference — see `docs/README.md` for which parts are maintained. |
| `examples/` | A runnable walkthrough. Deliberately not shipped in the wheel. |
| `tasks/` | The working files `CLAUDE.md` requires: `todo.md` and `lessons.md`. Plus `live-test/` — the transcripts and per-slice findings of a live probe run (`chemclaw.evals.live`), kept because the archived report cites them per probe and a finding whose reproduction is not on disk is a claim rather than evidence. Run output, never source: nothing imports it, and a later run overwrites it. |
| `.github/workflows/` | CI. The **only** place GitHub Actions reads workflows from — see D-146. |

## Entry points (`CHEMCLAW_COMPONENT`)

One image, four process roles, chosen by `CHEMCLAW_COMPONENT` and dispatched by `deploy/entrypoint.sh`.
The role → module → what-it-serves table lives once, in `deploy/README.md`, rather than forked here —
the two would drift the way the map itself drifts if nobody re-derives it. The one thing worth stating
here, because it is a property of the dispatch itself rather than of any one role: `entrypoint.sh`
matches `connector-worker-*` **before** `connector-*` in its `case`, so a bundle's own Temporal worker
is never swallowed by the more general connector-server pattern that shares its prefix.

## Names that look like duplicates and are not

**`science/calc/` vs `connectors/calc/`** (and `bo`, `fingerprints` likewise). The first
is the engine: pure computation, importable and testable with no orchestration stack. The second is
the wrapper: the durable job definition and the MCP tool surface that expose that engine to the
agent. Keeping them apart is the layering rule; merging them would put Temporal imports inside the
physics. Before D-148 they were `calc/` and `connectors/calc/` — same distinction, no way to see it
from the names.

For `calc` the pairing now spans two repositories, and the line moved rather than blurred
(`D-2026-08-16-the-physics-leaves-the-cache-stays`). `science/calc/` holds the cache, the ledger,
the payload shapes and the arithmetic that is *not* a calculation — RRHO partition functions over a
Hessian, Boltzmann weights over an ensemble. `connectors/calc/` holds the client, the composition
over remote primitives, and the durable jobs. The engines are `Chemclaw3-mcp`'s. What the rule still
forbids is the same thing: a Temporal import inside the arithmetic, and a second copy of a
capability.

The pairing is now the **only** arrangement: capability code lives in a connector bundle or in
`science/`, nowhere else. `chemclaw.mcp` was the last exception, holding the fingerprint code one
directory away from `connectors/molfp` while looking exactly like a duplicate of it; D-156 split it
along this same line and deleted the package.

**`skills/` vs `connectors/*/skills/`.** A bundled skill ships and deploys with its connector; a
global skill belongs to no single capability. Both are discovered by one mechanism —
`CHEMCLAW_SKILLS_DIR` is a `PATH`-style list, and the connector registry appends each enabled
bundle's directory — so the split is about ownership, not lookup.

**`core/quantities.py::Quantity` vs `core/units.py::Measurement`.** Two files in one package about
numbers, and they are not the same subject. `quantities.Quantity` is *a number a payload returned,
under the key the tool gave it* — a label and a float, used to check that a figure stated in an
answer is grounded in one a tool produced. It knows nothing about dimensions and must not: it
reports what a tool said, not what is true, and the moment it started deciding that `sd` means "the
uncertainty of the value above it" it would be asserting a relationship the tool did not state.
`units.Measurement` is a physical quantity — value, unit, uncertainty, basis — with conversions and
a comparison that refuses across dimensions. The first is evidence about a payload; the second is a
fact about the world. Named apart rather than merged for exactly that reason
(`D-2026-08-29-a-quantity-without-a-unit-is-a-number`).

**`core/metrics.py` vs `evals/metric.py` vs `evals/metrics.py`.** Three files, one word, no
relationship. `core/metrics.py` is the Prometheus registry an operator scrapes — turns, tokens,
jobs, refusals — process-wide because that is the scope a scrape targets. `evals/metric.py` is the
`@metric` decorator and registry for scored eval criteria, and `evals/metrics.py` holds the seed
criteria themselves. The collision predates the R2 move that brought the first into `core/`; it is
recorded here rather than resolved by a rename, because both names are right in their own package.

## Why two runtime directories are not in `data/`

`data/` is every corpus the code reads. `knowledge/` and `skills/` are read at runtime too and are
deliberately *not* under it: they are architecture layers 4 and 3 — what the system knows and how it
judges — authored by people rather than configured by an operator. Their position at the root is
what says so. Folding them in would buy an exceptionless sentence at the cost of a real distinction
(D-156).

## Where declarations live

Two defaults resolve against the installed package rather than the working directory, because
what they name ships inside it: `connectors_dir` and `data_sources_dir` (D-148). Both remain
overridable and remain `PATH`-style lists, so pointing a deployment at an *additional* private
bundle directory works as before. `safety_rules_path` was the third until the hazard screen became
`Chemclaw3-mcp`'s, where the table is baked into the server's image rather than configured
(`D-2026-08-15-safety-is-a-tool-not-a-gate`).

## Related repositories

This repository is the backend and orchestration core.

- [`Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui) — the frontend.
- [`Chemclaw3_mock`](https://github.com/8fqycwdt8v-oss/Chemclaw3_mock) — mock MCP tools, data
  sources and HOC, so the system can be tested end to end without real integrations.

## Keeping this file true

Adding a top-level directory, or a subpackage under `src/chemclaw/`, means adding a row here — and
giving that directory a `README.md`, because GitHub renders one the moment a reader clicks the
folder.

Both are **enforced, not requested**. `tests/test_repo_map.py` checks the two tables above against
the directories on disk in both directions, and checks that every directory has a README;
`tests/test_packaging.py` holds the structural half (`src/` is exactly one package, and no import
package reappears beside it). This paragraph asked for the same thing for two restructures and got
it by luck; a map nobody verifies is read, believed, and wrong.

The design record is `docs/decisions/` (one file per ADR, `D-NNN`), indexed by
`docs/decisions/README.md`. `docs/reference/architektur.md` is the original pre-implementation
design and is **historical** — right about the four layers, silent on connectors, which now carry
every tool, job and skill.
