# D-050 — F7: the generic data-source seam (compose two half-contracts, don't merge them)

**Context.** The system had two disjoint half-contracts — `ElnAdapter` (ingest: fetch + map to the
canonical ORD reaction) and `SourceRetriever` (retrieve: evidence for a query) — with different
methods and DTOs, and two selection styles (a config-string dict factory for ELN, a hardcoded
`[GraphRetriever()]` list for retrieval). Attaching a new source (first live one: a custom Snowflake
ELN connector) touched both places.

**Decision.**
- **One seam by composition, not merger** (`sources/base.py`). A `DataSource` names itself and
  exposes an optional `ingest` half and an optional `retrieve` half, each being the *existing*
  protocol verbatim (`IngestHalf = ElnAdapter`, `RetrieveHalf = SourceRetriever`). No new DTOs —
  `RawEntry`/`OrdReaction`/`EvidenceChunk` are reused. `SourceSpec` (frozen) is the concrete impl and
  rejects a source that provides neither half. The protocol members are read-only properties so a
  frozen impl satisfies it.
- **Config-driven registry** (`sources/registry.py`, `data_sources` config). `graph` is
  retrieve-only (the knowledge graph); `eln-json`/`eln-ord` are ingest-only (the ELN adapters
  re-hosted verbatim — the ELN is not *also* the graph retriever, so no double count).
  `active_retrieve_sources()` / `active_ingest_sources()` select by config.
- **Both consumers re-hosted with no behavior change** (F7-T3). `gather_evidence`'s
  `_text_retrievers()` now returns `active_retrieve_sources()` — the default yields exactly the one
  `GraphRetriever` as before. `eln_sync.sync_eln_entries` now ingests `active_ingest_sources()` and
  merges per-source summaries — the single default source folds to the previous single-adapter
  behavior. All existing ELN/research tests pass unchanged (the acceptance bar).
- **Provenance already flows** (F7-T4): the mapped `OrdReaction` carries `provenance` + `reaction_id`
  (native ref), and knowledge still enters via the terminal PR-gate while serving indices stay
  ungated (D-018) — source-agnostically, because the seam changed only the *selection*, not the flow.

**Deferred behind the seam (unchanged from the plan):** the live custom Snowflake ELN connector
(durable `background-jobs` sync with a per-source *pipeline cursor* over Snowflake's load-timestamp) —
lands as the first registered adapter. The current shared single cursor is adequate for one ingest
source; per-source cursors arrive with that connector.

**Consequence.** A second source is one registry entry + one config token, zero edits to the ingest
loop or the evidence gatherer — proven by a fake retriever appearing in `gather_evidence` and a fake
source's halves being selected, all offline.
