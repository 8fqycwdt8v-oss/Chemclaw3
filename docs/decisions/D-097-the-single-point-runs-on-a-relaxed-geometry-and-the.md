# D-097 — The single point runs on a relaxed geometry, and the skill catalogue that found it

**Context.** Ideating the skill layer (`docs/xtb-skill-catalogue.md`) surfaced that
`compute_xtb_energy` is the tool an agent naturally reaches for to compare isomers, and that no
skill governed that use. Measuring before writing the judgment — the discipline that produced the
pKa finding in D-095's companion review — found a defect rather than a limitation.

**The finding.** Over five textbook isomer pairs, the single point on a raw ETKDG embedding got
the **sign of the relative energy wrong in two**: isobutane vs. n-butane, and ethanol vs. dimethyl
ether. The cause is not the Hamiltonian but the geometry — residual strain in an unrelaxed
embedding exceeds the energy difference being asked about. The same geometries relaxed with MMFF
give all five orderings correctly.

**Decision.** `calc.xtb` relaxes before the single point, via `_sp_structure`. This makes the
geometry policy uniform: `calc.pka` and `calc.xtb_props` already relaxed for exactly this reason,
and the energy path was the one that did not. Pinned by a parametrized regression test over all
five pairs, so a change that reverts the relaxation fails loudly rather than returning confident,
backwards chemistry.

**Consequence.** Cached single-point energies re-address (the geometry is part of `structure_id`),
so old entries are recomputed rather than mixed with new ones — the same clean invalidation D-095
recorded, for the same reason. Absolute energies shift slightly; every *ordering* improves.

**The residual limit, carried by a skill rather than a comment.** Relaxed magnitudes are still
poor — ethanol vs. dimethyl ether comes out ~3.5 kcal/mol against an experimental ~12. The new
`relative-energy-comparisons` skill states the rule this implies (orderings, not magnitudes; ties
under ~1 kcal/mol; same formula and charge or the comparison is meaningless, not merely
imprecise) and points at X3 for anything quantitative.

**Skill catalogue.** `docs/xtb-skill-catalogue.md` maps 28 skills across six families — product
prediction, degradation/stability, conformation, reaction design, process/formulation, and
cross-cutting — against the capability each needs. Three shipped here because they need none:
`product-prediction` (regioisomers and the kinetic-vs-thermodynamic question the tools cannot
answer for you), `relative-energy-comparisons`, and `degradation-liabilities` (forced-degradation
study design and impurity hypothesis filtering).

**What the distribution argues.** **19 of the 28 catalogued skills are gated on X3 or X4.** The
judgment layer is not the bottleneck — the capability under it is. Two entries also change the
value case for those phases: an xTB Hessian yields IR *intensities* as well as frequencies, so a
computed IR spectrum is a real discriminator between candidate impurity structures (X3); and
bond dissociation energies — radical stability, HAT selectivity, antioxidant strength — are now
unblocked at the model level, because D-095's `Structure` validates a declared multiplicity
instead of refusing every open shell. Both need only the X4 reaction composite, not new physics.
