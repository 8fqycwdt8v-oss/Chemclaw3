"""Chemclaw: an AI agent for pharmaceutical and chemical process R&D.

Every first-party module lives under this package, grouped by the four architecture layers
`ARCHITECTURE.md` describes (D-148):

- `core` — the shared kernel every other subpackage imports, and which imports none of them.
- `agent`, `api` — layer 1: conversation orchestration (MAF) and the HTTP front door.
- `durable` — layer 2: the Temporal workflows, activities and worker.
- `connectors` — the capability seam; one bundle per capability, each colocating its manifest,
  its tool server, its durable work and its own skills.
- `science` — the pure-computation engines (`calc`, `bo`, `safety`) the connectors wrap.
- `kg`, `ingest`, `retrieval`, `memory` — layer 4: the Markdown knowledge graph, what feeds it,
  and what reads back out of it.
- `templates`, `evals`, `cli` — the step templates, the eval harness, and the terminal
  entrypoints.

Layer 3 (Agent Skills) is `SKILL.md` files, not code: the global ones in `skills/` at the
repository root, the bundled ones inside each connector.
"""
