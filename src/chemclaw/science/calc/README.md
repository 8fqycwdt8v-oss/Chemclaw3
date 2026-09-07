# `science/calc` — the calculation cache, the calibration ledger, and the shapes both are about

**Not the calculators.** The xTB and CREST engines moved to `Chemclaw3-mcp`'s `servers/calc` in
`D-2026-08-16-the-physics-leaves-the-cache-stays`, on a boundary drawn by *composability* rather
than speed: a calculation whose identity is derivable from its inputs is a primitive and moved; a
composite, whose key would name an output, was decomposed so every nested step stays separately
cached.

What stayed is what a stateless server cannot hold:

- `store.py`, `postgres_store.py` — the D-011 cache. An identical calculation is computed once,
  ever, and concurrent misses on one key in one process share one computation.
- `calibration.py` — the prediction ledger, keyed exactly on `(calc_type, calc_version,
  input_hash)`; nothing here derives a version.
- `artifacts.py`, `postgres_artifacts.py`, `structures.py` — the content-addressed store for a run's
  by-products, and the structures a job was asked about.
- `models.py` — every shape the cache reconstructs and the Temporal wire carries.
- `thermo.py`, `uncertainty.py`, `logd.py` — the arithmetic that depends on something the expensive
  half never saw (a temperature, a pH), which is exactly why its composites were decomposed.
- `solvents.py`, `budget.py`, `geometry.py` — the supported-solvent check, the spend ceiling, and
  geometry handling.

A cache is **not a record**: this store maps a key onto an opaque payload and refuses any predicate
on it, which is why the scientific record is `publish/`'s job instead.
