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


## X5-X7 (added after "continue with all remaining x")

- [x] **X5 the `xtb` binary** — `calc/xtb_cli.py`. The measurement that justified it: 8.3x on a
      76-atom substrate, 10.9x on 118 atoms, because ANCopt optimizes in normal coordinates
      (39 and 94 cycles against 177 and 232 Cartesian steps).
- [x] **X6 CREST** — `calc/crest_cli.py` + `calc/conformers.py`, conformer/tautomer/protomer
      searches, degeneracy-weighted populations, conformational entropy, `level="thorough"`.
- [x] **X7 the expert seam** — `run_xtb_task` over a typed spec, role-gated.
- [x] Both binaries pinned into the container image; every new setting in `.env.example`.
- [x] Skills: `tautomer-analysis`; `conformational-analysis` extended for ensembles;
      `docs/xtb-skill-catalogue.md` §9 ideates the seven further skills CREST's searches unlock.
- [x] ~~X9~~ retired: ANCopt *is* the internal-coordinate optimizer.
- [ ] **X8 (MCP)** — answered, not built. It is an either/or migration of the agent's advertised
      surface, not an addition, and it touches skill frontmatter, the registry test and the
      in-process `bo/` callers. Scoped in `BACKLOG.md`.
- [ ] **X10 transition states** — the largest remaining gap at the model level; unchanged by X5-X7.

### What the binaries changed about the earlier phases

Two X3/X4 decisions are now obsolete and were removed rather than left as dead weight: the
hand-written internal-coordinate optimizer (X9) is unnecessary, and the Cartesian trust-region
loop is demoted to the fallback path. Two are unchanged and were re-validated across backends:
the shared RRHO (both reproduce water's 45.10 cal/(mol K)) and the cost router (still the right
answer — with the binary, drug-sized work is minutes instead of tens of minutes, which is *still*
past any inline budget).

## X8 — the calculation capability as an MCP server

Goal (the user's, stated directly): run the calculators in **their own pod**, so the heavy
chemistry dependencies and the CPU load scale independently of the agent.

### The boundary this forces, discovered before writing any code

Not every calculator tool can move, and the reason is identity rather than chemistry:

- **`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`** route
  to Temporal above a cost threshold, and submitting a durable job needs `require_actor()` and
  `get_current_session_id()` — both **turn-ambient** and, by the F4-T3 rule, never model-supplied.
  An MCP server has neither: it is a separate process with no conversation and no authenticated
  user. Passing them as tool arguments would make identity a model-authored value, which is
  exactly the thing that rule exists to prevent.
- **`run_xtb_task`** is role-gated through `authorize_trigger` for the same reason.

So: **MCP carries capability; identity stays with the agent.** That is the line, and it also
predicts what can ever move.

### Build

- [x] `mcp_servers/calc/server.py` — FastMCP over the synchronous calculators, thin like
      `molfp`/`rxnfp`: every tool body already lives in `calc/`.
- [x] Move (not copy) those tools out of `agents/calc_tools.py`. Two advertisements of one tool
      is the failure mode to avoid.
- [x] `settings.mcp_servers` gains `mcp-calc`; `deploy/entrypoint.sh` gains the component.
- [x] **`scripts/validate_skills.py` must resolve a declared tool against MCP `allowed_tools` too.**
      Today it checks the in-process registry only, so every skill declaring a moved tool would
      fail. Fixing that is not a workaround — a skill names a *capability*, and which transport
      delivers it is a deployment decision the skill should be insulated from.
- [x] Tests: the transport test already parametrizes over configured stdio servers, so the new
      server is covered on adding it; plus the registry set, and the validator's new resolution.

### X8 review

Green. The measure of whether the boundary was drawn in the right place: **no skill changed** in
a migration that moved seven tools out of process, and `test_mcp_transport` needed no edit —
it already parametrizes over configured servers and proved the new one spawns and advertises
exactly its allowed set.

The one non-mechanical change was the validator, and it was a correction rather than an
accommodation: a skill declaring `predict_pka` is declaring a capability, and it should not care
which process answers. Widening the lookup without weakening it (an invented name still fails) is
what makes the transport a deployment decision.

## X11 — two molecules together, and the amine question the measurement re-scoped

Goal (the user's, stated directly): "leave X10 to backlog. However implement the fix for basic
amine and NCI, make it fully operational." Both were the two halves of the X11 backlog entry.

### Build

- [x] `calc/complexes.py` — `ComplexSpec`/`InteractionResult`/`compute_interaction`, over CREST
      `--nci` plus three optimizations. Interaction energy as a difference of **relaxed** species,
      so the deformation cost of binding is included rather than defined away.
- [x] `calc/pka.py` extended for bases: protomer enumeration in RDKit, most stable cation defines
      the conjugate acid, separate calibration, `site: "acid" | "base"` on the result.
- [x] `compute_interaction_energy` in `agents/calc_tools.py`, with the same cost routing as the
      other minute-scale tools (D-087) — it defers to Temporal above the inline budget.
- [x] `ComplexJobSpec` through `workflows/models.py` + `xtb_activities.py`, so it is durable.
- [x] `skills/molecular-association/SKILL.md`; `ionization-and-partitioning` rewritten around the
      measured two-class result; `calculation-selection` and `degradation-liabilities` corrected.
- [x] Tests: `tests/test_complexes.py` (CCSD(T)/CBS references, pair ordering, cache) and the base
      half of `tests/test_pka.py` (in-sample, held-out, the refusal, acid precedence).

### What the measurement changed about the plan

The plan said `--protonate`/`--deprotonate` was how U2 (basic amines) gets solved. It was not.
Fitting 20 experimental amines split the class in two, and the split is electronic rather than
structural: **aromatic and aryl nitrogen calibrates to Spearman 1.000** (RMSE 0.17 — better than
this system's acid calibration), while **aliphatic amines rank at -0.17**, which is no ranking
ability at all. A protomer *search* would not have moved that, because the failure is solvation:
gas-phase GFN2 gets the proton affinity order exactly right, ALPB reverses it, and the truth is
non-monotonic. So half the goal shipped and half is a refusal with a diagnosis (D-091), and the
CREST structural route was left unbuilt rather than built because the plan named it.

Two things the build itself taught, both caught by tests rather than by review:

- **Geometry policy is not free for bases.** MMFF geometries give ρ 0.893, GFN2-optimized ones
  1.000. Protonation pyramidalizes a nitrogen; relaxing it is doing real work, not polishing. The
  acid path keeps its own validated policy — refitting it is a separate decision.
- **`_combine` is not symmetric**, so A-with-B and B-with-A keyed to different cache entries and
  ran the same minutes-long search twice. `_ordered` canonicalizes the pair at the entry points;
  both the asymmetry and the invariant it forces are pinned by tests.

### X11 review

Green. The honest summary of the result is that the interesting half is the part that does *not*
ship: refusing aliphatic amines is worth more than a number would have been, because ρ = -0.17
carries no information while looking exactly like a value that does. The skill says so in the
same terms, so the agent declines rather than reaching for a substitute.
