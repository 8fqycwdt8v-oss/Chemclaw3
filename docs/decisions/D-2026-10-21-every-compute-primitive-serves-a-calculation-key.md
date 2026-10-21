# D-2026-10-21-every-compute-primitive-serves-a-calculation-key — a compute server derives its own cache key with the kit's function, and the key names the engine build

**Status:** proposed · **Date:** 2026-10-21

## Context

D-011 says a persisted result is never recomputed. It holds only for `calc`.
- **calc.** The server's `calculation_key` returns `calc_type`, `calc_version`, `input_hash` and
  `params_hash`, where `calc_version` is `method + engine + backend_version`. Chemclaw3's
  `remote_key` folds its own `CALCULATION_EPOCH` over the server's `params_hash`.
- **Every other job.** It keys on `stable_hash([connector, job, payload])` in its workflow id,
  which names neither engine nor version.

`CalculationKey.build` exists twice, in `servers/calc/engine/key.py` and `science/calc/store.py`,
and a test pins the two copies against each other.

A second engine, or the same engine on two clusters, would share or miss cache rows silently.

## Options

1. **calc-only keys** (status quo). Each new backend invents its keying or skips it.
2. **A fleet-wide contract:**
   - The kit owns `keys.py`, holding `CalculationKey.build` and `stable_hash`.
   - `chemclaw_contracts.compute` owns `ComputeIdentity` and `KeyedResult`.
   - Any server declaring `compute: true` serves `calculation_key` for each of its compute tools.
   - `calc_version` adds the backend image digest for engines that run off the serving pod.
3. **Key in core only**, from the arguments. Core cannot see what an engine build changes, so the
   key would be well-formed and match nothing reliably.

## Decision

**Option 2.**
- `calc` moves onto the kit's function with byte-identical keys, proven by
  `tests/test_key_contract.py`.
- A fleet test calls `calculation_key` for every compute tool and refuses a null key that carries
  no stated caveat.
- Core's `cached_remote` becomes backend-agnostic, and `calculation_results` gains a `backend`
  column through an expand-and-contract migration.

## Consequences

- No cache row moves: the key format, `as_str()` and both epochs are unchanged.
- A key from a cluster backend names its engine version and image digest, so two clusters with
  different builds never share a row.
- `CalculationIdentity` stays an alias of `ComputeIdentity` for one contracts minor version.
- Plan item: W12 Track A.
