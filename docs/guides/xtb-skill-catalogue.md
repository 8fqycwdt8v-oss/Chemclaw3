# xTB skill catalogue — the judgment layer, ideated in full

Companion to `docs/archive/plans/xtb-tools-proposal.md` (the *how*, archived) and `docs/guides/xtb-use-cases.md` (the
*why*). This is the **skill** layer: every piece of chemical judgment worth writing down,
across the whole xTB capability ladder, whether or not the tools exist yet.

Purpose: stop the skill set from drifting toward whatever happened to be built first. This
catalogue is the map; **§8 says what ships today**. The *Tier* columns in §1–§6 record the capability
phase each skill was gated on when the catalogue was written (X3 geometry/thermochemistry, X4
reaction composites, …); X3, X4, X6 and X11 have since shipped, so read §8 rather than a Tier cell for
current status. The calculations themselves run in `Chemclaw3-mcp`'s `servers/calc` (GFN2-xTB and
CREST); there is no DFT tier to escalate to.

**Ship rule.** A skill may only declare tools that exist — `make skill-validate` checks the
frontmatter against the live registry (D-081). So a skill for an unbuilt capability stays in
this document until its tool lands. That is the constraint that keeps the catalogue honest
rather than aspirational.

---

## 1. Product prediction — "what will I actually get?"

The family the whole system points at. A process chemist's first question is rarely "what is
the HOMO"; it is "what comes out of the flask, and what else comes out with it".

| Skill | The question | Needs | Tier |
|---|---|---|---|
| **`product-prediction`** | Given these reactants and conditions, what is the major product — and what are the credible minor ones? | Fukui + precedent (now); ΔG (X4) | **Now (partial)** |
| **`regioisomer-ranking`** | Which position reacts, and how confident is that call? | `predict_site_reactivity` | **Now** |
| **`chemoselectivity`** | Two reactive groups in one molecule — which one goes first? Do I need a protecting group at all? | Fukui + pKa (now); ΔG‡ (X5) | **Now (partial)** |
| **`tautomer-analysis`** | Which tautomer dominates, in this solvent — and therefore which structure every downstream number refers to? | X3 thermo, properly X6 | X3 |
| **`stereochemical-outcome`** | Which diastereomer, and by how much? | X3 + conformers (X6) | X6 |
| **`structure-elucidation-support`** | Three candidate structures fit the mass — which fits the spectrum? | X3 (IR frequencies **and intensities** from the Hessian) | X3 |

**The under-rated one is `structure-elucidation-support`.** An xTB Hessian yields not just
frequencies but IR *intensities*, i.e. a computable IR spectrum. Comparing a computed spectrum
against a measured one is a genuine discriminator between candidate structures for an unknown
impurity — a routine, painful analytical problem. This was missing from the use-case review and
raises X3's value further.

---

## 2. Degradation, impurities and stability — "what goes wrong on storage?"

Commercially the highest-stakes family in process R&D, and almost entirely unserved today.

| Skill | The question | Needs | Tier |
|---|---|---|---|
| **`degradation-liabilities`** | Where will this oxidize, hydrolyse, or photodegrade? Which forced-degradation conditions are worth running? | Fukui + functional-group judgment | **Now (partial)** |
| **`impurity-structure-hypotheses`** | An unknown at RRT 1.34 — which structures are electronically plausible, and which can I rule out? | Fukui (now); + IR/ΔG (X3/X4) | **Now (partial)** |
| **`oxidative-stability`** | Is this API prone to autoxidation? Which excipients/antioxidants matter? | IP/ω (X5), BDEs (X4) | X4 |
| **`hydrolytic-liability`** | Which bond hydrolyses first, and at which pH? | ΔG of hydrolysis (X4) + pKa | X4 |
| **`radical-and-HAT-selectivity`** | Which C–H is abstracted? How stable is the resulting radical? | Bond dissociation energies (X4, open-shell) | X4 |

