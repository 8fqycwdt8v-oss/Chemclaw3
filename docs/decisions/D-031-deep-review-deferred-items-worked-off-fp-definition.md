# D-031 — Deep-review deferred items worked off: fp-definition guard, ELN re-drive, KISS cleanups

**Context.** D-030 deferred three items with documented reasons; this closes all three.

**Decision — done.**
- **Fingerprint-definition guard (was latent LOW).** Every fingerprint row now records the
  *definition* that produced its bits (`ecfp:r{radius}:b{bits}`, `drfp:b{bits}` — from
  `molecule_definition()`/`reaction_definition()`), and similarity search returns only rows
  matching the store's current definition. Equal-width bits of a different Morgan radius are
  incomparable; the width check (`bit(N)`) can't catch that, but the definition filter does —
  after a definition change, stale rows fall out of similarity search (safe: no wrong scores,
  just missing hits) until re-indexed. The durable `PostgresFingerprintStore` takes the
  definition as a constructor arg and filters in SQL; the ephemeral `InMemoryFingerprintStore`
  filters only when explicitly bound to a definition (it can't accumulate mixed definitions, so
  the default is unfiltered — this also makes the guard testable without Postgres). Migration
  `004_fingerprint_definition.sql` adds the column (002/003 carry it for fresh DBs). Substructure
  search stays unfiltered by design — it re-matches the stored SMILES with RDKit and never
  touches the bits, so a stale-definition row is still a correct substructure hit. Runbook (vi).
- **ELN reject re-drive (was MEDIUM).** `RejectedEntry` now carries the entry's `created_at`,
  and the rejection WARNING logs it — the exact `since` an admin re-runs the sync from to
  re-ingest a corrected entry. The re-drive capability already existed (the sync is re-runnable
  from any earlier cursor; ingestion is idempotent); this makes each rejection self-describing.
  No dead-letter/automatic re-drive was built (KISS — deterministic bad data shouldn't retry
  itself). Runbook (v) documents the procedure.
- **KISS cleanups.** (a) Inlined the single-implementer `SolubilityModel` seam: removed the
  Protocol, the never-passed `model=` param on `predict_solubility`/`run_cached_solubility`, and
  the `_DEFAULT_MODEL` indirection — `EsolBaseline` is now called directly (reintroduce a seam at
  the second model, Rule of Three). (b) Deleted `report.harness.gather_report` (no production
  caller — the report workflow assembles the `Report` itself, per-section, for durability); its
  three tests now assemble via a local `_gather` helper over `gather_section`. (c) Wired
  `memory.interaction.note_from_confirmed_answer` (was implemented+tested but unreachable) into a
  new agent tool `record_confirmed_answer` (`agents/memory_tools.py`) that routes a
  chemist-confirmed answer through the PR-gate — completing plan step 5.5's "user interaction as
  the fourth memory source" instead of deleting it. (d) Kept `StoredResult.provenance` after
  review: it is accurate GxP audit metadata (every value in a compute cache *is* "computed"), not
  a dead stub; docstring clarified that it is audit trail, not a control signal (no code branches
  on it), and the seam for a future `provenance="measured"` value under the same key.

**Result.** `make lint type test` green: 229 passed / 16 skipped (sandbox-infra only). New/moved
tests: the in-memory definition-exclusion guard, the `record_confirmed_answer` gate test, and the
retargeted report gathers.
