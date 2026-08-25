# Reaction labelling, a precedent index, and Pistachio as an evidence corpus

Plan: `/root/.claude/plans/investigate-deeply-following-user-playful-spindle.md`

## Phase 1 — the vocabulary and the two-phase index
- [x] `science/labels/vocabulary.py` — `SpeciesRole`, `LabelGroup`, `species_role_from`, `VOCABULARY_VERSION`
- [x] `science/labels/records.py` — `ReactionLabel`, `SpeciesLabel`
- [x] `science/labels/policy.py` — `LabelPolicy`
- [x] `science/labels/store.py` — `LabelIndex` Protocol, in-memory + Postgres backends
- [x] `infra/sql/050_reaction_labels.sql` + README rows + grants
- [x] `DataSourceManifest.labels`
- [x] `ingest_reaction` writes the record phase; `sync_entries` / `eln_sync` thread `source`
- [x] tests

## Phase 2 — the `rxnlabel` server and its client
- [ ] `Chemclaw3-mcp:servers/rxnlabel/` (separate PR)
- [ ] `ingest/labels/labeller.py` (MCP client), `core/config/labels.py`

## Phase 3 — the enrichment background service
- [ ] `ingest/labels/{enrich,merge}.py`, `durable/label_sync.py`, schedule

## Phase 4 — Pistachio as an evidence corpus (warehouse `corpus:` binding)
## Phase 5 — the six questions as tools
## Phase 6 — pattern-fingerprint substructure screen (optional)

## Review
(filled at the end)
