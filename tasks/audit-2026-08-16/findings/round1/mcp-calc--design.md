# `servers/calc` — design and simplification

Slice: `servers/calc/src/chemclaw_mcp_calc/tools.py`, `engine/pka.py`, `engine/xtb_opt.py`,
`engine/xtb_cli.py` and the tests under `servers/calc/tests/`.

Seven findings. The first three are one root cause seen from three sides: **`XtbSpec.engine` is
written into every key as "which implementation runs it", and three of the five code paths that
consume a spec never ask it.** Everything else is ordinary duplication and stale self-description.

All reproductions were run with `uv run` inside `/workspace/chemclaw3-mcp`; scripts are under
`/tmp/calcaudit/`.

---

## `engine` is in every xTB key but the single point, the properties and the Fukui paths never dispatch on it

- **Severity**: high
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/xtb_spec.py:98` (`XtbSpec.engine`) and
  `:153` (`XtbSpec.calc_version`); consumed without dispatch at
  `engine/xtb.py:49` (`_energy` → `gfn2_energy`), `engine/xtb_props.py:170`
  (`compute_properties`) and `engine/xtb_props.py:222` (`compute_fukui`). Surfaced by
  `tools.py:189` `compute_xtb_energy`, `tools.py:263` `compute_electronic_properties`,
  `tools.py:295` `predict_site_reactivity`.
- **Trigger**: any deployment where `resolve_backend()` answers `"xtb"` — i.e. the documented
  supported case of an image that ships the `xtb` binary under the `auto` default, or an explicit
  `CHEMCLAW_XTB_ENGINE=xtb`. Call `compute_xtb_energy("CCO")`.
- **Consequence**: the number is produced by tblite in-process — `xtb_cli.run` is never called —
  and is stamped `calc_version = "GFN2-xTB+xtb+xtb-6.6.1/…"`. Two pods that differ only in whether a
  binary is *installed* compute byte-identical energies and write them under different
  `calc_version`/`calc_key`. On Chemclaw3's side that is a permanently partitioned calculation cache
  (every entry recomputed once per pod flavour) and a partitioned `predictions` ledger, which
  `calculator_trust` reports as `UNCALIBRATED` rather than as an error. It is exactly the failure
  `XtbSpec.calc_version`'s own docstring forbids: *"name every program whose output survives into the
  stored payload, and no program that does not run."*
- **Evidence**: `xtb_cli.CliTask` has an `sp` member and `xtb_cli.run(..., task="sp")` exists, so the
  binary path was intended; nothing calls it. `engine/xtb.py:49-70` builds `resolved =
  spec.for_structure(structure)`, uses `resolved.calc_version()` for the key, then unconditionally
  calls `gfn2_energy(...)`, which is `run_singlepoint` → `tblite.interface.Calculator`
  (`engine/xtb_engine.py:278-291`, `:212-252`). `xtb_props` does the same with `run_singlepoint`.

  `/tmp/calcaudit/engine_split.py` — same process, only `shutil.which("xtb")` differs:

  ```
  pod A energy : -11.393351662348477
  pod B energy : -11.393351662348477
  identical    : True
  pod A key    : xtb.sp@GFN2-xTB+tblite+tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7
  pod B key    : xtb.sp@GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2:389b625b3220108a:b41312b0cdc59ab7
  ```

  `/tmp/calcaudit/engine_claim.py` (binary genuinely absent, `CHEMCLAW_XTB_ENGINE=xtb`) confirms the
  dispatch never happens:

  ```
  compute_xtb_energy  version: GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2
  electronic_props    version: GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2
  site_reactivity     version: GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2
  xtb_cli.run invocations    : 0
  ```

  The guard that looks like it covers this does not:
  `tests/test_calc_version.py:118 test_the_version_names_the_programs_that_actually_ran` asserts only
  that the string contains `"GFN2-xTB"`, contains `"tblite-"`, and does *not* contain `"auto"`. It
  never checks that the named backend is the one that ran, so the property its name claims is
  untested.
- **Fix**: make `for_structure` the one place that answers "which backend can actually run this spec
  on this structure", since `cache_key` already routes through it and `identity.py` therefore
  inherits the answer for free. Add to `XtbSpec.for_structure` (`xtb_spec.py:118`) a downgrade to
  `tblite` for any task with no binary implementation:

  ```python
  _BINARY_TASKS = frozenset({"opt", "hess"})   # what xtb_cli.run actually implements
  ...
  if self.engine == "xtb" and (structure.uhf or self.task not in _BINARY_TASKS):
      return self.model_copy(update={"engine": "tblite"})
  ```

  Not behaviour-preserving, deliberately: it changes `calc_version`/`calc_key` on binary-equipped
  pods to name the program that ran. Alternative, if the binary path is wanted rather than the
  honest key, dispatch in `xtb.py`/`xtb_props.py` on `resolved.engine` — but that is new physics
  plumbing, whereas the above is a three-line correction of a claim.

---

## The frozen-atom fallback keeps `engine="xtb"`, so every `scan_point` on a binary pod is keyed as the wrong program

- **Severity**: high
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/xtb_opt.py:388-389`
  (`_optimize_with_binary`, the `if spec.frozen_atoms: return _optimize_with_library(spec, structure)`
  branch); surfaced by `tools.py:559 relax_structure` and `tools.py:672 scan_point`.
