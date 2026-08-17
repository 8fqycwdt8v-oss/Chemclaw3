# `src/chemclaw/science/calc/` — correctness pass

Files read in full: `store.py`, `postgres_store.py`, `artifacts.py`, `postgres_artifacts.py`,
`thermo.py`, `models.py`, `calibration.py`, `logd.py`, `uncertainty.py`, `solvents.py`, `__init__.py`.
Cross-checked against the consumers that actually drive them (`connectors/calc/compose.py`,
`connectors/calc/remote.py`, `connectors/calc/server/tools.py`) and, where a claim was about the
other side of the wire, against `8fqycwdt8v-oss/Chemclaw3-mcp`'s `servers/calc` source.

Scripts referenced below live under
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/`.

---

## `_align_intensities` does not perform the check its docstring says it performs

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/thermo.py:124-138` (`_align_intensities`), call site
  `thermo.py:320-323` (`thermochemistry_from_hessian`)
- **Trigger**: any `HessianPayload` on the `ir_intensities` branch (the `xtb`-binary backend) whose
  leading external-mode block does not have exactly `intensities.size - modes` entries — most
  plausibly a linear/non-linear disagreement between `_is_linear` here
  (`moments[0] < moments[2] * 1e-4`, `thermo.py:156-158`) and xtb's own linearity test, which
  changes the external count between 5 and 6.
- **Consequence**: every reported IR intensity is silently paired with the wrong mode. The
  `zip(..., strict=True)` at `thermo.py:389` cannot catch it, because the slice is *defined* as
  `intensities[intensities.size - modes:]` and therefore always has exactly `modes` entries. A
  genuinely absorbing band is reported as IR-silent and the last real band's intensity is dropped.
  `strongest_bands()` then selects the wrong bands for a spectrum comparison.
- **Evidence**: the docstring claims the opposite —

  > "Reconciling by count is the point — if the two projections disagree about how many external
  > modes a molecule has, every intensity would shift by one mode, so a mismatch fails loudly
  > instead (gate G4)."

  The code only raises when `external < 0`, i.e. when the server reported *fewer* entries than the
  projection found modes:

  ```python
  external = intensities.size - modes
  if external < 0:
      raise ValueError(...)
  return intensities[external:]
  ```

  There is no check that `intensities.size == 3 * N`, nor that `external in (5, 6)` —
  and `HessianPayload.atom_count` (`models.py:490`), which is exactly the datum needed, is carried
  across the wire and **never read anywhere in this package**
  (`grep -rn "atom_count" src/chemclaw/science` → one hit, the field declaration).

  Reproduced with `probe3.py`, driving the *recorded* CO2 Hessian from
  `tests/fixtures/calc_hessians.json` (real payload, 3 atoms, 4 vibrational modes) through
  `thermochemistry_from_hessian` with three different `ir_intensities` lists:

  ```
  atom_count: 3 -> 3N = 9
  server says linear (5 ext)       len=9  modes=4  reported intensities=[11.0, 22.0, 33.0, 44.0]
  server says NON-linear (6 ext)   len=9  modes=4  reported intensities=[0.0, 22.0, 33.0, 44.0]
  24-entry list (3N=9)             len=24  modes=4  reported intensities=[99.0, 99.0, 99.0, 99.0]  <- no error
  ```

  Row 2 is the shift: the first real band comes back at 0.0 km/mol and the fourth band's intensity
  is gone, with no error raised. Row 3 shows a list of a completely unrelated length is accepted too.

  Reachability note, stated honestly: I could not prove the trigger occurs against the real server.
  All three recorded fixtures come from the tblite/dipole-derivative path (`ir_intensities: None`),
  so **the `ir_intensities` branch has no recorded evidence in this repository at all** — the
  layout it assumes (all `3N` entries, externals first) is asserted only in prose, here and in
  `Chemclaw3-mcp`'s `xtb_hessian.Hessian` docstring.
- **Fix**: check rather than infer. `_align_intensities` already receives `structure`; pass or read
  `atom_count` and assert the shape:

  ```python
  atoms = len(structure.elements)
  external = intensities.size - modes
  if intensities.size != 3 * atoms or external not in (0, 5, 6):
      raise ValueError(
          f"the server reported {intensities.size} intensities for a {atoms}-atom structure "
          f"with {modes} vibrational modes; the external block is {external}"
      )
  ```

  and add a fixture from the `xtb`-binary path so the branch has one recorded payload behind it.