**BDEs are now unblocked at the model level.** `Structure` validates a declared multiplicity
rather than refusing every open shell, so a homolysis ΔH — radical stability, HAT site
selectivity, antioxidant strength — needs only the reaction-energy composite (X4), not new
physics. That was an unintended dividend of the X1 work.

---

## 3. Conformation and shape — "which shape is the molecule actually in?"

The silent error source under everything else: today every number in the system describes one
force-field conformer.

| Skill | The question | Needs | Tier |
|---|---|---|---|
| **`conformational-analysis`** | Which conformers are populated, and does the answer change if I average properly? | X3, properly X6 | X3 |
| **`atropisomer-assessment`** | Is this a controllable stereoisomer under ICH, or does it interconvert freely at process temperature? | X3 scan, X5 `--bhess` | X3 |
| **`ring-strain-and-macrocyclization`** | Is this ring closure feasible? What is the strain penalty? | X3 | X3 |
| **`conformational-polymorph-risk`** | Does this molecule have several low-energy conformers — i.e. is it a polymorphism risk worth screening hard? | X6 | X6 |
| **`conformer-hygiene`** | The standing methodological rule: when is one conformer enough, and when does it invalidate the answer? | none — pure judgment | **Now** |

**`atropisomer-assessment` is the one with a regulatory hook.** A rotational barrier maps to an
interconversion half-life at a given temperature, and that number decides whether a compound
must be controlled as a separate stereoisomer. A computable answer to a regulatory question is
rare and worth prioritizing.

**`conformer-hygiene` needs no new tool** and guards every other skill. Currently one paragraph
inside `computational-evidence`; it deserves to be its own loadable skill once X3 makes
conformer choice an actual decision rather than a fixed limitation.

---

## 4. Reaction design and mechanism — "why, and what should I change?"

| Skill | The question | Needs | Tier |
|---|---|---|---|
| **`catalyst-ligand-selection`** | Which ligand for this coupling, and why? | Electronic descriptors (now); sterics need X3 | **Now (partial)** |
| **`solvent-selection`** | Which solvent — for rate, selectivity, solubility, *and* the green-chemistry scorecard? | ΔG_solv (X4), CPCM-X (X5) | X4 |
| **`mechanism-hypothesis-testing`** | Two mechanisms explain the data — which is energetically credible? | X4/X5 | X4 |
| **`barrier-and-selectivity-estimates`** | How high is the barrier? What selectivity does that imply? | X3 scans, X5 `--bhess` | X3 |
| **`redox-and-electrochemistry`** | Oxidation/reduction potential window; is this reagent strong enough? | IP/EA/ω (X5) | X5 |
| **`protecting-group-strategy`** | Which PG survives these conditions, which comes off first? | ΔG (X4) + precedent | X4 |
| **`acid-base-and-speciation`** | What is charged, at which pH, and how does that change reactivity? | pKa incl. **bases** (U2) | U2 |

**`catalyst-ligand-selection` is half-built.** The descriptors exist and are now wired into BO
(U1/D-096), but the featurization is electronic only — cone angle and buried volume need a
geometry. The skill can ship as electronic-only with that limit stated, and gets sharper at X3.

---

## 5. Process, formulation and safety

| Skill | The question | Needs | Tier |
|---|---|---|---|
| **`ionization-and-partitioning`** | Extraction pH, salt selection, ionization state | pKa | **Shipped** |
| **`salt-and-cocrystal-screening`** | Which counterion or coformer, and will the salt be stable? | pKa for **bases** (U2) | U2 |
| **`thermal-hazard-triage`** | Which compounds go to calorimetry first? | X4 decomposition energetics | covered by `thermal-safety-assessment` (`connectors/thermalsafety/skills/`) |
| **`crystallization-solvent-selection`** | Which solvent for the crystallization, which anti-solvent? | `predict_solubility`, `crystallisation_yield` | covered by `crystallisation-design` (`skills/`) |

