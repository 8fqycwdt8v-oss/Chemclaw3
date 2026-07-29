# D-098 — X3/X4: geometries, free energies, the reaction composite, and durable routing

**Context.** X1/X2 gave the xTB layer its seams and the properties a single point already
produces. Everything above that — "what does it look like", "what is ΔG", "does this reaction
go" — needed a geometry optimizer and a Hessian. The skill catalogue (D-097) had measured the
gap precisely: **19 of its 28 skills were gated on X3 or X4**.

**Decision.** Build both phases: `calc/xtb_opt.py` (L-BFGS-B over tblite's analytic gradient),
`calc/xtb_thermo.py` (finite-difference Hessian, quasi-RRHO thermochemistry, IR intensities),
`calc/xtb_scan.py` (relaxed scans), `calc/reaction.py` (balanced reaction energies and solvent
comparisons), five agent tools, and — see below — a durable execution path for the expensive
ones.

**No `ase`.** The proposal offered "`ase` (or a scipy L-BFGS over the tblite gradient)". Taking
the second: `scipy` was already resident and `minimize(method="L-BFGS-B", jac=True)` over an
*analytic* gradient is a dozen lines. ASE's `Vibrations` caches displacements **to a directory
on disk**, a side effect that does not belong inside a content-addressed calculator. `scipy` is
promoted from transitive to declared, because first-party modules now import it.

**Spec subclasses, not one widening `XtbSpec`.** `OptSpec`/`ThermoSpec`/`ScanSpec` inherit
`cache_key` unchanged — it derives from `model_dump()`, so a subclass field is keyed by
construction. Adding `temperature_k` to the base model would have put a temperature in a
*single point's* cache key.

**The optimized structure is a field of the cached result.** X1 deferred a structure store until
something produced a geometry; X3 does. It turned out to need one field, not a subsystem: the
result store already persists it, content-addressed by the optimization's own key, and `origin`
records the lineage.

**IR intensities came free.** The Hessian loop displaces every Cartesian and reads the gradient;
tblite returns the dipole from the same SCF, so dipole derivatives — and therefore a computable
IR spectrum — cost one array that was being discarded. Same move X2 made for charges and bond
orders. This is what makes `computed-spectra-comparison` shippable.

### Three defects the measurements found

**1. Open-shell energies were silently wrong.** tblite's `uhf` only sets the *occupation*; with
no spin-dependent term the energy expression does not stabilize an open shell at all. Triplet O2
came out **1.7 kcal/mol above** singlet O2 — the ground state, inverted. Adding the
spin-polarization contribution wherever there are unpaired electrons puts the triplet 15.8
kcal/mol below (experimental gap ~22) and cuts ethane's C–C dissociation error from +42 to +25
kcal/mol. Measured that this leaves the validated X2 Fukui orderings (phenol, toluene,
nitrobenzene) unchanged, so it applies uniformly rather than as a special case. Cache impact is
handled by a new `_HAMILTONIAN_REVISION` tag in `engine_version()`: a change to *how* a
calculation is set up is otherwise invisible to the key.

**2. The optimizer's first step could destroy the molecule.** L-BFGS-B scales its opening trial
step by 1/|gradient|, which on a strained geometry is wildly too large — measured on a water
with a 1.6 Å O–H, its first move collapsed the bond to **0.20 Å** and the SCF then failed to
converge at all. Fixed with a trust radius enforced through bounds, re-entered per leg.

**3. Ordinary molecules optimize onto saddle points.** A force field hands over an eclipsed
methyl and a Cartesian optimizer preserves that symmetry all the way down. Ethyl acetate — an
ordinary ester — settles at a **-42 cm⁻¹** mode, where its "free energy" is not one.
`relax_to_minimum` displaces along the imaginary mode and re-optimizes; ethyl acetate needs one
such step and lands 0.016 kcal/mol lower, which confirms the diagnosis (a shallow rotor saddle,
not a different structure).

A fourth was found by a test rather than a measurement: filtering the x/y/z rotations by
singular value looks equivalent to a proper linearity test and is not — an optimized CO2 is bent
by a fraction of a degree, so its "null" rotation survives the cut and eats a real vibration.
Rotations are now built about the principal axes and kept by moment of inertia, the same
criterion the entropy uses.

**Validation is against measurement, not against itself.** Water's standard entropy comes out
**45.05 cal/mol/K against a measured 45.10**; the ZPE, the mode counts (including CO2's 3N−5),
the n-butane torsion profile (anti lowest, gauche +0.6, syn barrier 5.7) and water's IR band
ordering are all pinned the same way.

### Temporal, and a stopgap that was the wrong call

X3/X4 were first shipped with an *atom cap and a point cap* — refusing work that would block a
turn. That was wrong, and the timings say so: a four-species reaction is 4.6 s, a seven-point
scan 4.2 s, a five-solvent screen ~25 s, and a long scan on a mid-sized molecule is minutes.
Refusing a calculation because it is slow is a worse answer than running it durably. (X1/X2 were
genuinely different: a single point is 2.4 ms, where a workflow is pure overhead.)

So the expensive tools now **route by predicted cost** (`calc/xtb_cost.py`, a power law fitted
to those measurements, used only against a threshold): under the inline budget they compute in
the turn, over it they submit an `XtbJobWorkflow` on the existing `hpc-jobs` queue and return a
job id with a push-back. One activity rather than a fan-out, because every expensive part is
already content-addressed — a retry after a worker restart walks straight through the work it
already did. The job spec is a **closed, typed union**, the same boundary rule the proposal sets
for the expert escape hatch.

**`get_qm_job_status` → `get_job_status`.** Generalized rather than duplicated: "how is my
calculation doing" is one question, and two near-identical tools is a way to have the model
choose wrong. Dispatch is on the id prefix, so a foreign id is rejected before anything is
deserialized.

**Skills.** Six new: `reaction-thermodynamics`, `conformational-analysis`,
`atropisomer-assessment` (the one with a regulatory hook — a computed barrier maps to an
interconversion half-life and therefore to an ICH class, and the method's error spans two
classes, which is the whole point of the skill), `computed-spectra-comparison`,
`solvent-selection`, `bond-strength-and-radicals`. Five existing skills updated for the widened
ladder.

**The limit carried by skills rather than code, as with pKa (D-097/U3).** GFN2 homolysis
energies are badly overestimated in absolute terms even with spin polarization, while the
*orderings* hold (benzylic C–H clearly weaker than methane's). `bond-strength-and-radicals`
states the rule this implies — rank, never quote — and the reaction result attaches an
open-shell warning of its own.
