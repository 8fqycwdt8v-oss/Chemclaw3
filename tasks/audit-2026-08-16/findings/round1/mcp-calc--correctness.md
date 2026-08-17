# mcp-calc — correctness (round 1)

Repo: `/workspace/chemclaw3-mcp`. Slice: `servers/calc/` — `tools.py`, `engine/pka.py`,
`engine/xtb_opt.py`, `engine/xtb_cli.py`, and the server's tests.

Everything below was run. Environment: `uv sync` in the repo, no `xtb` binary
(`xtb_cli.binary_path() is None`, `binary_version() == "absent"`), so the in-process `tblite`
backend is the one that ran — which is the shipped image's configuration.

What I checked and found **clean**, so it is not padded into findings below:

- Key derivation agreement. I ran `calculation_identity(tool, args)` against the compute tool's own
  result for all ten keyable tools reachable without `crest`
  (`compute_xtb_energy`, `compute_electronic_properties`, `optimize_geometry`, `predict_pka`,
  `predict_solubility`, `predict_developability_profile`, `relax_structure`,
  `compute_properties_at`, `compute_hessian`, `predict_site_reactivity`): **`calc_key` and
  `calc_version` matched on every one.** That is the seam most likely to silently address the wrong
  cache row, and it holds.
- `xtb_opt._optimize_with_library`'s idempotency claim ("a structure that is already a minimum runs
  no leg at all and comes back byte-identical"). Measured on water: first pass `steps=2`, second
  pass `steps=0`, positions byte-identical, `structure_id` unchanged. True as written.
- The frozen-atom mask, the ANC chain rule (`scale * (V^T g)`), the Hartree/Bohr² → Hartree/Ångström²
  conversion in `_read_hessian`, the `_energy_from_log` / `_cycles` / `_read_vibspectrum` parsers
  against realistic xtb output, the `--opt`-exited-non-zero-but-wrote-files downgrade (re-verified
  in-process afterwards, so the convergence guarantee survives it), and the pKa acid/base branch on
  seven molecules (acetic acid 6.51 vs 4.76, phenol 11.22 vs 9.99, pyridine 5.40 vs 5.23, aniline
  4.23 vs 4.60 — all inside the stated uncertainties; methylamine, acetamide and pyrrole correctly
  refused).

---

## `predict_site_reactivity` truncates the ranking here, so the payload the caller re-ranks is missing atoms

