# xTB capability layer — X3 (geometries + thermochemistry) and X4 (the composite)

Proposal: `docs/xtb-tools-proposal.md` §12. Branch: `claude/xtb-chemclaw-tools-proposal-nujp14`.

Scope of *this* change: **X3** — `optimize_geometry`, `compute_thermochemistry`, `scan_coordinate`
— and **X4** — `compute_reaction_energy`, `compare_solvent_effects`. Together these are the phases
the skill catalogue says gate 19 of its 28 skills.

## Design decisions taken during planning (deviations from the proposal, with reasons)

1. **No `ase` dependency.** The proposal offered "`ase` (or a scipy L-BFGS over the tblite
   gradient)". Taking the second: `scipy` is already resident (via `scikit-learn`/`bofire`) and
   `scipy.optimize.minimize(method="L-BFGS-B", jac=True)` over tblite's *analytic* gradient is a
   dozen lines. ASE would buy an optimizer we get for free, plus a `Vibrations` class that caches
   displacements **to a directory on disk** — a side effect that does not belong inside a pure,
   content-addressed calculator. Its thermochemistry helper is the only real loss, and RRHO is
   ~80 lines of textbook physics we can pin against water's measured entropy. `scipy` is promoted
   from transitive to declared, because a first-party module now imports it.
2. **Spec *subclasses*, not one widening `XtbSpec`.** Thermochemistry has a temperature, a symmetry
   number and a pressure; optimization has a gradient tolerance and a step cap; a scan has its
   coordinate. Adding them all to `XtbSpec` would put a `temperature_k` in a *single point's* cache
   key. `OptSpec`/`ThermoSpec`/`ScanSpec` inherit `cache_key` unchanged — it derives from
   `model_dump()`, so a subclass field is keyed by construction exactly as a base field is.
3. **The optimized structure is a field of the cached result, not a new store.** X1 deferred a
   structure store until something produced a geometry; X3 does. But `OptimizationResult` carrying
   its `Structure` *is* persistence — the result store already holds it, content-addressed by the
   optimization's key. A second store with one writer would be the speculative abstraction.
4. **`compute_thermochemistry` also returns IR intensities.** The Hessian loop displaces every
   Cartesian and reads the gradient; tblite hands back the **dipole** at the same time, so dipole
   derivatives — and therefore a computed IR spectrum — cost nothing beyond an array we were
   already discarding. This is the same "read what the SCF already produced" move as X2, and it is
   what makes the catalogue's `computed-spectra-comparison` shippable.
5. **`level="thorough"` is not offered.** The proposal's third tier is a conformer ensemble, which
   is X6. A `Literal["quick", "standard"]` that refuses to name what it cannot do beats an option
   that raises.
6. ~~**A size guard instead of half of X5.**~~ **Reversed during the build.** The original plan
   was to refuse anything too slow for an inline turn, on the grounds that durable routing is
   explicitly X5. The measurements said otherwise — 4.6 s for a four-species reaction, ~25 s for a
   five-solvent screen, minutes for a long scan — and refusing work because it is slow is a worse
   answer than running it durably. The expensive tools now route by predicted cost
   (`calc/xtb_cost.py`) onto `XtbJobWorkflow`. The atom and point caps that remain are
   practicality limits, not latency ones.