- **Trigger**: a pod where `resolve_backend()` answers `"xtb"`. Call
  `relax_structure(structure, frozen_atoms=[0])`, or **any** `scan_point` — `scan_point_inputs`
  (`engine/scan.py:129`) always sets `frozen_atoms`, so every point of every relaxed scan takes this
  branch.
- **Consequence**: the Cartesian L-BFGS-B path computes the geometry, and the result reports
  `engine="xtb"` with `calc_version` naming `xtb-6.6.1`. `OptimizationResult.engine`'s own comment
  (`xtb_opt.py:96-98`) states the field exists *"because the two do not agree to the last decimal, so
  a reader comparing two results needs to know they are comparable"* — it records the backend that
  did not run. Downstream, a free ANCopt relaxation and a constrained tblite relaxation of the same
  molecule are stamped with the same `calc_version`, and a scan profile's geometries are attributed
  to a program that was never invoked.
- **Evidence**: `/tmp/calcaudit/frozen_engine.py`, with `shutil.which` patched to report a binary and
  `xtb_cli.run` replaced by a recorder:

  ```
  xtb_cli.run invocations : 0 (0 == tblite did the work)
  result.engine           : xtb
  result.calc_version     : GFN2-xTB+xtb+xtb-6.6.1/tblite-0.7.0/rdkit-2026.3.5/h2
  result.calc_key         : xtb.opt@GFN2-xTB+xtb+xtb-6.6.1/…:ef0db0221aeab39a:e3eb8399f7b354a7
  scan_point engine       : xtb | xtb_cli.run calls: 0
  ```

  Compare the open-shell fallback, which handles the identical situation correctly by rewriting the
  spec in `for_structure` (`xtb_spec.py:133-135`) so that dispatch, `calc_version` and `cache_key`
  cannot disagree. The frozen-atom fallback was written at the dispatch site instead, and the key
  never learned about it.
- **Fix**: move it to the same resolver. Override in `OptSpec` (`xtb_opt.py:48`):

  ```python
  def for_structure(self, structure: Structure) -> Self:
      spec = super().for_structure(structure)
      if spec.engine == "xtb" and spec.frozen_atoms:
          return spec.model_copy(update={"engine": "tblite"})
      return spec
  ```

  Then delete the `if spec.frozen_atoms:` branch in `_optimize_with_binary` — it becomes
  unreachable, because `optimize_structure` already dispatches on `spec.for_structure(structure)`
  (`xtb_opt.py:181`). `identity._relax_structure` and `identity._scan_point` need no change: they go
  through `XtbSpec.cache_key`, which applies `for_structure` itself. Not behaviour-preserving for the
  key (that is the repair); fully behaviour-preserving for the geometry.

---

## `binary_version()`'s safety docstring is false when the engine is pinned