- **Severity**: high
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/tools.py:329-334`
  (`predict_site_reactivity._run`), lines 331-332
- **Trigger**: any molecule with more than `settings.xtb_fukui_top_n` (default 15) atoms, called
  with the tool's defaults. Reproduced with ibuprofen,
  `CC(C)Cc1ccc(cc1)C(C)C(=O)O` (33 atoms):
  `await tools.predict_site_reactivity(smiles=...)` — no `mode`, no `top_n`.
- **Consequence**: the server sorts all 33 sites by `f_minus` (the default electrophilic mode) and
  then returns only the top 15. The 18 dropped atoms are gone from the payload, so the
  `f_plus`/`f_zero` columns the payload still carries describe an **incomplete set**. The tool's own
  docstring instructs the reader to use exactly that: *"The three single points do not depend on
  `mode` — it only chooses the sort — so every result carries all three indices per atom. Read the
  other rankings off `f_minus`/`f_plus`/`f_zero` rather than calling again for a second mode"*
  (tools.py:313-316). Following that instruction on ibuprofen silently loses the **carboxyl carbon**
  — true rank #4 by `f_plus`, and the archetypal nucleophilic-addition site the same docstring names
  ("addition to a carbonyl"). The consumer reports a top-5 nucleophilic ranking that is wrong at
  position 4 with nothing raising anywhere.

  This is not hypothetical downstream. The core repo's caller
  (`/home/user/Chemclaw3/src/chemclaw/connectors/calc/server/tools.py:790-797`) deliberately sends
  **only `{"smiles": smiles}`** — neither `mode` nor `top_n` — with the comment *"the row holds
  every atom, so asking for more sites re-slices a cached result instead of running three more
  single points"*. The row does **not** hold every atom: it holds this server's electrophilic
  top-15. So on the core side (a) `SiteReactivityResult.ranked_for("nucleophilic")` re-ranks a
  pre-truncated list, and (b) `top_n=100` — documented there as "pass a larger number to see the
  whole molecule" — re-slices a 15-element list and still returns 15 while `total_atoms` says 33.
  The cache makes it permanent: the truncated payload is what `cached_compute` persists.
- **Evidence**: `/tmp/probe/p7.py`, output verbatim —

  ```
  total atoms: 33  served: 15
  TRUE nucleophilic top 5:
    #1 idx=11 O f_plus=0.1393
    #2 idx=12 O f_plus=0.0694
    #3 idx=4  C f_plus=0.0678
    #4 idx=10 C f_plus=0.0674   <-- ABSENT from served payload
    #5 idx=32 H f_plus=0.0621
  What a caller re-sorting the SERVED payload by f_plus would report as top 5:
    #1 idx=11 O f_plus=0.1393
    #2 idx=12 O f_plus=0.0694
    #3 idx=4  C f_plus=0.0678
    #4 idx=32 H f_plus=0.0621
    #5 idx=24 H f_plus=0.0618
  ```

  Atom 10 confirmed as the carboxyl carbon (`/tmp/probe/p8.py`):
  `10 C nbrs: [(8,'C','SINGLE'), (11,'O','DOUBLE'), (12,'O','SINGLE')]`.

  The offending code:

  ```python
  result = xtb_props.compute_fukui(*xtb_props.fukui_inputs(smiles), mode)
  limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
  return result.model_copy(update={"sites": result.sites[:limit]})
  ```

  Note this repo already states the rule that forbids it, one file over, for the same shape of
  field — `EnsembleSpec` in `engine/crest_search.py`: *"`max_members` is not here, and its absence
  is the point… Truncation is a presentation choice made by whoever reads the result, and the reader
  is on the other side of this seam — so the field does not exist here at all."* `top_n` is that
  field, kept.
- **Fix**: drop the truncation from this server. `predict_site_reactivity` returns the full ranked
  list (`total_atoms == len(sites)` always); the `top_n` parameter goes with it, since the caller
  that owns presentation already re-slices (`tools.py:797` in the core repo). If the parameter must
  stay for wire compatibility, it must at minimum be sent through `calculation_key`'s `accepts` as a
  *keyed* argument so a 15-site row and a 33-site row cannot share a cache key — but the right fix
  is deleting it, because the row that gets cached should be the complete one.

---

## A tool call abandoned by its client keeps its worker thread, and the "cheap, no SCF" probe queues behind it

- **Severity**: medium
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/tools.py` — every
  `asyncio.to_thread(...)` body (lines 151, 185, 206, 229, 259, 291, 334, 365, 383, 424, 518, 553,
  586, 612, 668, 703, 749, 786); the claim under test is the module docstring, lines 54-59.
- **Trigger**: N concurrent long tool calls, where N ≥ `min(32, os.cpu_count() + 4)` — 8 in this
  container. `compute_hessian` is documented as "minutes on a drug-sized molecule" and
  `search_conformer_ensemble` as "minutes to hours". A client that times out and retries makes it
  worse rather than better: cancelling the awaiting coroutine does not stop the worker.
- **Consequence**: two measured effects. (1) `asyncio.to_thread` is uncancellable once the work has
  started — the coroutine raises `CancelledError` immediately, the thread runs to completion holding
  its executor slot, and the result is discarded. A disconnected or timed-out client therefore
  *burns* a slot for the full duration of a Hessian and gets nothing. (2) The default
  `ThreadPoolExecutor` is shared by every tool, so once it is saturated the `calculation_key` probe
  — advertised at tools.py:160 as *"Cheap; no SCF"* and as the thing to call *before* paying for a
  compute — waits behind the computes it exists to avoid. The Containerfile runs a single uvicorn
  worker (`servers/calc/Containerfile:69`) and nothing in `mcp_server_kit` or `chemclaw_mcp_calc`
  sets an executor or a limiter (grepped: no `set_default_executor`, no `max_workers`, no
  `Semaphore`), so the 8-slot pool is the whole pod's capacity. The event loop stays responsive,
  which is what the docstring claims; tool *execution* does not, which is what the docstring implies.