7. **Relaxed scans freeze the atoms that define the coordinate.** RDKit's `rdMolTransforms` sets a
   bond/angle/dihedral by moving the whole attached fragment; freezing those atoms and relaxing
   everything else is then exactly a constrained minimization over the free subspace, expressed as
   equal L-BFGS-B bounds. The approximation (the frozen atoms' own local geometry cannot relax) is
   stated in the result and in the skill.

## Build

- [x] X3.1 `calc/xtb_engine.py`: `make_calculator` + `evaluate_point` (Angstrom in; Hartree,
      Hartree/Angstrom and the dipole out); friendly failure for an unknown ALPB solvent; the
      spin-polarization contribution for open shells, versioned into the cache key.
- [x] X3.5 Durable routing (unplanned, see decision 6): `calc/xtb_cost.py`, `XtbJobWorkflow` +
      activity, `agents/xtb_job_tools.py`, and `get_qm_job_status` generalized to `get_job_status`.
- [x] X3.2 `calc/xtb_opt.py`: `OptSpec`, `OptimizationResult`, `optimize_structure`,
      `run_cached_optimization`. Frozen-atom support (bounds), convergence on max |gradient|.
- [x] X3.3 `calc/xtb_thermo.py`: finite-difference Hessian + dipole derivatives, Eckart projection,
      harmonic frequencies, IR intensities, quasi-RRHO thermochemistry, `ThermochemistryResult`.
- [x] X3.4 `calc/xtb_scan.py`: `ScanSpec`, relaxed scan over a distance/angle/dihedral.
- [x] X4.1 `calc/reaction.py`: balance check, per-species pipeline, `compute_reaction_energy`.
- [x] X4.2 `calc/reaction.py`: `compare_solvent_effects` over the same reaction machinery.
- [x] X4.3 Agent tools + config + `.env.example`.
- [x] X3/X4 skills: the catalogue entries these unblock.
- [x] Docs: ADR, `BACKLOG.md`, catalogue status.

## Raised by the user mid-build, and done

- [x] **A structured way to register Temporal capabilities** (D-086). Adding `XtbJobWorkflow`
      meant editing a hardcoded list in a worker — the one extension seam left that forced an
      edit to infrastructure code, and a silent one (an unregistered workflow never runs and
      nothing fails until a job waits forever). `workflows/registry.py` now mirrors
      `agents.tool_registry`: `@durable_workflow("hpc")` / `@durable_activity("background")` at
      the definition site, workers read what they serve.
- [x] **Sized for the real workload: 200-800 Da, minutes not seconds** (D-087). The cost model
      was fitted on 3-14 atom test molecules and under-predicted a 76-atom substrate
      **sevenfold**. Refitted on measured drug-sized timings (exponent 1.7 -> 3.0; the 76-atom
      point now reproduces to 1%). Atom ceiling 120 -> 150, optimizer step cap 400 -> 1500, job
      budget 1 h -> 4 h, and the activity heartbeats between species/solvents/scan points so a
      dead worker is caught in minutes rather than at the timeout.
- [ ] **xTB as an MCP server** — answered, not built. Recorded as X8 in `BACKLOG.md` with the
      reason it is an either/or switch rather than an addition.

## Verification (planned before building)

- **Optimization**: ethanol's energy drops and the gradient falls below tolerance; a deliberately
  stretched bond returns to a normal C–O length; optimizing an already-optimized structure is a
  no-op (idempotence, which is also what makes the cache key honest).
- **Frequencies**: water gives 3 real modes, no imaginary; a *distorted* (unoptimized) geometry
  gives at least one imaginary — the `is_minimum=False` case the proposal says must exist.
- **Thermochemistry against measurement**: water's standard entropy at 298.15 K, σ=2, is
  45.10 cal/mol/K. Anything that fails to reproduce it within ~2 units has the physics wrong.
  ZPE against the measured 13.26 kcal/mol.
- **IR**: water's bend is the strongest of its three fundamentals (measured 53.6 km/mol vs. 2.2 and
  44.6) — an ordering, which is what a semiempirical intensity supports.
- **Reaction**: the Fischer esterification of `evals/cases/green-esterification.md` returns
  ΔE/ΔH/ΔG; an unbalanced equation is rejected; a second reaction sharing a species demonstrably
  hits the cache (assert hits, not wall clock).
- **Torsion**: n-butane's C–C–C–C profile has minima at ~180° (anti) and ~±60° (gauche), anti
  lowest, with a barrier of the right order at 0°.

## Review

**Built, and green under `make lint type test` + `make skill-validate`.** Five new calculator
modules, five new agent tools, a durable job path, six new skills and five updated ones.

**Three defects the measurements found, none of which a design review would have.** Open-shell
energies had no spin-polarization term, so triplet O2 came out *above* singlet — a qualitative
inversion that would have made every radical number wrong. The optimizer's first step could
collapse a bond and leave the SCF unconvergeable. And ordinary molecules — ethyl acetate —
optimize onto rotor saddle points, where a "free energy" is not one. Each is recorded in D-085
with the number that exposed it, and each is pinned by a test that fails if it returns.

**One scope decision reversed mid-build, correctly.** X3/X4 first shipped with an atom cap and a
point cap: refusing calculations that would block a turn. The user pushed back that these are
longer-running jobs and belong in Temporal, and the timings agreed — 4.6 s for a reaction, ~25 s
for a solvent screen, minutes for a long scan. Refusing work because it is slow is a worse answer
than running it durably. The caps that remain are practicality limits, not latency ones.

**What is still missing, stated plainly:** no transition-state search, so no barriers and no
rates; one conformer everywhere, so no ensembles; and homolysis energies that rank correctly
while being badly wrong in absolute terms. The first two are X5/X6; the third is carried by
`bond-strength-and-radicals`.
