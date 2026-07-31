# Architecture and repository map

Start here if you are trying to find something. `README.md` is how to run it; this is what the
directories are and why they are separate.

**The one rule that makes the tree navigable: `src/` is all the code. Everything beside it is
data, configuration or documents.**

## The four layers

Four layers, each with one responsibility. The rule that matters is that their concerns never
merge — `CLAUDE.md` states it, and `tests/test_layering.py` enforces the parts a machine can check.

1. **MAF** (Microsoft Agent Framework) — conversation orchestration and short reasoning steps.
2. **Temporal** — durable execution of long or expensive jobs. Durability lives here and *only*
   here, never in MAF. Two kinds of task queue: `background-jobs` (light: sync, re-index, reports)
   and one per connector bundle that owns durable work, each sized for that work. Every result is
   persisted once and never recomputed.
3. **Agent Skills** (`SKILL.md`) — "how do I do X" (judgment), loaded on demand.
4. **Markdown knowledge graph in Git** (NetworkX indexer) — "what do we know" (data and relations).

Skills hold judgment; connectors hold capability (deterministic tools). Anything the agent
generates enters the graph through a **PR-gate**, so a human signs off before it becomes knowledge.

## The code: `src/chemclaw/`

| Subpackage | Layer | What it is |
| --- | --- | --- |
| `core/` | — | The shared kernel: config, database, HTTP, ids, logging, errors, embeddings, the Temporal client. Everything imports it; it imports no sibling, and `test_layering.py` proves that. |
| `agent/` | 1 (MAF) | Conversation orchestration: the agent, its tool surface, sessions, identity, authorization, the plan/execute harness. |
| `api/` | 1 (MAF) | The FastAPI + SSE front door that serves `agent/` over HTTP, behind OIDC. |
| `durable/` | 2 (Temporal) | Workflows, activities, and the `background-jobs` worker that hosts them. |
| `connectors/` | 2 + 3 | The capability seam. One bundle per capability (`chem`, `calc`, `bo`, `qm`, `safety`, `molfp`, `rxnfp`), each colocating its `connector.yaml` manifest, its MCP tool server, its Temporal worker, and its own `skills/`. Adding a capability is adding a directory here — no core edit. |
| `science/` | — | The pure-computation engines: `calc` (xTB/GFN2, conformers, pKa, solubility, thermochemistry), `bo` (BoFire), `safety` (hazard screening), `fingerprints` (ECFP4/DRFP + Tanimoto). No Temporal, no MCP. |
| `kg/` | 4 (Graph) | The graph indexer, the schema and link validators, the PR-gate that writes notes. |
| `ingest/` | — | Getting records in: `sources` is the generic `DataSource` seam, `eln` the ELN adapters hosted behind it. |
| `retrieval/` | — | Reading back out: the retrievers, hybrid search, the vector index, the report harness. |
| `memory/` | — | The memory layers over past campaigns, interactions and failures. |
| `templates/` | — | Step templates: the manifest, registry and resolver. |
| `evals/` | — | The eval harness and metrics. |
| `cli/` | — | Every terminal entrypoint in one place: `chat` (the admin CLI, and the `chemclaw` console script) and the eight validators `make` runs. |

Layer 3 has no code: it is `SKILL.md` files — the global ones in `skills/`, the bundled ones
inside each connector.

## Everything else

| Directory | What it is |
| --- | --- |
| `knowledge/` | **Layer 4's data**: the graph itself, Markdown notes with frontmatter, one directory per note type. PR-gated; `CHEMCLAW_NOTE_REPO_DIR` can point it at a dedicated checkout. |
| `skills/` | **Layer 3**: the global `SKILL.md` files — judgment not tied to one connector. |
| `data/` | Every corpus the code reads at runtime, each behind a `CHEMCLAW_*` setting: `evals/` (the case-set, retrieval corpus and committed baseline), `templates/` (the shipped step templates), `profiles/` (agent profiles), `vendored/` (build-time datasets with their provenance), `eln-exports/` (sample ELN drops). |
| `tests/` | The suite. Also the gate for several *declarations*: the packaging lists, the Containerfile COPY set, the Helm chart's kinds, the ADR ledger, the layering rules. |
| `infra/` | The local dev stack (`docker-compose.yml`) and the SQL migrations. |
| `deploy/` | OpenShift delivery: one rootless multi-target image (`Containerfile`, role chosen by `CHEMCLAW_COMPONENT`) and the Helm chart. |
| `docs/` | The record and the reference — see `docs/README.md` for which parts are maintained. |
| `examples/` | A runnable walkthrough. Deliberately not shipped in the wheel. |
| `tasks/` | The working files `CLAUDE.md` requires: `todo.md` and `lessons.md`. |
| `.github/workflows/` | CI. The **only** place GitHub Actions reads workflows from — see D-146. |

## Two pairs of names that look like duplicates and are not

**`science/calc/` vs `connectors/calc/`** (and `bo`, `safety`, `fingerprints` likewise). The first
is the engine: pure computation, importable and testable with no orchestration stack. The second is
the wrapper: the durable job definition and the MCP tool surface that expose that engine to the
agent. Keeping them apart is the layering rule; merging them would put Temporal imports inside the
physics. Before D-148 they were `calc/` and `connectors/calc/` — same distinction, no way to see it
from the names.

The pairing is now the **only** arrangement: capability code lives in a connector bundle or in
`science/`, nowhere else. `chemclaw.mcp` was the last exception, holding the fingerprint code one
directory away from `connectors/molfp` while looking exactly like a duplicate of it; D-155 split it
along this same line and deleted the package.

**`skills/` vs `connectors/*/skills/`.** A bundled skill ships and deploys with its connector; a
global skill belongs to no single capability. Both are discovered by one mechanism —
`CHEMCLAW_SKILLS_DIR` is a `PATH`-style list, and the connector registry appends each enabled
bundle's directory — so the split is about ownership, not lookup.

## Why two runtime directories are not in `data/`

`data/` is every corpus the code reads. `knowledge/` and `skills/` are read at runtime too and are
deliberately *not* under it: they are architecture layers 4 and 3 — what the system knows and how it
judges — authored by people rather than configured by an operator. Their position at the root is
what says so. Folding them in would buy an exceptionless sentence at the cost of a real distinction
(D-155).

## Where declarations live

Three defaults resolve against the installed package rather than the working directory, because
what they name ships inside it: `connectors_dir`, `data_sources_dir` and `safety_rules_path`
(D-148). Each remains overridable, and the two directory ones remain `PATH`-style lists, so
pointing a deployment at an *additional* private bundle directory works as before.

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
