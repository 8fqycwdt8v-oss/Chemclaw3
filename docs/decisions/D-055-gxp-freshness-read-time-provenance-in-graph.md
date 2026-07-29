# D-055 — GxP freshness + read-time provenance in graph retrieval (audit KM-6, KM-7)

**Context.** The knowledge-management gap analysis (`docs/audit/09-knowledge-management-gaps.md`)
found two read-path gaps that are cheap, offline, and central to the GxP posture — no infra, no
schema migration, no curated artifact, no chosen threshold:

- **KM-7 (freshness).** `Note.valid_from`/`valid_to` existed but were **never checked at read**, so a
  not-yet-valid or expired note served as current fact with no signal — sharp for a GxP base that
  must not present superseded conditions as current.
- **KM-6 (provenance at read).** `NoteRef` (the agent-facing view from `find_notes`/`expand_note`)
  exposed only `id/type/smiles/tags`, so the agent could not weigh a source by author/origin/
  confidence/validity without a second lookup, even though the note carried all of it.

**Decision.**
- `Note.is_current(as_of)` encodes the validity window (inclusive bounds; either bound optional).
  The three discovery/evidence sweeps — `find_notes`, `expand_note`'s neighbor list, and
  `GraphRetriever.retrieve` (the report path) — now exclude non-current notes as of `date.today()`.
  **Explicit by-id expansion still returns the anchor** even if expired (an explicit lookup, not a
  discovery sweep); only discovered/neighbor/report evidence is freshness-filtered. Nothing is
  deleted — the note stays in Git and reachable by id, it is only dropped from *current-evidence*
  results.
- `NoteRef` carries `created_by`, `source`, `confidence`, `valid_from`, `valid_to` (all defaulted so
  a bare reference is still constructible); `_ref` fills them from the note. This also wires the
  previously-unread `confidence` field into the agent's view (part of KM-5's concern) without
  building a cross-source ranker.

**Consequence (behavior change, flagged).** Retrieval results change: an expired or not-yet-valid
note no longer appears in `find_notes`, in `expand_note` neighbors, or in report evidence. This is
intended GxP behavior (don't serve superseded facts as current). The chosen policy is *exclude
silently from current-evidence sweeps* (the note is still in Git and by-id reachable) rather than
*include-with-a-flag*; if a surfaced-but-flagged behavior is later wanted, `is_current` is the single
seam to branch on.

**Result.** `make lint type test` green; `mypy --strict` clean. Tests: `test_note.py` (window
semantics incl. inclusive boundaries), `test_graph_tools.py` (provenance surfaced; expired excluded
from `find_notes` and from `expand_note` neighbors while the anchor is kept), `test_report.py`
(`GraphRetriever` skips expired). The remaining gap-doc items are either deferred-by-design/
infra-gated or carry a design decision (a gold-set, a ranking function, a concurrency limit, an audit
schema migration) and are left for an explicit follow-up rather than guessed.