- **Severity**: medium
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/xtb_cli.py:216-232` (`binary_version`),
  claim at `:226-228`; the contradicting code is `engine/xtb_spec.py:51-54` (`resolve_backend`).
- **Trigger**: `CHEMCLAW_XTB_ENGINE=xtb` on an image without the binary — the shipped image, per this
  module's own opening note. Then call any xTB tool.
- **Consequence**: the docstring says *"Here it is honest: this process really did resolve the
  backend, found no binary, and `resolve_backend()` will therefore never select `"xtb"`, so the
  string never reaches a key."* `resolve_backend` returns an explicitly configured `"xtb"` **without
  consulting `is_available()`**, so `"absent"` is interpolated into `backend_version` and lands in a
  real `calc_version` and a real `calc_key` on a result that computed fine (tblite ran — see the
  first finding). That is precisely the silent-`UNCALIBRATED` trap the whole port exists to contain,
  reachable from a one-line environment setting rather than from a client re-derivation.
- **Evidence**: `/tmp/calcaudit/engine_claim.py`:

  ```
  xtb binary installed       : False
  binary_version()           : absent
  resolve_backend()          : xtb
  compute_xtb_energy  version: GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/rdkit-2026.3.5/h2
  ```

  And via pKa (`/tmp/calcaudit/pka_key.py engine`), where the string reaches the calibration ledger's
  primary key while the acid value is bit-identical to the unpinned run:

  ```
  base    pka=6.512636701266935  calc_version=…/opt-GFN2-xTB+tblite+tblite-0.7.0/…
  engine  pka=6.512636701266935  calc_version=…/opt-GFN2-xTB+xtb+xtb-absent/tblite-0.7.0/…
  ```
- **Fix**: make the pin fail loudly instead of encoding `absent`. In `resolve_backend`:

  ```python
  if choice == "xtb":
      if not xtb_cli.is_available():
          raise ValueError(
              f"CHEMCLAW_XTB_ENGINE=xtb but the {settings.xtb_binary!r} binary is not installed; "
              "a version string naming it would match no row in the calibration ledger"
          )
      return "xtb"
  ```

  and correct the docstring to say the string is unreachable *because of that check*, not because
  `resolve_backend` happens not to pick `xtb`. Behaviour-preserving on every correctly-configured
  deployment; turns one misconfiguration from silent into loud.

---

## The spec that a primitive runs is built twice — in `tools.py` and again in `identity.py`

- **Severity**: medium
- **Location**: clone sites, one pair per tool:
  - `tools.py:585` `OptSpec(solvent=solvent, frozen_atoms=tuple(frozen_atoms or ()))`
    ↔ `engine/identity.py:233` `OptSpec(solvent=_solvent(arguments), frozen_atoms=frozen)`
  - `tools.py:613` `XtbSpec(task="properties", solvent=solvent)`
    ↔ `engine/identity.py:246` `XtbSpec(task="properties", solvent=_solvent(arguments))`
  - `tools.py:641` `HessianSpec(solvent=solvent)` ↔ `engine/identity.py:260` `HessianSpec(solvent=…)`
  - `tools.py:743-748` `EnsembleSpec(search=…, effort=…, solvent=…, temperature_k=temperature_k or
    settings.xtb_thermo_temperature_k)` ↔ `engine/identity.py:281-288`, which spells the same default
    as `crest_search.EnsembleSpec().temperature_k`
  - `tools.py:785` `ComplexSpec(effort=effort, solvent=solvent)` ↔ `engine/identity.py:295-297`
- **Trigger**: any future edit to one side. The five SMILES-in calculations are immune by
  construction — `xtb.sp_inputs`, `xtb_props.properties_inputs`, `xtb_props.fukui_inputs`,
  `xtb_opt.optimization_inputs`, `scan.scan_point_inputs` each return the `(spec, structure)` pair and
  are called from both sides (`xtb.py:87` says so explicitly: *"Extracted so `run_xtb` and
  `identity.calculation_identity` read the same definition rather than two agreeing copies"*). The six
  above have no such helper and are two agreeing copies.
- **Consequence**: the failure this codebase names as the worst one available to it —
  `identity.py:307-314` — is a probe that returns the key of a *different* calculation, because the
  lookup then hits a real row holding someone else's answer. The parity test only closes part of the
  gap: `tests/test_calculation_key.py:131 test_the_primitives_key_up_front_too` exercises
  `relax_structure`, `compute_properties_at`, `compute_hessian` and `scan_point` **at their defaults
  only** — `relax_structure` with `frozen_atoms` is never parity-checked, and the two CREST tools
  cannot be checked at all because the binary is absent (`test_calculation_key.py:192` asserts they
  refuse). So a divergence in `frozen_atoms`, `effort`, `search` or `temperature_k` handling ships
  green.
- **Evidence**: I diffed all six pairs; they agree today, so this is latent rather than live. The
  asymmetry is the finding: the same module states the extraction rule for five tools and abandons it
  for six, and the tests that would have caught the difference are default-only.
- **Fix**: give each primitive the `*_inputs` helper the others have, next to its engine, and have
  both callers use it. E.g. in `xtb_opt.py`:

  ```python
  def relaxation_inputs(structure: Structure, solvent: str | None,
                        frozen_atoms: Sequence[int] | None) -> tuple[OptSpec, Structure]:
      return OptSpec(solvent=solvent, frozen_atoms=tuple(frozen_atoms or ())), structure
  ```

  and equivalents in `xtb_props`, `xtb_hessian`, `crest_search`. Behaviour-preserving; it deletes six
  duplicated constructions and makes `test_calculation_key`'s coverage gap harmless.

---

## `CHEMCLAW_CREST_EFFORT` cannot reach any served call

- **Severity**: medium
- **Location**: the setting `engine/config.py:120` (`crest_effort`); its only readers are the two
  `default_factory` lambdas at `engine/crest_search.py:71` and `:91`; both are shadowed by hardcoded
  literals at `tools.py:709` (`effort: crest_cli.CrestEffort = "quick"`), `tools.py:755` (same), and
  `engine/identity.py:283` / `:296` (`arguments.get("effort", "quick")`).
- **Trigger**: set `CHEMCLAW_CREST_EFFORT=extensive` and call `search_conformer_ensemble` or
  `search_binding_modes` without naming `effort`.
- **Consequence**: the operator gets a `quick` search, silently, and the payload reports
  `effort="quick"`. The config comment at `engine/config.py:117` — *"`crest_effort` is the default
  search depth"* — is false for every path a caller can take. Since the tool signature default is
  also what `calculation_key` assumes, the two agree on the wrong value, so nothing anywhere
  disagrees loudly. This is the repo's own named failure mode: a setting with no effective reader is
  configuration in appearance only (`config.py:20-23`).
- **Evidence**: `/tmp/calcaudit/crest_effort.py`, with `CHEMCLAW_CREST_EFFORT=extensive` set before
  import and `crest_cli` stubbed to look installed:

  ```
  settings.crest_effort      = extensive
  EnsembleSpec().effort      = extensive
  tool call passed effort    = quick
  payload effort             = quick
  ```

  `grep -rn crest_effort` over the repo returns exactly four lines: the two `default_factory`
  lambdas, the config comment, and the field itself. No other reader exists.
- **Fix**: use the same shape the server already uses for `top_n` (`tools.py:331`,
  `settings.xtb_fukui_top_n`) and `ph` (`tools.py:387`, `logd_default_ph`) — make the wire default a
  sentinel and let the spec's `default_factory` fire. Change both tool signatures to
  `effort: crest_cli.CrestEffort | None = None` and pass `effort` through only when it is not `None`
  (or spell it `effort=effort or settings.crest_effort`), and mirror the same in `identity.py`'s two
  derivations so the probe and the compute path keep agreeing. Behaviour-preserving on a default
  configuration; makes the knob live.

---

## `xtb_opt` builds the same 15-field result three times, and threads a parameter it does not use

- **Severity**: low
- **Location**: `servers/calc/src/chemclaw_mcp_calc/engine/xtb_opt.py:351-367`
  (`_optimize_with_library`), `:414-430` (`_optimize_with_binary`), `:458-474`
  (`_force_field_result`); the dead parameter is `template` at `:477` (`_energy_and_gradient`).
- **Trigger**: adding or renaming any `OptimizationResult` field, or changing how displacement is
  measured.
- **Consequence**: three constructions must be edited in lockstep; only five of the fifteen fields
  actually differ between them (`initial_energy_hartree`, `energy_hartree`, `relaxation_kcal`,
  `steps`, `max_gradient`, plus `frozen_atoms`). The displacement expression
  `float(np.sqrt(np.mean((final - positions) ** 2)))` together with its two setup lines
  (`_, positions = structure.arrays()`, `final = np.array(optimized.positions)`) is written out three
  times, verbatim in two of them. `_force_field_result` is a five-parameter helper with exactly one
  caller and no reuse potential.
- **Evidence**: `_energy_and_gradient(spec, template, at)` uses `template` only for
  `numbers, _ = template.arrays()` (`:486`), and `at` is a `Structure` carrying the same `elements`
  by construction — the binary path's `at` is either `structure` itself (`:403`) or `optimized`,
  which `_from_xyz` builds with `elements=template.elements` (`xtb_cli.py:283`). So
  `numbers, _ = at.arrays()` is identical, and the parameter can go. Both call sites also discard the
  third element of its return tuple (`initial, _, _` and `energy, gradient, _`).
- **Fix** (behaviour-preserving):
  1. One private builder,
     `_result(spec, structure, optimized, key, *, initial, energy, steps, max_gradient) -> OptimizationResult`,
     that computes `relaxation_kcal` and `displacement_rms_angstrom` once and reads `frozen_atoms`
     off `spec`. All three sites become a single call.
  2. Inline `_force_field_result` into `_optimize_with_binary`'s `if spec.method == "GFN-FF":` branch
     — after (1) it is four lines: the `outcome.cycles is None` refusal plus one `_result(...)` call.
  3. Drop `template` from `_energy_and_gradient` and narrow its return to `(energy, gradient)`.

---

## `tools.py` describes a nine-tool server that serves seventeen

- **Severity**: low
- **Location**: `servers/calc/src/chemclaw_mcp_calc/tools.py:1` ("nine request/response
  calculators"), `:57-59` ("asserts the hop for every one of the nine"), `:171` — inside
  `calculation_key`'s `Args`, which is shipped as the MCP tool description — ("one of the nine on
  this server"); `__all__` at `:115-128`. Same stale count at `engine/identity.py:24`, `:340`,
  `tests/test_event_loop_offload.py:5,12`, `tests/test_calculation_key.py:71,243`, `engine/key.py:118`.
- **Trigger**: read the served surface.
- **Consequence**: `calculation_key`'s own `tool` argument accepts **fourteen** names
  (`identity.COMPUTE_TOOLS`) while its documentation says nine, and the server lists seventeen tools.
  `test_event_loop_offload.py` claims to cover "every one of the nine" while its `CASES` table holds
  fifteen (and, to its credit, is closed against the real surface by
  `test_every_served_tool_is_covered`). `__all__` names ten of the seventeen tools — it gained
  `combine_structures` when the primitives were added and missed the other seven, so it now reads as
  a maintained list that is wrong rather than as an absent one.
- **Evidence**:

  ```
  17
  ['calculation_key', 'combine_structures', 'compute_electronic_properties', 'compute_hessian',
   'compute_properties_at', 'compute_xtb_energy', 'embed_structure', 'optimize_geometry',
   'predict_developability_profile', 'predict_logd', 'predict_pka', 'predict_site_reactivity',
   'predict_solubility', 'relax_structure', 'scan_point', 'search_binding_modes',
   'search_conformer_ensemble']
  __all__ tools missing: ['compute_hessian', 'compute_properties_at', 'embed_structure',
                          'relax_structure', 'scan_point', 'search_binding_modes',
                          'search_conformer_ensemble']
  __all__ non-tools: ['resolve_calculator_versions', 'server']
  ```
- **Fix**: replace every fixed count with the structural statement the code actually guarantees —
  "the SMILES-in calculators", "the structure-in primitives" — since three separate tests already
  assert the sets against `server.list_tools()` and a number is the one thing that cannot be
  enforced. For `__all__`: either complete it or delete it; nothing in the repo does
  `from ... import *`, and `app.py` imports `resolve_calculator_versions` and `server` by name, so
  deleting it costs nothing and removes a list that will go stale again.

---

## Checked and found sound

- The `asyncio.to_thread` hop is on **all** seventeen tools, and
  `test_event_loop_offload.py::test_every_served_tool_is_covered` closes the case list against
  `server.list_tools()` rather than a hand-kept list — the docstring's count is stale but the
  mechanism is not.
- `pka.relaxation_spec()` genuinely is the single construction point shared by the compute path and
  `pka_cache_key`, which is what the two-callers rule asks for.
- The pKa key's inclusion of the optimiser's parameters on the *acid* branch (which never calls
  `optimize_structure`) is over-broad — `/tmp/calcaudit/pka_key.py tol` shows acetic acid's pKa
  unchanged at `6.512636701266935` while `params_hash` moves from `3dff6345a84597a1` to
  `b5ff10ca109d525e` under `CHEMCLAW_XTB_OPT_GRADIENT_TOLERANCE=2e-2` — but `calc_version`'s
  docstring argues the case explicitly (the branch is chosen by the molecule *after* the version is
  built, and a version that re-derives the dispatch can disagree with it). Extra cache misses, no
  ledger loss, and the argument holds. Recording it, not filing it.
- `xtb_cli.run_isolated`, `_REQUIRED_OUTPUTS`/`_produced_everything`, and the `_safe` argv check are
  each single-purpose and each earn their place; `CliError` being a `RuntimeError` rather than a
  `ValueError` is a deliberate, correct split that avoids exactly the string-matching this lens looks
  for.