---

## Ensemble truncation drops conformers by energy order, not by population

- **Severity**: medium
- **Location**: `src/chemclaw/science/calc/thermo.py:474-490` (`ensemble_from_members`, the
  `conformers[:max_members]` slice)
- **Trigger**: a CREST ensemble in which a conformer beyond index `max_members - 1` carries a large
  rotamer `degeneracy`. `settings.crest_max_members` is 20; a `--extensive` search on a flexible
  substrate routinely returns more.
- **Consequence**: the returned `conformers` list omits the *dominant* member while
  `total_found`, the populations and the entropy still describe the whole ensemble. The result then
  reads as "here are the 20 that matter out of 47" — the docstring's own words — when the one
  carrying most of the population is not among them. This is the same failure mode the module goes
  to some length to argue about elsewhere: degeneracy is load-bearing (n-butane 73% → 59%), and the
  truncation is the one place it is ignored.
- **Evidence**: populations are computed with degeneracy (`thermo.py:460-465`) but the list is never
  re-ordered by them — the slice takes `payload.members` order, which is energy order.
  Reproduced with `probe2.py` (three members at 0.0 / 0.31 / 0.63 kcal, degeneracies 1 / 1 / 60):

  ```
  === B: truncation is by list order, not by population ===
  all populations : [0.0447, 0.0263, 0.9291]
  kept (max=2)    : [0.0447, 0.0263] -> dropped the 92.9% conformer
  ```
- **Fix**: truncate by what the truncation claims to rank by —

  ```python
  ranked = sorted(conformers, key=lambda c: c.population, reverse=True)[:max_members]
  conformers=sorted(ranked, key=lambda c: c.relative_kcal)
  ```

  keeping `total_found`, the populations and the entropy untouched as they already are.

---

