# D-011 — Results are persisted once, never recomputed (calculation store, first-class)

Every calculation goes through **one** result store keyed by
`(calc_type, calc_version, input_hash, params_hash)` — the calculator version is in the key so a
model/method update cannot silently poison the cache. One interface, swappable backend
(in-memory for tests, Postgres for real). This generalizes the QM-only step 1.10 into a
cross-cutting layer every calculator and every BO objective evaluation shares (DRY, no per-calc
cache). Plan Phase 1b.
