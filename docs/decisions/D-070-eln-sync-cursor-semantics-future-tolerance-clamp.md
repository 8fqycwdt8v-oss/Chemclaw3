# D-070 — ELN sync cursor semantics: future-tolerance clamp, overlap window, chunked activities

**Context.** Three independent failure modes could silently stall or starve ELN ingestion: (1) one
future-dated entry timestamp became the persisted high-water cursor, permanently skipping all later
real entries; (2) an export file landing *after* a newer-stamped sibling was dropped forever by the
`created_at >= since` filter; (3) the sync activity ingested an unbounded backlog in one 300s
attempt with no heartbeat.

**Decision.** The cursor still advances past sane-timestamped rejections (re-fetching
deterministic bad data only re-rejects it), but entries stamped beyond wall clock +
`eln_sync_future_tolerance_seconds` are rejected *without* cursor advance; every fetch reaches
`eln_sync_overlap_seconds` behind the cursor (idempotent ingestion makes re-fetch free) with the
cursor floored at `since`; and the activity heartbeats and ingests in cursor-persisting chunks of
`eln_sync_batch_size`, capping only past-cursor entries so a truncated chunk strictly advances.
Relatedly, memory note ids now anchor on the cluster's *smallest member* rather than the full
member set, so a grown cluster supersedes its note in place through the PR-gate instead of minting
a duplicate note per sync.

**Consequence.** A typo'd year, a late-landing export, or a large backfill each degrade to a
visible per-run warning and bounded catch-up work instead of silent permanent data loss.

**Result.** Behavior tests in `tests/test_eln.py`, `tests/test_eln_workflow.py`,
`tests/test_memory.py`; chunk-resume proven with cursor persistence per chunk.
