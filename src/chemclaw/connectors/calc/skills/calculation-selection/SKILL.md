---
name: calculation-selection
description: >-
  Judgment for choosing which fast calculator to run for a given question
  (semiempirical energy vs. predicted property) and reading the result honestly.
tools:
  - refine_ensemble
  - compute_ensemble_property
  - rank_species
  - survey_bond_strengths
  - enumerate_tautomers
  - enumerate_protonation_states
  - enumerate_bond_cleavages
  - describe_topology
  - compute_xtb_energy
  - compute_electronic_properties
  - predict_site_reactivity
  - predict_pka
  - predict_solubility
  - optimize_geometry
  - compute_thermochemistry
  - scan_coordinate
  - compute_reaction_energy
  - compare_solvents
  - sample_conformers
  - predict_developability_profile
  - predict_logd
---

# Calculation selection

Holds the *judgment* about the fast calculators; the mechanics live in the tools
(e.g. `compute_xtb_energy`). Use this to decide **which** calculator answers the
question and how far to trust it.

For the prior question — *should anything be computed at all*, and how a computed number
sits next to retrieved precedent — load `computational-evidence`. Precedent outranks
calculation; this skill assumes that decision is already made.

## Which calculator

- **Electronic energy / relative stability / conformer energy** → `compute_xtb_energy`
  (GFN2-xTB semiempirical single point on one seeded conformer). Fast, deterministic,
  good for *relative* comparisons of related structures.
- **Conformationally flexible molecule, solution-phase behavior** → `sample_conformers`
  (a CREST metadynamics search, Boltzmann-weighted with rotamer degeneracies, reporting the
  conformational entropy a single-conformer free energy is missing). Minutes on a drug-sized
  molecule, so it returns a job id above the inline budget — poll with `get_durable_job_status`. Reach
  for it instead of `compute_xtb_energy` when a single seeded conformer is unlikely to be
  representative (a flexible chain, multiple accessible rotamers), and read
  `conformational-analysis` first: the search is *sampled*, so a missing conformer is not
  evidence of absence.
- **Aqueous solubility** → `predict_solubility` (fast property model; reports an
  uncertainty — surface it).
- **pKa** → `predict_pka` (GFN2-xTB solvated protonation/deprotonation energy +
  calibration). Two domains with different accuracy, and the tool says which one it
  used: **O-H/S-H acids** (carboxylic acids, phenols, alcohols, thiols; ~1.6-unit
  uncertainty) and the **conjugate-acid pKa of aromatic or aryl nitrogen** (pyridines,
  azoles, anilines; ±1.0, and the more accurate half). It **refuses aliphatic amines**
  and errors on a molecule with neither site; N-H/C-H acids are out of scope. "Has a
  nitrogen" is not "has a base": amide, carbamate, urea and sulfonamide nitrogen (lone pair
  in the C=O/S=O), nitrile nitrogen, and pyrrole-type ring nitrogen (lone pair in the
  aromatic sextet) are **not** basic sites, so acetamide or pyrrole errors rather than
  returning a pKa. Load
  `ionization-and-partitioning` before using the value for anything — an acid site wins
  silently over a basic one, and individual predictions miss by up to two units.
- **Which atom reacts (regioselectivity)** → `predict_site_reactivity` (condensed
  Fukui indices; three fast single points). Load the `reactivity-descriptors` skill
  before interpreting the ranking — it ranks sites *within* one molecule only. It now
  takes a `structure_id`, so the question a chemist actually asks after a conformer
  search — *which site is reactive in **this** conformer* — is answerable rather than
  answered on a fresh embedding. A second mode at the same geometry is free.
- **Frontier orbitals, dipole, partial charges, bond orders** →
  `compute_electronic_properties` (one single point). Best used to *compare* related
  molecules; also covered by `reactivity-descriptors`.
- **A real 3D structure** → `optimize_geometry` (relaxes to a genuine GFN2 minimum).
  Everything above runs on a force-field geometry; this is the tool that stops that
  being true.
- **Free energy, entropy, an IR spectrum, "is this a minimum?"** →
  `compute_thermochemistry` (optimization + Hessian). The only route to a ΔG here.
  `computed-spectra-comparison` holds the judgment on the spectrum.
- **Does this reaction go?** → `compute_reaction_energy` (every species treated
  identically; balance enforced). Load `reaction-thermodynamics` first — it answers
  equilibrium and says nothing about rate.
- **Which solvent?** → `compare_solvents`. Load `solvent-selection` first; the
  computable criterion is rarely the binding one.
- **Rotational barrier, torsion profile, ring strain** → `scan_coordinate`
  (`conformational-analysis`, and `atropisomer-assessment` for the regulatory case).

**Any ΔG needs a rotational symmetry number, per species.** Entropy depends on sigma, so a
defaulted 1 puts a symmetric molecule's free energy too low by R·ln(sigma) — 0.41 kcal/mol for
H2, N2, O2, CO2 or water, 1.47 for benzene. Supply it: `compute_thermochemistry` takes
`symmetry_number` for the one molecule; `compute_reaction_energy` and `compare_solvents` take
`symmetry_numbers`, a map keyed by each species' **exact** SMILES string. The values are
1 for no rotational symmetry, 2 for H2/N2/O2/CO2/water, 3 for ammonia, 6 for ethane, 12 for
benzene. Do not assume it cancels across a balanced equation — it cancels only when both sides
carry the same symmetry, and any hydrogenation consumes H2. The two reaction tools enforce this:
a species left out of the map gets sigma=1 and the result reports **no ΔG**, naming that species,
while ΔE and ΔH — which sigma does not touch — stand as reported. Stating 1 explicitly is a real
statement and does yield a ΔG, so state it for the unsymmetric species too rather than omitting
them.