## `ConformerEnsemble.lowest` and the truncation both assume an ordering nothing on this side declares or checks

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/models.py:687-690` (`ConformerEnsemble.lowest`),
  `thermo.py:474-490`; consumer `connectors/calc/compose.py:612` (`_species_energy`, `thorough`)
- **Trigger**: an `EnsemblePayload` whose `members` are not ascending in `energy_hartree`.
- **Consequence**: `lowest` returns `conformers[0].structure` — the *first* member, not the minimum.
  At `thorough` level that structure seeds the whole optimisation + Hessian + free-energy chain for
  one species, while `ensemble_correction_kcal` (defined relative to the true minimum) is added on
  top, so the reported ΔG is not even internally consistent. Nothing raises.
- **Evidence**: `ensemble_from_members` never sorts; `EnsemblePayload` (`models.py:623-640`) states
  no ordering contract, and `Conformer`/`ConformerEnsemble` state none either — only the `lowest`
  docstring's bare claim "The lowest-energy member". Reproduced with `probe2.py` (members at
  −10.000 / −10.005 / −10.002 Hartree):

  ```
  === A: members not sorted by energy ===
  relative_kcal in returned order: [3.138, 0.0, 1.883]
  populations                    : [0.0048, 0.9554, 0.0398]
  `lowest` relative_kcal         : 3.138 -> lowest is NOT the minimum
  ```

  Two things keep this at *low*. First, the producer does hold up its end today: `Chemclaw3-mcp`'s
  `engine/crest_cli.py::run` ends with
  `return sorted(paired, key=lambda member: member.energy_hartree)` and its comment names this exact
  risk. Second, the consumer side is inconsistent about trusting it — `compose.interaction()`
  (line ~486) takes `min(modes.members, key=...)` over the *same* `EnsemblePayload` type, while
  `_species_energy` takes `[0]`. One of the two is wrong about what the contract is.
- **Fix**: make the invariant local. Sort inside `ensemble_from_members` before building
  `conformers` (it is `O(n log n)` on tens of members), which also lets `compose.interaction()` drop
  its `min()`. A one-line `sorted(...)` removes a cross-repository assumption that costs nothing to
  enforce here. Separately, `crest_max_members` (`core/config/calculators.py:53`) carries no
  `ge=1`, so `CHEMCLAW_CREST_MAX_MEMBERS=0` makes `lowest` an `IndexError`.

---

## The two `ResultStore` backends disagree on a timezone-naive `since`/`until`

- **Severity**: low
- **Location**: `src/chemclaw/science/calc/store.py:284-287` (`_matches`) vs
  `postgres_store.py:47-58` (`_FIND`); producer `connectors/calc/server/tools.py:255-264`
  (`_timestamp`)
- **Trigger**: `find_calculations(since="2026-08-01")`. `datetime.fromisoformat("2026-08-01")`
  returns a **naive** datetime and `CalculationQuery.since: datetime | None` accepts it.
- **Consequence**: the in-memory backend raises `TypeError: can't compare offset-naive and
  offset-aware datetimes`; the Postgres backend does not raise — it hands a naive `timestamp` to a
  `timestamptz` comparison, which Postgres resolves against the session `TimeZone`. Nothing in
  `core/db.py::_merged_options` (line 82) sets `TimeZone`, so on a deployment whose server default
  is not UTC the window silently shifts by the offset and rows fall out of a listing whose own tool
  docstring calls an empty result "a real answer".
- **Evidence**: `_matches`'s docstring states the opposite — "Shared by the in-memory store and by
  the tests that pin the two backends agreeing; the Postgres store expresses the same predicate as
  SQL". `InMemoryStore.find`'s docstring already records this class of bug being hit once (the
  `datetime.max` sentinel) and fixes only the `created_at` half, not the query half. Reproduced with
  `probe2.py`:

  ```
  === C: naive `since` against a tz-aware row ===
  InMemoryStore.find raised: can't compare offset-naive and offset-aware datetimes
  ```
- **Fix**: normalise at the boundary — a `model_validator` on `CalculationQuery` that attaches
  `UTC` to a naive `since`/`until` (or rejects it). One place, both backends.

---

## The fake calculation server derives `input_hash` differently from the real one, so `find(smiles=…)` is untested

- **Severity**: low (test fidelity; production is correct)
- **Location**: `src/chemclaw/science/calc/store.py:160-167` (`molecule_hash`) vs
  `tests/calc_server_fake.py:189-199`
- **Trigger**: any test that stores a row keyed by the fake server and then queries it by molecule.
- **Consequence**: no such test exists, so the one code path `molecule_hash` was written for has no
  end-to-end coverage — and the fake, whose docstring promises "**Keys are derived the way the
  server derives them**", derives the one thing `find` depends on differently. A future test written
  over the fake would show `find(smiles=…)` returning nothing and be read as "the store is empty".
- **Evidence**: the fake hashes the bare canonical string
  (`inputs = require_canonical_smiles(arguments["smiles"])`, then `stable_hash(inputs)`); the real
  server hashes a mapping (`Chemclaw3-mcp` `engine/solubility.py::cache_key`:
  `inputs={"smiles": require_canonical_smiles(job.smiles)}`, and `engine/pka.py::pka_cache_key` the
  same). `molecule_hash` matches the real server. `hash_check.py`:

  ```
  canonical            : CCO
  server  input_hash   : f29e20f49d416e54     <- what tests/calc_server_fake.py derives
  find()  molecule_hash: a7d334ebee616d78     <- what find() compares against
  equal?               : False
  find(smiles='CCO')   : []
  find(calc_type='pka'): 1
  ```

  Corroborated in the other direction: the recorded key in `tests/test_calc_remote.py:96`
  (`"input_hash": "07010a68dabf6858"` for `predict_solubility` on `c1ccccc1`) is exactly
  `stable_hash({"smiles": "c1ccccc1"})` = `molecule_hash("c1ccccc1")`, not the bare-string form. So
  two stand-ins in the same suite disagree about the server's key derivation.
- **Fix**: change `tests/calc_server_fake.py` to `stable_hash({"smiles": canonical})` for the
  molecule-keyed tools, and add one test that stores a fake-server-keyed pKa row and finds it by
  SMILES. Separately, `find_calculations`'s docstring advertises "descriptors" as a molecule-keyed
  type; the server's type is `developability`.

---

## What I checked and found sound

Recorded so a later pass does not repeat the work.

- **The RRHO arithmetic is right, verified numerically rather than by reading.**
  `thermo_check.py` drove `_translational` / `_rotational` / `_vibrational` on water at 298.15 K,
  101325 Pa:

  ```
  S_trans 34.6091 (NIST 34.61) · S_rot 10.4504 (10.47, sigma=2) · S_vib 0.0079
  S_total 45.067 cal/mol/K against the experimental 45.10
  ZPE 12.877 kcal/mol = 0.5·hc·(1594.7+3657.1+3755.9), exact
  sigma 1→2 shift = 1.3774250290482237 vs R ln2 = 1.3774250290482222
  CO2: linear detected, vibrational subspace dim 4 (3N-5), S_rot 13.07
  ```

  Unit chains check out independently: `HARTREE_TO_KCAL`, `_GAS_CONSTANT_CAL` (= R/4.184),
  `_J_PER_MOL_TO_KCAL`, `hartree_per_j_mol`, the amu·Å²→kg·m² factor in `_rotational`, the
  Hartree/Å²→J/m² factor in `_normal_modes`, and the `(D/Å)²/amu → km/mol` factor. Grimme's
  quasi-RRHO damping `1/(1+(ω0/ω)⁴)`, the free-rotor `μ' = μB/(μ+B)`, `H = U + RT`, `G = H − TS`,
  the spin term `R ln(2S+1)`, and the `S_conf = −R Σ p ln(p/g)` / `−T·S_conf` pair are all as
  published. `tests/test_calc_thermo.py` pins the same numbers against real recorded payloads.
