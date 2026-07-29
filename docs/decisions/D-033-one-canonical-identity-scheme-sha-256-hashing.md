# D-033 — One canonical identity scheme: SHA-256 hashing + canonical SMILES in every key

**Context.** In-depth review found the "compute once, never twice" guarantee (D-011) had a hole:
the calculation cache keys (`calc.xtb`/`pka`/`solubility`) and the QM workflow-dedup id
(`workflows.models.qm_job_key`) were built from the **raw** SMILES string, so `"CCO"` and `"OCC"`
— the same molecule — produced different keys and recomputed. Separately, four near-identical
canonical-JSON hash helpers had drifted: three used SHA-256 (at 12 or 16 hex chars) and
`qm_job_key` used **SHA-1** (48 bits — the weakest identity in the system, yet load-bearing as
workflow id, scheduler handle, and cache key at once).

**Decision.** Two shared modules now own identity: `chemclaw.ids.stable_hash(payload, *, chars)`
(the one canonical-JSON + SHA-256 helper, all four call sites ported) and `chemclaw.chem`
(`canonical_smiles` moved here from `eln.chem` since the compute layer needs it too, plus a strict
`require_canonical_smiles` that raises `InvalidSmilesError`). Every calculator cache key and
`qm_job_key` canonicalizes the SMILES before hashing; `qm_job_key` moved to SHA-256 (16 hex / 64
bits). `prepare_input` (the QM G4 boundary) now canonicalizes, so an invalid molecule is rejected
at the durable boundary instead of flowing through the mock into a stored result. `InvalidSmilesError`
was added to `publish._BAD_DATA_TYPES` (Temporal matches non-retryable types by exact class name).

**Key-material change.** `qm_job_key` output changed (algorithm + canonicalization), so QM workflow
ids and QM cache entries for pre-existing non-canonical inputs are a one-time miss — acceptable while
the cache is young. The `calc` cache keys kept SHA-256[:16], so only genuinely non-canonical SMILES
re-key; canonical inputs still hit existing rows. `eln.chem` was deleted (its two callers now import
`chemclaw.chem`).

**Result.** `tests/test_ids.py` proves equivalent SMILES share one key across all three calculators
and `qm_job_key`, and that invalid SMILES are rejected. Lint/type/test green.
