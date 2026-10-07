"""The calculation cache, the calibration ledger, and the shapes both are about.

Not the calculators: the xTB/CREST engines are `Chemclaw3-mcp`'s `servers/calc`, exposed as
individually keyed primitives. A composite whose key would name an output is decomposed and composed
here, so every step is cached. What stays is what a stateless server cannot hold:

- `store.py` / `postgres_store.py` — the D-011 cache: an identical calculation is computed once.
  This side's `CALCULATION_EPOCH` composes with the server's (folded over its `params_hash` by
  `connectors/calc/remote.remote_key`), so a bump on either side alone invalidates every stored row;
  `tests/test_calc_remote.py::test_the_two_epochs_compose_rather_than_having_to_match` holds it.
- `calibration.py` — the prediction ledger, keyed on `(calc_type, calc_version, input_hash)`.
  Nothing here derives a version; it comes off a result or `calculation_key`.
- `artifacts.py` / `postgres_artifacts.py` — the content-addressed store for a run's by-products.
- `models.py` — every shape the cache reconstructs and the Temporal wire carries.
- `thermo.py` — RRHO over a Hessian and Boltzmann weights over an ensemble, which depend on a
  temperature the server never saw.
- `logd.py` — the Crippen sum and the Henderson-Hasselbalch term over a remote pKa.
- `uncertainty.py`, `solvents.py` — the uniform estimate shape and the supported-solvent check.

The server client (`connectors/calc/remote.py`) and the composition (`connectors/calc/compose.py`)
live one layer up: `science` may import only `core`.
"""