- **Evidence**: `/tmp/probe/p9.py` —

  ```
  cpu_count: 4 default max_workers: 8
  cheap call waited 2.70s behind 8 saturating calls
  ```

  (the 8 saturating calls slept 3.0 s each; substitute a 150-atom Hessian and the wait is minutes.)

  `/tmp/probe/p10.py` —

  ```
  CancelledError raised after 0.00s; worker finished? False
  worker still running in background: True
  worker ran to completion anyway: True
  ```
- **Fix**: bound and account for the concurrency explicitly rather than inheriting the interpreter
  default. Install a dedicated, sized `ThreadPoolExecutor` for the compute tools
  (`loop.set_default_executor` is the blunt version; a per-server executor passed to
  `loop.run_in_executor` is the honest one) and keep the cheap paths — `calculation_key`,
  `embed_structure`, `combine_structures` — off it so a lookup probe can never queue behind an SCF.
  Add an admission limit (a semaphore sized to the executor) so an over-subscribed server refuses
  with a retryable error instead of silently queueing, and document that a cancelled call does not
  free capacity.

---

## `_from_xyz`'s "element mismatch is a loud validation failure" is not implemented

- **Severity**: low
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/engine/xtb_cli.py:274-290`
  (`_from_xyz`), docstring lines 277-279
- **Trigger**: feed `_from_xyz` an XYZ whose element column disagrees with the template. Only
  reachable on a deployment that ships the `xtb` binary (the default image does not), via
  `_optimize_with_binary` reading `xtbopt.xyz`.
- **Consequence**: the docstring asserts *"trusting the template makes an element mismatch a loud
  validation failure instead of a silently different molecule."* Nothing in the function reads the
  symbol column at all — `row.split()[1:4]` takes coordinates and discards `row.split()[0]`. A file
  whose atom order or composition changed is accepted silently and re-labelled with the template's
  elements. The guarantee a reader would rely on when adding a second binary-backed task (or
  swapping in a different `xtb` build) does not exist. Only the *count* is checked, and only
  indirectly, by `Structure`'s length validator.
- **Evidence**: `/tmp/probe/p1.py` —

  ```
  from_xyz with WRONG element symbols in file -> [8, 1, 1] [[0.0,0.0,0.0],[1.0,0.0,0.0],[0.0,1.0,0.0]] st_c9d47957f91b8c2f
  SHORT raised: ValidationError ... 2 positions for 3 elements
  ```

  The template was O/H/H; the file said C/N/S; the result is O/H/H at the file's coordinates, with a
  content address that names a molecule the file did not contain.
- **Fix**: parse the symbol column and compare it against `template.symbols`, raising `CliError`
  naming the first differing index. Two lines, and it makes the docstring true.

---

## GFN-FF optimizations report `relaxation_kcal = 0.0` and an initial energy equal to the final one

- **Severity**: low
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/engine/xtb_opt.py:458-474`
  (`_force_field_result`), fields at lines 467-469
- **Trigger**: `CHEMCLAW_XTB_METHOD=GFN-FF` on a deployment carrying the `xtb` binary, then any
  `relax_structure` / `optimize_geometry` call. Not reachable in the shipped image (no binary,
  default method GFN2-xTB), which is why this is low rather than higher.
- **Consequence**: `initial_energy_hartree` is set to the *final* energy and `relaxation_kcal` to
  `0.0`. Both are values a reader can act on, and both are false: `OptimizationResult`'s own field
  comment (line 102-103) says *"How much the relaxation was worth… A large value on a supposedly
  relaxed input means the starting geometry was misleading"*, so `0.0` reads as "this geometry was
  already at a minimum" for a relaxation that may have moved it a long way. `OptimizationSummary`
  carries the same `0.0` up to whatever reads the summary. The docstring calls this "reported as 0.0
  rather than invented", which inverts the meaning of the word — 0.0 *is* the invented value; the
  uninvented one is absent.