- **`_vibrational_basis` handles the linear case correctly** — the null rotation is dropped by
  moment of inertia rather than by singular value, and the SVD's external block is full rank in both
  the 5- and 6-column cases, so `null_space` returns exactly 3N−5 / 3N−6.
- **`CALCULATION_EPOCH` really is folded into every `calc` key.** Both places named in `store.py:33`
  do it (`CalculationKey.build` for the DFT path, `remote.remote_key` for the calc path), and the
  server folds it a *third* time inside its own `CalculationKey.build` (`Chemclaw3-mcp`
  `engine/key.py`). Redundant, but deterministic and consistent — a bump moves every key.
- **The calibration SQL is right.** `_SELECT_RECONCILED`'s column order matches the tuple unpacking
  in `reconciled_for`; likewise `_SELECT_LINKS` in `postgres_artifacts.list_for`. `record_prediction`
  runs the reverse reconciliation in the same transaction as the upsert; `record_observation` stores
  the measurement before reconciling, so an unpredicted measurement survives; the `observed_value IS
  NULL` guard on `_RECONCILE_FROM_MEASUREMENT` is correct and the deliberately version-agnostic
  `_RECORD_OBSERVATION` is too.
- **`logd.ionisable_sites` and `_lone_pair_is_available` are byte-equivalent to the server's own site
  enumeration** (`Chemclaw3-mcp` `engine/pka.py::_acidic_protons` / `_basic_nitrogens`), including
  all five element/valence constants — so the claim that the duplicated enumeration is "exactly as
  good as the predictor's" holds. The Henderson-Hasselbalch branch on `site` and the
  `ionised_ratio → ionised_fraction` domain guard are both correct.
- **The artifact layer round-trips.** Content address over uncompressed bytes, `encode` falling back
  to `"none"` when deflate does not shrink, `decode` raising on an unknown codec, `too_large`
  measured on uncompressed length in both backends, `byte_size`/`stored_bytes` written the right way
  round, and `ArrayOffloadingStore` writing blobs before the row and treating a missing blob as a
  miss rather than an error — all as documented. The `_address()` name mapping is collision-free for
  `HESSIAN_ARRAYS`, and `payload` mutation is confined to local copies.
- **`cached_compute`'s check-then-act** on a shared store is documented as such and matches
  `InMemoryStore`/`PostgresStore` behaviour; `_UPSERT`'s `COALESCE` on `compute_seconds` does keep
  the original miss's cost.
- **`solvents.py`** is pure stdlib as its "leaf module" contract requires, and `SUGGESTED_SOLVENTS`
  is a strict subset of `ALPB_SOLVENTS`.