**Guardrail, repeated because this family is where over-trust does damage.**
`thermal-hazard-triage` may only ever *order a queue for calorimetry*. It may never appear as
reassurance. `safety-screening`'s rule — the screen flags, it never clears — already extends to
computation, and any skill in this family inherits it.

---

## 6. Meta / cross-cutting

| Skill | Holds | Status |
|---|---|---|
| **`computational-evidence`** | Compute vs. retrieve; combining both; recording the result as a note | **Shipped** |
| **`calculation-selection`** | Which calculator for which question, and where the semiempirical tier stops | **Shipped** (`connectors/calc/skills/`) |
| **`reactivity-descriptors`** | Reading Fukui rankings and frontier orbitals honestly | **Shipped** |
| **`relative-energy-comparisons`** | What a semiempirical energy difference does and does not support | **Shipped** |
| **`descriptor-featurization`** | When to featurize a categorical BO space, and what the descriptors miss | folded into `experiment-design` |
| **`computed-spectra-comparison`** | Comparing a computed IR spectrum to a measured one | **Shipped** |

---

## 7. A measured finding that shaped §6

`compute_xtb_energy` is the tool an agent naturally reaches for to compare isomers, and until
this change it ran on a **raw ETKDG embedding**. Measured over five textbook isomer pairs, that
inverted the sign of the relative energy in **two of five** — isobutane vs. n-butane, and ethanol
vs. dimethyl ether — because the residual strain in an unrelaxed geometry exceeds the difference
being asked about. Relaxing with MMFF gets all five orderings right.

Fixed at the root (the energy path now relaxes, as `calc.pka` and `calc.xtb_props` already did)
and pinned by parametrized regression tests. But the residual limit is what
`relative-energy-comparisons` must carry: even relaxed, the *magnitudes* can be far off — ethanol
vs. dimethyl ether comes out 3.5 kcal/mol against an experimental ~12. **Orderings, not
magnitudes**, until X3 provides real optimization and thermochemistry.

This is the same shape as the pKa finding: the tool is good for ranking and misleading for
values, and the only reason we know either is that both were measured before judgment was
written about them.

---

## 8. What ships today, and what is still unwritten

Read off the tree (`ls skills/ src/chemclaw/connectors/*/skills/`), not off this list, when it
matters — `make skill-validate` holds each shipped skill's `tools:` against the live registry.

**Shipped from this catalogue:** `product-prediction`, `relative-energy-comparisons`,
`degradation-liabilities`, `reactivity-descriptors`, `computational-evidence`,
`ionization-and-partitioning`, `reaction-thermodynamics`, `conformational-analysis` (absorbing
`conformer-hygiene`), `atropisomer-assessment`, `computed-spectra-comparison` (absorbing
`structure-elucidation-support`), `solvent-selection`, `bond-strength-and-radicals` (absorbing
`radical-and-HAT-selectivity`), `tautomer-analysis`, `molecular-association`,
`ensemble-workflows`, plus `calculation-selection` and `experiment-design` in their connector
bundles. `thermal-hazard-triage` and `crystallization-solvent-selection` are covered by
`thermal-safety-assessment` and `crystallisation-design`.

**Still missing at the model level: a transition-state search.** `barrier-and-selectivity-estimates`
and `mechanism-hypothesis-testing` need one, and a relaxed-scan maximum is a sketch of a barrier
rather than a barrier.

**Unblocked by capability and not yet written:** `hydrolytic-liability`,
`protecting-group-strategy`, `oxidative-stability`, `chemoselectivity`, `regioisomer-ranking` (as a
standalone skill; `product-prediction` carries the core case), `impurity-structure-hypotheses`,
`catalyst-ligand-selection`, `redox-and-electrochemistry`, `stereochemical-outcome`, plus the CREST
family in §9.

**Still gated on accuracy rather than capability:** `salt-and-cocrystal-screening` and
`acid-base-and-speciation` for aliphatic-amine bases, whose pKa error is the continuum solvent's
(`docs/guides/xtb-use-cases.md`).