## When the question is about a *set*, not a structure

Everything above answers about one structure. Four jobs answer about a set, and the judgment for
all of them is in **`ensemble-workflows`** — load it before using any of them, and before deciding
that a single-structure answer is good enough.

- **Which form is this molecule actually in?** → `rank_species` over `enumerate_tautomers`, or the
  fixed sequence `run_tautomer_resolution`. Ask this *first* on anything with a mobile proton
  between heteroatoms: every other number here describes whichever tautomer was drawn.
- **What is charged, at which pH?** → `enumerate_protonation_states` then `rank_species`, or
  `run_microspecies_profile`. This is the amphoteric and polyprotic case `predict_pka` and
  `predict_logd` refuse; it is not a substitute for them on a single site, where they are calibrated
  and this is not.
- **Is this property a real number for a floppy molecule?** → `compute_ensemble_property`. Returns
  a mean *and a spread*, and the spread is the finding as often as the mean is.
- **Which bond breaks first?** → `enumerate_bond_cleavages` then `survey_bond_strengths`, or
  `run_bond_strength_survey`.
- **How much of the folded form is there?** → `refine_ensemble`, which re-weights by free energy
  instead of electronic energy. A different treatment, not a better one, and it costs a Hessian per
  member — reach for it when a *population* is the answer, not when a geometry is.

The enumeration tools are free and structural; only the ranking costs anything. So enumerate first,
look at the set, and then decide what to spend. `describe_topology` is free too and answers whether
the molecule is flexible enough for a search to find anything.

## The cost ladder, and when a tool hands back a job id

Roughly: a single point is milliseconds; an optimization is under a second for a small
molecule; a Hessian is 6N of those; a reaction is that per species; a solvent screen is
that per solvent. The expensive tools estimate their own cost and, above a threshold,
return a **job id instead of a result** — report it and poll with `get_durable_job_status`
rather than treating it as a failure. Prefer `level="quick"` when only an ordering of
electronic energies is needed; it skips every Hessian.
- **pH-dependent lipophilicity (logD)** → `predict_logd`. It applies the Henderson-Hasselbalch
  correction in the direction the site calls for, so say which site the number belongs to. Use
  for HPLC mobile-phase pH selection, extraction, or formulation questions where the
  pH-independent LogP alone is not the number that matters.

  Its domain is **strictly narrower than `predict_pka`'s**, not the same — do not promise a
  logD on the strength of a pKa having worked. It is built on `predict_pka`, so it inherits
  every refusal that tool makes (aliphatic amines, no ionisable site, charged inputs), and then
  adds one of its own: `predict_pka` reports **one** pKa and one Henderson-Hasselbalch term can
  consume exactly one, so a molecule that ionises at two sites in the working pH window is
  refused rather than corrected once.

  **Served:** a single ionisable centre — one O-H/S-H acid (carboxylic acid, phenol, alcohol,
  thiol) or one aromatic/aryl nitrogen base. Extra sites are fine when they stay un-ionised at
  the pH asked for, so a diol, a sugar or a polyol (O-H, pKa ~15) is served at any ordinary pH,
  and a diacid is served well below its pKa.

  **Refused:** anything **amphoteric** (an acid site and a base site — amino acids, aminophenols,
  nicotinic acid), because `predict_pka` always answers with the acid and never evaluates the
  base; and any **polyprotic** molecule whose reported site is substantially ionised at that pH
  (succinic acid at 7.4). Both refusals exist because the second pKa is not computable from this
  predictor at all — the alternative was a number wrong by 2-5 log units carrying a +/-1.6
  uncertainty. Report the refusal and what it says; do not substitute logP for logD and do not
  retry at a pH you picked to get past the gate.
- **Developability triage (Ro5/Veber, MW, LogP, TPSA, H-bond counts)** →
  `predict_developability_profile`. Report the flags as heuristics to weigh, never a pass/fail
  verdict on their own.
- **Reaction exotherm / thermal-hazard screen** → `compute_reaction_energy`, whose result
  carries `is_strongly_exothermic` against a configured threshold. Use `level="quick"` when the
  flag is all you need: it skips every Hessian and differences electronic energies only.
  Advisory, like the structural hazard screen (`screen_hazards`) — a flag for attention, never
  a safety certification.

## Reading results honestly

- xTB energies are only meaningful **relatively** (same method, comparable
  systems); never present a single absolute Hartree value as a physical answer on
  its own — compare against a reference or a related molecule.
- Every result is cached, so exploring many related structures is cheap — do the
  comparison rather than reasoning from one number.
- If a property predictor reports an uncertainty, state it; if the question needs
  higher accuracy than a fast method gives, say so rather than over-claiming. There is
  **no** heavier tier to escalate to — semiempirical is all of it — so the honest move
  is to name the limit and propose the experiment that would settle it.
- The **fast** calculators (single point, properties, Fukui, pKa) run on a force-field
  geometry, not a GFN2-optimized one. Fine for ranking and for comparing related
  structures; when the question is about a specific conformation or needs a real
  stationary point, `optimize_geometry` is the fix and costs almost nothing.
- Everything here still describes **one conformer**, optimized or not. That limit does
  not go away with a better geometry — `conformational-analysis` says when it
  invalidates an answer rather than merely widening it.
