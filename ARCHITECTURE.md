# Architecture and repository map

Start here if you are trying to find something. `README.md` is how to run it; this is what the
directories are and why they are separate.

## The four layers

Four layers, each with one responsibility. The rule that matters is that their concerns never
merge — `CLAUDE.md` states it, and `tests/test_layering.py` enforces the part a machine can check.

1. **MAF** (Microsoft Agent Framework) — conversation orchestration and short reasoning steps.
2. **Temporal** — durable execution of long or expensive jobs. Durability lives here and *only*
   here, never in MAF. Two task queues: `hpc-jobs` (few, heavy workers) and `background-jobs`
   (light: sync, re-index, reports). Every result is persisted once and never recomputed.
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data and relations).

Skills hold judgment; connectors hold capability (deterministic tools). Anything the agent
generates enters the graph through a **PR-gate**, so a human signs off before it becomes knowledge.

## Where the code is

| Directory | Layer | What it is |
| --- | --- | --- |
| `chemclaw/` | — | The shared kernel every other package imports: config, database, HTTP, ids, logging, errors, embeddings, the Temporal client. It imports nothing first-party. |
| `agents/` | 1 (MAF) | Conversation orchestration: the agent, its tool surface, sessions, identity, authorization, the plan/execute harness. |
| `service/` | 1 (MAF) | The FastAPI + SSE front door that serves `agents/` over HTTP, behind OIDC. |
| `workflows/` | 2 (Temporal) | Workflow and activity definitions — the durable half of every long job. |
| `workers/` | 2 (Temporal) | The `background-jobs` worker process. |
| `connectors/` | 2 + 3 | The capability seam. One bundle per capability (`chem`, `calc`, `bo`, `qm`, `safety`, `molfp`, `rxnfp`), each colocating its `connector.yaml` manifest, its MCP tool server, its Temporal worker, and its own `skills/`. Adding a capability is adding a directory here. |
| `skills/` | 3 (Skills) | The global `SKILL.md` files — judgment that is not tied to one connector. |
| `kg/` | 4 (Graph) | The graph indexer, the schema and link validators, the PR-gate that writes notes. |
| `knowledge/` | 4 (Graph) | The graph itself: Markdown notes with frontmatter, one directory per note type. Data, not code. |
| `calc/` | — | The physics engine: xTB/GFN2, conformers, pKa, solubility, thermochemistry, and the Postgres calculation cache. Pure computation. |
| `bo/` | — | The BoFire Bayesian-optimization engine and its benchmarks. Pure computation. |
| `safety/` | — | Hazard rules and screening. Pure computation. |
| `eln/` | — | ELN ingestion: the adapters, the sync cursor, export validation. |
| `sources/` | — | The generic `DataSource` seam. A new source is one `sources/<name>/datasource.yaml` plus its name in `CHEMCLAW_DATA_SOURCES` — no core edits. |
| `report/` | — | Retrieval and synthesis: the retrievers, hybrid search, the vector index, the report harness. |
| `memory/` | — | The memory layers over past campaigns, interactions and failures. |
| `mcp_servers/` | — | The two standalone MCP servers (`molfp`, `rxnfp`) that are not connector bundles. |
| `templates/` | — | Step templates: the manifest, registry and resolver, plus the shipped `*.yaml`. |
| `evals/` | — | The eval harness and metrics, plus the versioned case-set and retrieval corpus. |
| `scripts/` | — | Every validator `make` runs, plus the dev entrypoints. |
| `examples/` | — | A runnable walkthrough. Deliberately not shipped in the wheel. |

## Everything else

| Directory | What it is |
| --- | --- |
| `tests/` | The suite. Also the gate for several *declarations*: packaging lists, the Containerfile COPY set, the Helm chart's kinds, the ADR ledger. |
| `infra/` | The local dev stack (`docker-compose.yml`) and the SQL migrations. |
| `deploy/` | OpenShift delivery: one rootless multi-target image (`Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and the Helm chart. |
| `docs/` | Design documents, the runbook, plans, and point-in-time reviews. |
| `data/` | Vendored datasets shipped into the image. |
| `profiles/` | Agent profiles (YAML) selecting across skills and tools. |
| `tasks/` | The working files `CLAUDE.md` requires: `todo.md` and `lessons.md`. |
| `.github/workflows/` | CI. The **only** place GitHub Actions reads workflows from — see D-139. |

## Two pairs of names that look like duplicates and are not

**`calc/` vs `connectors/calc/`** (and `bo/`, `safety/` likewise). The first is the engine: pure
computation, no Temporal and no MCP imports. The second is the wrapper: the durable job definition
and the tool surface that expose that engine to the agent. They are separate so the engine stays
importable and testable without an orchestration stack, which is the layering rule above. Merging
them would put Temporal imports inside the physics.

**`skills/` vs `connectors/*/skills/`.** A bundled skill ships and deploys with its connector; a
global skill does not belong to one capability. Both are discovered by one mechanism —
`CHEMCLAW_SKILLS_DIR` is a `PATH`-style list — so the split is about ownership, not lookup.

## Related repositories

This repository is the backend and orchestration core.

- [`Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui) — the frontend.
- [`Chemclaw3_mock`](https://github.com/8fqycwdt8v-oss/Chemclaw3_mock) — mock MCP tools, data
  sources and HOC, so the system can be tested end to end without real integrations.

## Keeping this file true

Adding a top-level directory means adding a row here. The design record lives in `DECISIONS.md`
(`D-NNN`, append-only) with `ADR-REGISTRY.md` as its index; `docs/architektur.md` is the original
pre-implementation design and is **historical** — right about the four layers, silent on connectors,
which now carry every tool, job and skill.