- **Evidence**: the code, which is unambiguous:

  ```python
  initial_energy_hartree=outcome.energy_hartree,
  energy_hartree=outcome.energy_hartree,
  relaxation_kcal=0.0,
  ```

  with `max_gradient=None` on the next line showing that this model already expresses "not measured"
  as `None` for the field where it was thought through.
- **Fix**: make `initial_energy_hartree` and `relaxation_kcal` `float | None` and pass `None` on
  this path, exactly as `max_gradient` already does; or spend the one extra single point the
  docstring declines and report the real numbers.

---

## `_lone_pair_is_available`'s stated reason for exempting aniline is factually wrong

- **Severity**: low
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/engine/pka.py:188-190`
  (docstring of `_lone_pair_is_available`)
- **Trigger**: read the docstring, then run RDKit on aniline.
- **Consequence**: the docstring says *"Only a **single** bond from the nitrogen counts for the
  amide rule, which is what keeps aniline out of it: aniline's bond to the ring is aromatic, not the
  C=O single bond this looks for."* Aniline's exocyclic N–C bond is `SINGLE` in RDKit — the aromatic
  bonds are the ring's, not this one — so the loop at pka.py:203-214 *does* enter for aniline. What
  actually keeps aniline available is the inner test: the ring carbon has no `DOUBLE` bond to O/S.
  The behaviour is right; the stated mechanism is not, and the mechanism is what the next person
  extending the exclusion rules will reason from (e.g. concluding, wrongly, that an aryl-attached
  nitrogen can never be caught by the amide branch — acetanilide's N is, and correctly so).
- **Evidence**: `/tmp/probe/p11.py` —

  ```
  aniline N bonds: [('SINGLE','C'), ('SINGLE','H'), ('SINGLE','H')]
  available: True  basic Ns: [0]
  ring carbon aromatic? True  N-C bond type: SINGLE
  ```
- **Fix**: replace the sentence with the true one — the amide branch is entered for aniline and
  exits because the aromatic ring carbon carries no double bond to a chalcogen.

---

## A misconfigured `pka_solvent` fails startup resolution silently instead of being logged

- **Severity**: low
- **Location**: `/workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/tools.py:150-155`
  (`resolve_calculator_versions`), the `except` clause at line 152
- **Trigger**: `CHEMCLAW_PKA_SOLVENT` set to a solvent ALPB has no parameters for — e.g. `2-MeTHF`,
  which `xtb_spec.py` itself names as "among the most common process solvents there is and not one
  GFN2-xTB has parameters for". Then start the server.
- **Consequence**: `pka_calc_version()` → `relaxation_spec()` → `OptSpec(solvent=...)` raises
  pydantic `ValidationError` (a `ValueError`). The handler catches only `OSError` and
  `subprocess.SubprocessError`, so the exception escapes into the coroutine
  `mcp_server_kit.app.connector_app` starts with `asyncio.create_task(on_start())` and never awaits
  (`packages/mcp_server_kit/src/mcp_server_kit/app.py:186`). The task reference is held until
  shutdown and then cancelled, so the exception is never retrieved and nothing is logged while the
  process lives. The server reports healthy; the operator's first signal is every `predict_pka`,
  `predict_logd` and `calculation_key('predict_pka', …)` call failing at request time. The
  function's docstring claims it "Swallows its own failures, as the `on_start` contract requires" —
  it swallows them harder than that: the `logger.warning` it exists to emit never runs.
- **Evidence**: `/tmp/probe/p12.py` sets `settings.pka_solvent = "2-MeTHF"` and awaits
  `tools.resolve_calculator_versions()` directly; the traceback escapes the handler at
  `tools.py:151` with
  `pydantic_core._pydantic_core.ValidationError: 1 validation error for OptSpec / solvent /
  Value error, GFN2-xTB's ALPB solvation model has no parameters for '2-MeTHF'`.
- **Fix**: catch `Exception` in `resolve_calculator_versions` and log it — the whole point of the
  handler is that startup must not fail, so the narrow tuple buys nothing and costs the log line.
  (Separately: `connector_app` should add a done-callback that logs a failed `on_start` task, so no
  server's hook can fail invisibly.)
