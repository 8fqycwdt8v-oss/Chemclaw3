# `chemclaw.exhibits` — artefacts beside the chat

**Responsibility:** the versioned working documents a session shows beside its chat — a plan or
report draft, a table, a set of structures, a chart, one 3D geometry, a pinned tool result, a link
to a protocol, note or job. The agent writes them with three tools (`agent/exhibit_tools.py`); a chemist reads,
edits, pins and exports them over `api/routes/exhibits.py`. Named `exhibit` because `artifact` is
the calculation store's word here (D-124); every surface a chemist reads says "Artefacts".

The decision record is `D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`; the wire
contract `Chemclaw3_ui` builds against is the shapes in `models.py` and the routes.

## The one thing to internalize

**An artefact is part of the answer, not an effect.** It changes nothing outside the conversation,
as the answer text does not — so its tools are read-only for authorization and need no approved
plan. It is still kept off every helper's surface (`agent/subagents.SPEAKS_TO_THE_CHEMIST`), because
a helper writing one would put something on the chemist's screen from a context the chemist cannot
see. Promoting an artefact to a knowledge note or a protocol is the gated step, and it goes through
the tools that already do those writes.

## Modules

| Module | What it owns |
| --- | --- |
| `models.py` | The seven spec kinds (`document`, `table`, `structures`, `chart`, `result`, `link`, `geometry`) as a discriminated union with `extra="forbid"`; `parse_spec` (shape — including a geometry's XYZ layout — on every read and write) and `require_writable` (the caps and the SMILES parse, writes only); the API shapes `ExhibitHeader`, `ExhibitView`, `ExhibitRevision`, `ExhibitDiff`; the four errors a write can meet. |
| `store.py` | `ExhibitStore` with an in-memory and a Postgres backend (`115_session_exhibits.sql`). Append-only revisions under a header row lock; a stale base is `StaleRevision`; every read is session-scoped, so another session's id answers as an unknown one. |
| `diff.py` | A revision diff in `protocols.diff.FieldChange`'s shape, so one UI component renders both: line hunks for a document, cells for a table, item fields for structures, points for a chart, whole fields for a geometry. |
| `export.py` | `md`, `csv`, `smi` and `xyz` files; every CSV cell goes through `protocols.export.csv_cell`, the one formula-injection guard. `resolve_export` reads a cited geometry's bytes; `safe_filename` is the one `Content-Disposition` sanitiser. |
| `sources.py` | What a geometry's `source` names in the calculation artifact store (D-124): refused on write unless stored, read for its export and for `GET /calc-artifacts/content` (`D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`). |
| `grounding.py` | The figures an agent-written revision states that no stored tool result of the session accounts for — **unchecked, not wrong** — stored on the revision at write time. |

## What is deliberately not here

- **Bindings into the tool-result store** (a cell that *is* a tool value rather than a copy of
  one). Deferred by the ADR on a measurement: tables are 2.7% of what answers spend, so the token
  argument for them did not hold, and the cost is a handle in every tool result's shape.
- **Artefacts that run code** — model-authored HTML or JavaScript. Declined in the ADR, with its
  `Revisit when:`.
- **Rendering.** Structures are drawn by the UI's RDKit worker, charts by its own SVG and a geometry
  by its 3D viewer; no server path produces an image for an artefact.

## Lifecycle

Session-owned and conversation-tier: deleting the session deletes its artefacts, an erasure removes
those of the leaver's sessions (`agent/leaver.py`), and `retention_session_exhibits_days` ages them
out (`durable/retention.py`). The revisions table is INSERT-only by grant and goes only behind its
header, by cascade.
