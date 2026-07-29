# D-053 — Consolidate ELN source selection onto the F7 seam; memory honors `data_sources` (audit DUP-1)

**Context.** The forensic audit (`docs/audit/`) found the F7 "generic data-source seam" migration
left half-done. Two registries were live at once: `sources/registry.py` (config-driven via
`settings.data_sources`, used by the durable ELN sync) and `eln/registry.py` (a hardcoded
json+ord union via `all_eln_adapters()`, used by the memory-synthesis jobs). With the default
`data_sources="graph,eln-json"`, the durable sync ingested JSON only while the memory jobs read
json+ord — the two subsystems disagreed on the corpus, and `CHEMCLAW_DATA_SOURCES` silently had no
effect on memory synthesis, breaking the F7 "config, not code" guarantee.

**Decision.**
- `workflows/memory_jobs._all_reactions()` now reads `sources.registry.active_ingest_sources()` (the
  ingest halves of the configured active sources) instead of `eln.registry.all_eln_adapters()`.
- `eln/registry.py` is deleted — `sources/registry.py` is the single source-selection registry.
- `settings.eln_sync_adapter` is clarified as the ELN sync's **cursor-key label** (it was already only
  that after F7 — the sync ingests `active_ingest_sources()`, not this field); the field is kept so
  the stored high-water cursor key is stable, with a corrected docstring.

**Consequence (intentional behavior change, signed off).** Memory synthesis now honors
`data_sources`. With the default config it reads the JSON ELN source only; **ORD reactions are no
longer included in memory synthesis until `eln-ord` is added to `CHEMCLAW_DATA_SOURCES`.** This makes
the sync and the memory jobs read the identical, config-driven source set, so the two corpora can
never disagree again.

**Result.** `make lint type test` green; `mypy --strict` clean. New `tests/test_memory_jobs.py` pins
that the memory corpus tracks `data_sources` (adding `eln-ord` expands it; a retrieve-only config
yields an empty corpus); the removed `eln/registry.py` tests are covered by
`tests/test_datasource_seam.py`.
