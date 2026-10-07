"""Chemclaw: an AI agent for pharmaceutical and chemical process R&D.

Subpackages follow the four architecture layers in `ARCHITECTURE.md`:

- `core` — the shared kernel; imports no other subpackage.
- `agent`, `api` — layer 1: conversation orchestration (LangGraph) and the HTTP front door.
- `durable` — layer 2: Temporal workflows, activities and worker.
- `connectors` — the capability seam, one bundle per capability; `science` — the engines they wrap.
- `kg`, `ingest`, `retrieval`, `memory` — layer 4: the Markdown knowledge graph and its I/O.

Layer 3 (Agent Skills) is `SKILL.md` files, not code.
"""
