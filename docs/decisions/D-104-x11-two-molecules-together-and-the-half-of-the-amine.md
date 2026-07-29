# D-104 — X11: two molecules together, and the half of the amine problem that is refused

**Context.** Two gaps were named together in the X11 backlog entry because both are CREST searches
this system already had wired at the CLI layer and neither had a calculator: `--nci` samples how
two molecules associate, and `--protonate`/`--deprotonate` was the presumed route to **U2**, the
basic amines the pKa predictor had never covered. Both were assumed to be work, not risk. One of
them was.

### Non-covalent complexes: the only question here about a pair

`calc.complexes` computes an interaction energy as the difference of **relaxed** species — the
complex at its best sampled binding mode, minus each monomer optimized alone. That deliberately
includes the deformation cost of binding, which a rigid-monomer definition drops and which is part
of what associating actually costs. `--nci` is what makes the search tractable: it wraps the pair
in a logfermi wall, without which metadynamics simply lets the two molecules drift apart.

Validated against CCSD(T)/CBS: water dimer **-4.97** (ref -5.0), ammonia dimer **-2.86** (-3.1),
methane dimer **-0.41** (-0.5), water-ammonia **-5.31** (-6.4). Three within a few tenths, the
mixed donor/acceptor pair 1.1 kcal/mol under-bound. Good enough to rank association strength and
to say bound or not; not good enough to quote a binding constant.

The pair is the **cache subject**: `run_cached_interaction` keys on the combined starting
structure, so A-with-B and B-with-A are one entry rather than two runs of a minutes-long search.
Two limits ship with every number and are stated in the model and the skill: it is an *energy*,
not a free energy — the association entropy that decides whether the complex exists at a given
temperature is absent, and for weak pairs it is comparable to the interaction itself — and the
search is stochastic, so a binding mode that was not sampled cannot be reported.

### Basic amines: one class calibrates better than the acids, the other is refused

Fitted over 20 experimental amines. The class splits so sharply that shipping one number for both
halves would have been indefensible:

| class | n | Spearman | R² | RMSE | ships |
|---|---|---|---|---|---|
| aromatic / aryl N — pyridines, azoles, anilines | 7 | **1.000** | 0.993 | 0.17 | yes, ±1.0 |
| aliphatic amines | 13 | **-0.17** | — | — | **no** |

Aromatic nitrogen is the *better* of this system's two pKa calibrations — better than the acid
path's ρ 0.965 / RMSE ~1.5. Held out afterwards: 1,2,3-triazole +0.57, 3,4-lutidine -0.25.

**The refusal is diagnosed, not cautious.** In the gas phase GFN2 reproduces the experimental
proton affinity order exactly (NH₃ < MeNH₂ < Me₂NH < Me₃N), so the Hamiltonian is not the problem.
Switching on ALPB **reverses** that order completely. And the true aqueous order is neither: it is
non-monotonic (Me₃N < NH₃ < MeNH₂ < Me₂NH), because aqueous aliphatic amine basicity is set by how
many hydrogen bonds the ammonium ion can donate to water — which falls with substitution, and
which a continuum model, having no explicit solvent, cannot see. **No linear recalibration
recovers a non-monotonic relationship**, so this is not a threshold waiting to be relaxed; it
changes when the solvation treatment changes, and explicit-solvent or cluster-continuum is not in
this system. ρ = -0.17 is not "imprecise", it is no ranking ability, so a number would carry no
information while looking exactly like one that did (G4).

**The base path optimizes where the acid path does not**, and that was measured too: on the same
seven references, MMFF geometries give ρ 0.893 and GFN2-optimized ones give 1.000. Protonation
pyramidalizes a nitrogen and puckers a ring — the relaxation is doing real work. The acid
calibration keeps its force-field policy because it was fitted and validated through that path;
refitting it is a separate deliberate change, not a side effect of this one.

**Acid wins when a molecule has both.** A compound with an O-H has a pKa in the ordinary sense and
that is the number the question means. The `site` field says which equilibrium was computed,
because an amine's tabulated value is its *conjugate acid's* pKa and quoting it as "the pKa" is
wrong by orders of magnitude in the wrong direction.

**What was not built.** `--protonate`/`--deprotonate` — the structural half — turned out not to be
what U2 needed. The split above is electronic and the protomer enumeration is cheap in RDKit; a
metadynamics search for protonation *sites* would not have moved a correlation that fails for
solvation reasons. Left in the backlog rather than built speculatively.
