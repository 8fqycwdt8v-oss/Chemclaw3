"""The calculation cache, the calibration ledger, and the shapes both are about.

**Not the calculators.** The xTB/CREST engines moved to `Chemclaw3-mcp`'s `servers/calc` in
`D-2026-08-16-the-physics-leaves-the-cache-stays`, exposed as individually-keyed primitives. The
line was drawn on *composability* rather than on speed: a calculation whose identity is derivable
from its inputs is a primitive and moved; a composite — anything whose key would name an output —
was decomposed, because shipping one whole swallows the nested entries that are its entire economy.

What stayed is what a stateless server cannot hold:

- `store.py` and `postgres_store.py` — the D-011 cache. An identical calculation is computed once,
  ever, and `CALCULATION_EPOCH` is the one constant both repositories must change in the same PR.
- `calibration.py` — the prediction ledger, keyed exactly on `(calc_type, calc_version,
  input_hash)`. The version comes off a result or from `calculation_key`; **nothing here derives
  one**, and `tests/test_calc_remote.py` asserts that statically because getting it wrong is silent.
- `artifacts.py` / `postgres_artifacts.py` — the content-addressed store for a run's by-products.
- `models.py` — every shape the cache reconstructs and the Temporal wire carries.
- `thermo.py` — the statistical mechanics that had to stay: RRHO over a Hessian, Boltzmann weights
  over an ensemble. Both depend on a temperature the expensive half never saw, which is exactly why
  the composites they belong to were decomposed rather than shipped.
- `logd.py` — the Crippen sum and the single Henderson-Hasselbalch term over a remote pKa.
- `uncertainty.py`, `solvents.py` — the uniform estimate shape and the supported-solvent check.

The client that reaches the server is `connectors/calc/remote.py`, and the composition over its
primitives is `connectors/calc/compose.py`: both are one layer up, because a Temporal import has no
business inside this package and `science` may import `core` and nothing else.
"""
