# `chemclaw.exhibits` — artefacts beside the chat

**Responsibility:** the versioned working documents a session shows beside its chat — a plan or
report draft, a table, a set of structures, a chart, one 3D geometry, an html page, a pinned tool
result, a link to a protocol, note or job. The agent writes them with three tools (`agent/exhibit_tools.py`); a chemist reads,
edits, pins and exports them over `api/routes/exhibits.py`. Named `exhibit` because `artifact` is
the calculation store's word here (D-124); every surface a chemist reads says "Artefacts".

The decision record is `D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, with wave 3's
`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from` and
`D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves`; the wire
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
| `models.py` | The eight spec kinds (`document`, `table`, `structures`, `chart`, `result`, `link`, `geometry`, `html`) as a discriminated union with `extra="forbid"`, and the `$bind`/`rows_from` shapes where a value may be bound; `parse_spec` (shape — including a geometry's XYZ layout — on every read and write), `require_creatable` (the html switch) and `require_writable` (the caps, the SMILES parse and no literal `null` where only a vanished binding may leave one, writes only); the API shapes `ExhibitHeader`, `ExhibitView` (resolved `spec`, stored `raw_spec`, `bindings`), `ExhibitRevision`, `ExhibitDiff`; the four errors a write can meet. |
| `store.py` | `ExhibitStore` with an in-memory and a Postgres backend (`115_session_exhibits.sql`). Append-only revisions under a header row lock; a stale base is `StaleRevision`; every read is session-scoped, so another session's id answers as an unknown one. A caller-chosen id makes `create` a create-or-return, which is how a durable writer (a development report's activity) stays idempotent across retries. |
| `bindings.py` | Resolves a spec's bindings against the session's stored tool results (`tool_result_links`, the session is the scope): RFC 6901 pointers, a handle's prefix to exactly one ref, type and cap checks. A write is refused unless every binding resolves and is stored with the full ref; a read fills the values in and reads a swept result as `null`, `ok: false`. |
| `diff.py` | A revision diff in `protocols.diff.FieldChange`'s shape, so one UI component renders both: line hunks for a document, cells for a table, item fields for structures, points for a chart, whole fields for a geometry. Over stored specs, so a binding is the value compared. |
| `export.py` | `md`, `csv`, `smi`, `xyz` and `html` files (the last as `text/plain`, never `text/html`), from the resolved spec; every CSV cell goes through `protocols.export.csv_cell`, the one formula-injection guard. `resolve_export` reads a cited geometry's bytes; `safe_filename` is the one `Content-Disposition` sanitiser. |
| `sources.py` | What a geometry cites: a `structure_id` in the structure store (what the agent holds, resolved to `xyz` when read — a vanished one reads as an `ok: false` entry in `bindings`) or a `source` in the calculation artifact store (D-124). Either is refused on write unless stored and one frame within the atom cap; an artifact is read only within `calc_artifact_max_download_bytes`, decided from its recorded size, for an export and for `GET /calc-artifacts/content` (`D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy`). |
| `grounding.py` | The figures an agent-written revision states that no stored tool result of the session accounts for — **unchecked, not wrong** — stored on the revision at write time. A bound value states none; an html page is read for its text (`html_text`). The artefact tools' own results are never evidence (`models.EXHIBIT_TOOLS`), and the figures a person introduced are recorded on their revision (`introduced_figures`, migration 119) and read back as a set. |
| `telemetry.py` | The `exhibit.created`/`exhibit.revised` event and `chemclaw_exhibit_writes_total{author_kind,op}` for every write, and `chemclaw_exhibit_refusals_total{reason}` for every refused one — one place for the REST route, the agent's tools and the report activity. |

## What is deliberately not here

- **Serving an html page as HTML.** A page is stored and exported as text; it runs only in
  `Chemclaw3_ui`'s separate sandbox origin, in an opaque-origin frame with no network.
- **A tool that fetches a result back by its handle** — declined in the bindings ADR, with its
  `Revisit when:`.
- **Rendering.** Structures are drawn by the UI's RDKit worker, charts by its own SVG and a geometry
  by its 3D viewer; no server path produces an image for an artefact.

## Lifecycle

Session-owned and conversation-tier: deleting the session deletes its artefacts, an erasure removes
those of the leaver's sessions (`agent/leaver.py`), and `retention_session_exhibits_days` ages them
out (`durable/retention.py`). The revisions table is INSERT-only by grant and goes only behind its
header, by cascade.

## A development report as an artefact, and its one limitation

A report requested from a conversation is also written there as a `document` artefact by the
report's own activity (`durable/report_workflow.record_report_exhibit`), with an id derived from
the workflow so a retry writes nothing twice. **The run is shared, the artefact is not**: the
report's job id leaves the session out on purpose (`agent/durable_tools._report_id`), so a second
session asking for the same report rejoins the first run and gets the note but **no artefact of
its own**, and a status read there omits `exhibit_id` rather than name an artefact it cannot open
(`agent/durable_tools.readable_in_this_session`). Copying the artefact into every session that
rejoins would need the run to learn who rejoined it, which nothing records today.