---

## 9. What CREST adds — the skills its searches unlock (X6, X11 shipped)

CREST is a different *kind* of capability from everything else here: the others compute a
property of a structure you hand them, and CREST decides **which structure** to hand them.
That makes it upstream of the whole catalogue, and it opens a family of questions no
amount of judgment over single-point properties could reach.

### Shipped

| Skill | The question CREST answers | Search | Phase |
|---|---|---|---|
| **`tautomer-analysis`** | Which structure is this molecule actually in — and therefore what does every other number here refer to? | `--tautomerize` | X6 |
| **`conformational-analysis`** (extended) | Which shapes are populated, in what proportion, and what conformational entropy is every single-conformer free energy missing? | conformer search | X6 |
| **`molecular-association`** | How do two molecules associate, and how strongly — API with excipient, substrate with catalyst? | `--nci` | X11 |

`tautomer-analysis` is the one with the widest blast radius. A pKa, a Fukui ranking, a
dipole and a reaction free energy all describe whichever tautomer was drawn; if that is
the minor form, none of them is a number about the compound. It is now askable, and the
skill's main job is making sure it *gets* asked for the scaffolds where it matters
(heterocyclic N-H above all).

### Unlocked by capability, not yet written

| Skill | The question | Search | Why it is worth writing |
|---|---|---|---|
| **`protomer-and-microspecies`** | Which nitrogen protonates first? Which microspecies dominates at this pH? | `--protonate`, or — since D-2026-08-25 — `chem`'s `enumerate_protonation_states` plus `rank_species`, which needs no binary at all and is what `run_microspecies_profile` runs | Narrowed by X11, not closed. `predict_pka` now covers **aromatic and aryl nitrogen** (ρ 1.000) and picks the most stable protomer by RDKit enumeration, so the common single-site case is answered. What remains is genuinely structural: a molecule with several comparable basic sites, and the microspecies distribution across pH. Note that a CREST protomer search would *not* have rescued aliphatic amines — that failure is solvation, not structure (D-104). |
| **`salt-and-cocrystal-screening`** | Which counterion, and will the salt be stable? | `--protonate`/`--deprotonate` | Partly ungated by X11: a ΔpKa is computable when the base is aryl nitrogen, though both partners' uncertainties (±1.0 and ±1.6) are of the same order as the 2–3 unit rule being tested, so it is a direction rather than a verdict. An aliphatic-amine API — a great many of them — is still out of reach. |
| **`conformational-polymorph-risk`** | Does this molecule have many low-energy conformers — i.e. is it a polymorphism risk worth screening hard? | conformer search | A direct readout: `total_found` and a flat population distribution *are* the risk signal. Cheap now that ensembles exist. |
| **`shape-and-exposure`** | Which face or site is sterically accessible across the populated ensemble, not just in one geometry? | conformer search | The standing "sterics are invisible" caveat in `product-prediction` and `catalyst-ligand-selection` is a *single-conformer* limitation as much as an electronic one. |
| **`intramolecular-interactions`** | Is there an internal hydrogen bond, and does it hold in the populated ensemble? | conformer search | Worth several kcal/mol and it silently decides many of the comparisons this system makes. Answerable by inspecting the ensemble's geometries rather than one embedding. |
| **`ensemble-weighted-properties`** | What is the Boltzmann-averaged dipole, pKa, or descriptor — rather than one conformer's? | conformer search | **Shipped as capability** (`compute_ensemble_property`, D-2026-08-25) and as judgment in the `ensemble-workflows` skill rather than under this name. The BO featurization (U1/D-096) is the consumer still to wire. |

### The honest constraint on all of it

A CREST search is **the most expensive calculation in this system** — ~50 s for
14-atom n-butane, and it scales badly — and it is **stochastic**, so it samples
conformational space rather than enumerating it. Both facts belong in every skill above:
a population is a sampled quantity, a missing conformer is not evidence of absence, and
the search is a durable job rather than something a conversation waits for.
