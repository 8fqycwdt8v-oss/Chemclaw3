# D-132 — The Hessian is its own calculation: splitting the matrix from the thermochemistry computed over it

**Context.** D-124 kept the by-products a calculation used to destroy. Keeping them was worth
nothing until something read one back, and the first thing worth reading back was the Hessian.

The defect the storage audit found is narrow and expensive. `ThermoSpec` carried
`temperature_k`, `pressure_pa`, `symmetry_number`, `rrho_cutoff_cm` *and* `displacement_angstrom`
in one model, and `XtbSpec.cache_key` keys on every field via `model_dump()`. So asking for
thermochemistry at 350 K after 298 K was a cache miss that recomputed the second derivatives —
a quantity that does not depend on temperature at all. Measured in D-092, that matrix costs 26 s
on 76 atoms through the binary and 218 s through finite differences. **The one question a stored
Hessian answers trivially was the exact question that forced a full recomputation.**

**Decision.** A Hessian becomes a cached calculation in its own right (`calc/xtb_hessian.py`),
keyed by `HessianSpec` — the geometry, the method, the displacement, and nothing else.
`ThermoSpec` is unchanged and keeps every field it had; it gains `hessian_spec()`, the projection
onto what a Hessian actually depends on. Two `ThermoSpec`s differing only in a state variable
project onto the *same* `HessianSpec`.

So a second temperature is a miss on the thermochemistry — correct, the free energy really does
differ — and a hit on the Hessian. Minutes of second derivatives become milliseconds of partition
functions.

**The matrix lives in the artifact store, not in the result row.** A 76-atom Hessian is 228x228
float64: 416 kB, which has no business in JSONB. The row (`HessianResult`) holds content
addresses; the arrays are `.npy` blobs beside the Turbomole `hessian` and `vibspectrum` files the
binary wrote. This is what makes D-124 load-bearing rather than decorative — the artifact store is
now on the read path, not only the write path.

**A cached row whose artifact is gone is a miss, not a hit.** This is the load-bearing detail and
the reason `run_cached_hessian` is not built on `run_cached_with_artifacts`: those decide hit
versus miss from the result row alone, and here the row is only half the result. Artifacts are
optional by construction (D-124) — the store can be disabled, an artifact can exceed the cap, the
eviction sweep may reclaim a blob — so the read path verifies it can load the matrix before
claiming a hit, and a shape disagreeing with `atom_count` is rejected too. Without this, eviction
would be data loss and a mismatched blob would produce plausible, wrong frequencies.

A deployment with `artifact_store_enabled=False` therefore caches no Hessians and recomputes
exactly as it did before this split. That is a stated consequence, not a silent degradation.

**Two things this deleted.** `_CACHED_DIPOLE_DERIVATIVES` was a module-global dict handing
tblite's dipole derivatives across one call; with the Hessian cached, a hit would find it empty
and the IR intensities would break, so the derivatives became an artifact and the global went
away. And `compute_thermochemistry_with_artifacts` — added a week ago by D-124 — is gone, because
artifacts now belong to the layer that produces them.

**Also in this decision.**

- **`max_members` left the conformer cache key (STO-3).** It truncates a finished ensemble; it
  does not search. Keying on it meant "show me 20 instead of 10" re-ran CREST — by
  `calc/conformers.py`'s own docstring the most expensive single calculation in the system — to
  obtain an answer already in the store. `XtbSpec.unkeyed_fields()` is the seam: overriding it
  keeps the key derivation in one place, so a new field is still keyed by construction and
  *excluding* one is the visible, deliberate act.

- **A cross-method geometry pointer (STO-4), opt-in by construction.** The optimization cache keys
  on coordinates, so two RDKit embeddings of one molecule miss each other and a GFN-FF minimum
  cannot seed a GFN2 run. `calc/geometry.py` records the best known geometry per *subject*
  (canonical SMILES + charge + multiplicity + solvent) as an ordinary cached calculation — no new
  table. `run_cached_optimization` writes to it and deliberately does **not** read from it:
  silently swapping a caller's starting geometry would make one request return different answers
  under one key depending on what the store happened to hold, which is precisely the cache
  dishonesty `calc/xtb_spec.py` was written to prevent. The reuse is an explicit lookup that
  resolves a subject and *then* optimizes normally, so the key always names what really ran.

**Costs, stated rather than discovered.** Existing `xtb.hess` and `xtb.conformers` cache rows
cold-start: the key shapes changed, and there is no migration path that would be honest about what
the old rows contain. A stored conformer ensemble is now the whole ensemble rather than a
truncated one, so those rows are larger — `total_found` already reported the true count, so
nothing starts lying; the row simply holds what it counted.

**A finding that revised the plan.** The audit assumed every task had by-products worth keeping and
that the optimizer's capture path merely needed wiring. It does not: `xtbopt.xyz` is parsed in full
into `OptimizationResult.structure`, which the cache already persists, so capturing it would be a
second copy of the cache. `_ALREADY_STORED` names it alongside `xtbout.json` and an `opt` run
captures nothing. The same reasoning applies to CREST, whose ensemble file is now fully represented
in the result row. `hessian`/`vibspectrum` are *not* on that list even though the `.npy` holds the
same numbers: the two serve different readers — the `.npy` is this system's read path, the
Turbomole files are what every other quantum chemistry program can open — and content addressing
means two runs over an identical geometry share one copy of each.

**Alternatives rejected.** Putting the matrix in JSONB (a 416 kB row on the hot path, and Postgres
would TOAST it anyway with none of the dedup). Making artifacts mandatory once something reads them
(it would turn the eviction sweep D-124 built into data loss). Keying the Hessian on the full
`ThermoSpec` and post-correcting the thermochemistry (the recomputation is exactly what this
removes).

**Verification.** `tests/test_xtb_hessian.py` asserts the property end to end as a call count on
the expensive half: two thermochemistry requests differing only in temperature produce two
different free energies and exactly **one** Hessian. Also pinned: the negative control
(`displacement_angstrom` still forces a recomputation), the evicted-artifact fallback, the
disabled-store behaviour, and the shape check. `tests/test_conformers.py` and
`tests/test_geometry.py` cover STO-3 and STO-4, including that consulting the geometry pointer
never changes an optimization's cache key.

**Not covered here.** The end-to-end assertions that need the `xtb`/`crest` binaries are
`@needs_xtb`/`@needs_crest` and do not run in the environment this was written in. Every logic path
that does not need a binary is tested by a test that actually runs.

---
