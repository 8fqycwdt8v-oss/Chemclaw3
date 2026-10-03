# D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy — how a 3D structure becomes an artefact

**Status:** accepted · **Date:** 2026-10-03 · Wave 2 of
`D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect`, against the frozen wire contract
the frontend builds to (`geometry` kind, `GET /calc-artifacts/content`).

## Context

The calculations this system runs produce structures — an optimised geometry, a conformer — and a
chemist wants to look at one beside the answer, rotate it, and keep the file. Two stores already
hold such bytes: the calculation artifact store (D-124, `science/calc/artifacts.py`), content
addressed and **eviction-managed** (`durable/artifact_eviction.py`), and the artefact revisions
(`115_session_exhibits.sql`), append-only and session-owned. The contract fixed the spec's shape —
an inline `xyz` block **or** a `source` naming a calc artifact as `{calc_key, name}` — and a download
route for the cited bytes. Three things it left to this side are decided here.

## Decision

1. **A `source` is a citation, checked when it is written and not pinned.** A write naming an
   artifact the store does not hold is refused (`exhibits/sources.require_source_stored`), on the
   agent's tools and on the REST writes alike, so the model cannot cite a by-product it guessed. A
   stored revision is never re-checked: if eviction later takes the blob, the artefact still opens
   and its `xyz` export answers 404 — a missing file, not a broken artefact.
2. **`GET /calc-artifacts/content` is any authenticated caller's**, as `GET /notes/{id}` and
   `GET /jobs` are. The calculation cache is shared — one Hessian serves every session that asks
   about that molecule — and an artifact has no owner to scope to; the artefact citing it is the
   session-scoped object. The route refuses above `calc_artifact_max_download_bytes` with 413 from
   the recorded size, before the read decompresses the blob into memory.
3. **Migration `116_exhibit_geometry_kind.sql` widens the kind `CHECK` by dropping and re-adding
   it.** A `CHECK` has no other spelling for a widening; the drop is `IF EXISTS`, so a replay re-adds
   the same widened form. The previous image's *writes* all still pass — it knows six kinds and the
   constraint admits seven — so "deploy the previous image" remains the rollback for writes. What it
   cannot do is read a `geometry` revision: its `parse_spec` refuses the kind, so the listing shows
   the artefact and opening or exporting it fails. That is the runbook's rollback-table row for 116,
   and `tests/test_migrations_are_additive.py::_REVIEWED_ROLLBACK_BREAKS` holds the exemption to
   exactly the one `DROP CONSTRAINT` statement.

The XYZ checks (count line, known element, finite coordinates, one frame) are shape and run on every
parse; the atom cap `exhibit_max_atoms` is a write-only cap like the other counts, so lowering it
never makes a stored artefact unreadable. A geometry's grounding figure is its `energy_hartree`;
its coordinates are a structure a viewer reads, not figures anybody quotes, and are not scanned.

## Options considered

- **Copy the bytes into the spec at write time** (resolve `source` to `xyz` and store that). The
  artefact would survive eviction — but a revision would be a transcription rather than the
  calculation's own output, the spec would carry a structure twice the moment it was cited, and an
  ensemble or a large complex would spend the 200 kB spec cap on coordinates. Declined.
- **Pin a cited blob against eviction** (a reference count `artifact_eviction` honours). That
  couples a cost policy to conversation state the eviction sweep does not read today, and makes a
  session that cites a Hessian keep it for the session's lifetime. Declined.
  Revisit when: a chemist reports a geometry artefact whose `xyz` export has gone 404 under a
  deployment that turned eviction on (`artifact_store_max_bytes` or `artifact_evict_idle_days`
  non-zero) — that is the case this decision accepts, and the first report of it is the measurement.
- **Scope the download through an artefact** (`/sessions/{id}/exhibits/{xid}/source`). It would
  gate shared bytes behind one conversation and still not stop anyone holding a session from
  reading them, while `fetch_artifact` already lets any turn read the same store. Declined.

## What keeps it true

- `tests/test_exhibit_geometry.py` — the XYZ shape, the write-only atom cap, the stored-source
  refusal on both writers, the export and its evicted arm, the whole-field diff, the download's
  type, filename, 404s and its 413 before the read, and the Postgres arm writing the widened kind.
- `tests/test_migrations_are_additive.py` — 116's one reviewed statement, and the runbook row.
