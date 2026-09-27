"""The calculation cache, the calibration ledger, and the shapes both are about.

**Not the calculators.** The xTB/CREST engines moved to `Chemclaw3-mcp`'s `servers/calc` in
`D-2026-08-16-the-physics-leaves-the-cache-stays`, exposed as individually-keyed primitives. The
line was drawn on *composability* rather than on speed: a calculation whose identity is derivable
from its inputs is a primitive and moved; a composite — anything whose key would name an output —
was decomposed, because shipping one whole swallows the nested entries that are its entire economy.

What stayed is what a stateless server cannot hold:

- `store.py` and `postgres_store.py` — the D-011 cache. An identical calculation is computed once,
  ever. **`CALCULATION_EPOCH` is not a constant the two repositories have to change together**, and
  saying it was — the sentence that stood here, inherited from
  `D-2026-08-16-the-physics-leaves-the-cache-stays` — was already contradicted by the module that
  does the folding: `connectors/calc/remote.remote_key` says "it would have appeared to work".
  The two epochs **compose**: the server folds its own into the `params_hash` it answers with, and
  `remote_key` folds this side's over that digest, so a bump on either side alone invalidates every
  stored row. Measured over all four combinations against the fleet's real `params_hash` at both of
  its values — all four keys distinct, either bump alone sufficient. They are moved together by
  convention, because it keeps the two epoch logs readable side by side, and not by an invariant
  anything can enforce: neither repository can read the other's constant at runtime, which is
  exactly why an agreement stated in prose is the shape
  `D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it` refuses.
  `tests/test_calc_remote.py::test_the_two_epochs_compose_rather_than_having_to_match` is the
  executable form, and it already existed while this sentence said the opposite — which is the
  reason to name it here rather than to restate the rule a third time.
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
