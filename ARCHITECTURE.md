# Architecture and repository map

Start here to find something. `README.md` is how to run it; this is what each directory is. Every
directory has its own `README.md` with the detail.

**`src/` is all the code. Everything beside it is data, configuration or documents.**

## The four layers

1. **LangGraph** — conversation orchestration (`agent/`, `api/`).
2. **Temporal** — durable execution of long or expensive jobs (`durable/`, the connector workers);
   durability lives only here, and a persisted result is never recomputed.
3. **Agent Skills** — `SKILL.md` judgment in `skills/` and each bundle's `skills/`; no code.
4. **Knowledge graph** — what we know (`kg/`, `knowledge/`).

Their concerns never merge; `tests/test_layering.py` enforces the import direction.
`docs/decisions/CURRENT.md` lists the decisions in force.

## The code: `src/chemclaw/`

| Subpackage | Layer | What it is |
| --- | --- | --- |
| `core/` | — | The shared kernel every layer imports: config, database, HTTP, ids, logging, metrics, the egress guard (including the `LD_PRELOAD` half in `core/netguard_preload.c`) and the ambient-turn primitives. It imports no sibling at module scope and has exactly one declared lazy exception, to `connectors.registry`. |
| `agent/` | 1 | Conversation orchestration: the compiled graph (`agent/langgraph_agent.py`), its tool surface and middleware chain, the checkpointer, sessions, authorization, the plan gate and the turn's filesystem. |
| `api/` | 1 | The FastAPI + SSE front door behind OIDC; `create_app` in `api/app.py` is the composition root and `api/routes/` holds one module per resource. |
| `durable/` | 2 | Temporal workflows, activities and the `background-jobs` worker. |
| `connectors/` | 2 + 3 | The capability seam: one bundle per `connector.yaml`, with its worker, its skills and, where this repo runs it, its MCP server. The manifest of a `Chemclaw3-mcp` server this repo does not run is not here: it arrives with the installed `chemclaw-contracts` package, and the directory beside it holds only the `skills/` for it. |
| `science/` | — | Pure computation: `bo` (BoFire), `fingerprints`, `labels`, and `calc` (the result cache, calibration ledger and RRHO/Boltzmann arithmetic; the engines live in `Chemclaw3-mcp`). No Temporal, no MCP. |
| `kg/` | 4 | The graph indexer, schema and link validators, and `kg/record.py`, the one path that writes a note. |
| `ingest/` | — | Getting records in: the `DataSource` seam (`sources`), ELN adapters and transcriptions, commitments, mounted documents and the labelling drains. |
| `retrieval/` | — | Reading back out: retrievers, hybrid search, the vector index and the report harness. |
| `memory/` | — | Memory layers over past campaigns, interactions and failures, plus the ungated observations tier. |
| `hypotheses/` | — | Ranking competing explanations: rating fit, Swiss pairing, screen rules and rendering, behind `durable/hypothesis_tournament.py`. Domain-free, so not under `science/`. |
| `protocols/` | — | The prescriptive tier for what to run: one envelope for an experiment or an HTE plate, its checks, revisions and diffs. No judgment and no chemistry engine. |
| `analytical/` | — | The prescriptive tier for results: acceptance criteria and the deterministic verdict measurements earn against them, including stability trending. |
| `exhibits/` | — | Artefacts: the versioned working documents a session shows beside its chat, written by the agent and edited over REST. Session-owned, so they are erased with the conversation. |
| `templates/` | — | Step templates: the manifest, registry and resolver. |
| `evals/` | — | The eval harness and metrics. |
| `operations/` | — | The read-only operational model over what the system did: tool use, job runs, knowledge writes and spend. Counts and identifiers only. |
| `deliver/` | — | The outbound delivery seam to a person (digests, reports, escalations), off until `CHEMCLAW_DELIVERY_CHANNELS` names a channel. Every message is redacted in the registry. |
| `publish/` | — | The outbound result seam: computed values projected into typed records and delivered to a database this system does not own, off until `CHEMCLAW_RESULT_SINKS` names a sink. |
| `cli/` | — | Every terminal entrypoint: `chat` and the validators and verifiers `make` invokes. |

## Everything else

| Directory | What it is |
| --- | --- |
| `knowledge/` | Layer 4's data: Markdown notes with frontmatter, one directory per note type. |
| `skills/` | Layer 3: the global `SKILL.md` files not tied to one connector. |
| `data/` | Every corpus the code reads at runtime, each behind a `CHEMCLAW_*` setting: `evals/`, `templates/`, `profiles/`, `vendored/`, `eln-exports/`, `commitments/`. `data/README.md` names each setting. |
| `tests/` | The suite, including the guards on declarations: packaging, the image, the Helm chart, the ADR ledger and the layering rules. |
| `infra/` | The local dev stack (`docker-compose.yml`) and the SQL migrations for this system's own database. |
| `schema/` | Contracts this system publishes rather than runs. `result-store/`: schemas for databases it does **not** own, kept apart from `infra/sql/` because nothing here holds DDL on those stores. `api/`: the generated OpenAPI document the UI builds its types from, with its version rules. |
| `deploy/` | OpenShift delivery: one rootless multi-role image, the Helm chart, and `kind/` for a local cluster. `deploy/README.md` maps each `CHEMCLAW_COMPONENT` role to its module. |
| `docs/` | Decisions, guides, reference and archive — `docs/README.md` says which are maintained. |
| `examples/` | A runnable walkthrough, not shipped in the wheel. |
| `tasks/` | Working files: `todo.md` (the current plan) and `lessons.md`. |
| `.github/workflows/` | CI, the only place GitHub Actions reads workflows from. |

## Names that look like duplicates and are not

- **`science/calc/` vs `connectors/calc/`** (likewise `bo`, `fingerprints`): the first is pure
  computation with no orchestration import; the second is the durable jobs and tool surface that
  expose it. For `calc` the engines themselves are `Chemclaw3-mcp`'s.
- **`skills/` vs `connectors/*/skills/`**: a bundled skill ships with its connector, a global one
  belongs to none; one discovery mechanism (`CHEMCLAW_SKILLS_DIR`) finds both.
- **`core/quantities.py` vs `core/units.py`**: a `Quantity` is a number a tool returned under its
  key; a `Measurement` is a physical quantity with unit, uncertainty and conversions.
- **`core/metrics.py` vs `evals/metric.py` vs `evals/metrics.py`**: the Prometheus registry, the
  eval-criterion decorator, and the seed criteria.

## Keeping this file true

- A row here for every top-level directory and every direct subpackage of `src/chemclaw/`.
- A `README.md` in every top-level directory and every directory under `src/chemclaw/` that holds
  Python modules.
- One corpus lives inside `src/`: `science/bo/benchmarks/data/`, package data pinned to the
  benchmark that reads it.

`tests/test_repo_map.py` enforces all three in both directions.
