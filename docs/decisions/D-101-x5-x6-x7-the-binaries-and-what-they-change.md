# D-101 — X5/X6/X7: the binaries, and what they change

**X5, the `xtb` binary.** Added as a second backend behind the same task API, selected by
`settings.xtb_engine` (`auto` by default) and resolved to a concrete name *before* the cache key
is built — a key containing "auto" would mean different things on two deployments and they would
share entries computed by different programs.

**It is not a marginal improvement.** Measured, optimize + Hessian on the substrates this system
is pointed at:

| molecule                   | atoms | tblite + Cartesian L-BFGS | xtb backend | speedup |
|----------------------------|-------|---------------------------|-------------|---------|
| ibuprofen (MW 206)         |    33 |   19.0 s                  |    5.7 s    |  3.3x   |
| atorvastatin core (MW 559) |    76 |  315 s (177 steps)        |   38.1 s (39) | 8.3x  |
| erythromycin (MW 734)      |   118 | 1560 s (232 steps)        |  142.5 s (94) | 10.9x |

**This retires X9.** The internal-coordinate optimizer filed as "the single largest speedup
available for this workload" is ANCopt, and it is a process call away. Writing one would have
been a reimplementation of the reference.

**The seam is the Hessian, not the thermochemistry.** xtb prints its own thermodynamic block and
this backend ignores it, taking the Hessian matrix and handing it to `calc.xtb_thermo`. One RRHO
implementation — the one validated against water's measured standard entropy — keeps the symmetry
number an explicit input instead of xtb's silent guess, keeps quasi-RRHO identical across
backends, and therefore keeps free energies from the two comparable. The binary path reproduces
water's 45.10 cal/(mol K) exactly as the in-process path does, and that cross-backend agreement
is a test.

Also from X5: **GFN-FF**, which optimized the 118-atom substrate in 0.7 s. Not a quantum method
and it yields no orbitals, but it makes large-system pre-optimization free.

**A threading default that cost 4x.** Pinning `--parallel 1` was the cautious first choice and
made a 76-atom Hessian 98 s instead of 27 s. The default is now xtb's own (use the machine),
which is right for a dedicated worker pod; pin to 1 only where activities share one.

**X6, CREST.** Conformer, tautomer and protomer sampling. It removes this system's most pervasive
caveat — every other number describes one conformer — and supplies the **conformational entropy**
that every single-conformer free energy is missing. `compute_reaction_energy` gains
`level="thorough"`, which searches, works from the lowest member, and adds that term. It does
*not* Boltzmann-average free energies over every conformer: that is one Hessian per member, half
an hour each at 76 atoms. `treatment` on the result says which approximation was used rather than
letting a reader assume the better one.

**Rotamer degeneracy is load-bearing, not bookkeeping.** n-butane's gauche stands for two
mirror-image rotamers and its methyl rotations multiply further; weighting by degeneracy puts the
anti at 59.2% against CREST's own reported 59.14%, and the ensemble entropy at 6.23 against its
6.227. Ignoring degeneracy gives 73% — simply wrong. Both are pinned by a hand-computed test.

**CREST is the system's first non-deterministic calculator, and that had to be said out loud.**
Metadynamics samples from a random seed, so two runs differ. Everything else in `calc/` satisfies
"same key, same value"; this does not. The store is what makes it *stable* — the first run's
ensemble is what every later question sees, so a report and the number behind it cannot drift —
and `sampled: True` on the result tells a reader the populations are a sample.

This bit immediately, and instructively: the first test asserted `total_found == 2` for n-butane,
passed twice, and returned 4 on the third run because CREST split methyl-rotor variants
differently. The test now pins what is stable across runs and never a sampled count. A test that
pins a sampled quantity is a CI flake with a delay fuse.

**X7, the expert seam.** `run_xtb_task` takes a **typed spec, never a string** — no argv, no
flags, no `$...` control file, no paths. That is concrete rather than theoretical: a SMILES, an
ELN record and a retrieved document all reach this tool through the model, and xtb's control-file
syntax can reference external files and point charges. With a typed spec the worst a prompt
injection achieves is an expensive but well-formed calculation, which the authorization gate and
the cost router already bound. It is in `DEFAULT_WRITE_TOOL_GATES` — closed until an operator
grants the role — and deliberately has no second on/off setting, because two independent switches
for one capability is how a deployment comes to believe something is disabled when it is not.

Built last, as the proposal argued: after X1-X6 the list it has to cover is short — a non-default
GFN parametrization, a tightened accuracy — rather than everything the shaped tools had not got
to yet.

**Two things the binary does that its exit code does not tell you.** Its default
optimization level converges to ~1e-3 Hartree/Bohr, looser than the tolerance
`calc.xtb_opt` promises — ethanol stopped at 6.3e-4 Hartree/Angstrom against a 5e-4 target and
was correctly rejected, wasting the run; the fix is to ask for `vtight` rather than to loosen the
promise, because that promise is what makes the Hessian on top of it meaningful. And a Hessian on
**linear CO2** computes correctly — the output file holds its textbook 655/1345/2446 cm^-1 — and
then the process aborts during teardown with SIGABRT. A non-zero exit is therefore accepted when
every file the task is defined by is present, and logged; discarding a complete calculation over
a crash in its own cleanup would silently have lost every linear molecule.

**Both binaries are in the image**, as pinned release tarballs (UBI9 has neither in its
repositories). xtb is LGPL-3.0, crest GPL-3.0; both are invoked as separate processes over files
and never linked, so the usual analysis is that neither affects this codebase's licence — but
*distributing* them in an image is a decision for whoever owns the product, and the crest layer
is separable for exactly that reason. Both are optional at runtime: absent, `xtb_engine=auto`
falls back and the ensemble tools report that they are unavailable.
