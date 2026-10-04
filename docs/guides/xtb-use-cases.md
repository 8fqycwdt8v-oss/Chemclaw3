# Where xTB earns its place in ChemClaw — use cases, tiered by what unlocks them

Companion to `docs/archive/plans/xtb-tools-proposal.md` (the *how*, archived once its build was done). This is the *why*: the concrete
process-R&D questions xTB can answer, what each is worth, and which build phase unlocks
it. It was written to drive phase priority from user value rather than from what is
technically adjacent, and that build is now done: X3 (optimization + Hessian/RRHO), X4 (reaction
composites, solvent comparison), X6/X11 (CREST ensembles, tautomers, NCI) and the BO featurization
all ship, computed by `Chemclaw3-mcp`'s `servers/calc` behind this repository's cache. §7 says what
is still open. The tiers below are kept because the value judgement per use case still holds.

**Framing.** ChemClaw is a pharmaceutical/chemical **process** R&D assistant, not a
computational chemistry workbench. The bar for a use case here is not "xTB can compute
it" but "a process chemist makes a different decision because of it". Several things xTB
does well fail that bar and are listed in §5 rather than dressed up.

---

## 1. How to read the tiers

| Tier | Meaning |
|---|---|
| **Now** | Answerable with the original single-point calculators (shipped first) |
| **X3** | Needs geometry optimization + Hessian/thermochemistry |
| **X4–X6** | Needs reaction composites, the `xtb`/`crest` binaries, conformer ensembles (all shipped) |

Value is judged by *decision impact* — does it change what goes in the flask, the
report, or the next campaign — not by how interesting the number is.

---

## 2. Tier "Now" — available with the shipped tools

### 2.1 Regioselectivity of a functionalization ★★★
*Which ring position gets nitrated / halogenated / metalated? Where does the nucleophile
add?* — `predict_site_reactivity`.

The commonest structural question in route scouting, and until X2 the system had **no**
answer for it. Verified to reproduce the classical pattern in both directions
(activating substituents → *ortho/para*, -NO₂ → *meta*). Judgment lives in
`reactivity-descriptors`.

### 2.2 Triage before committing lab time or a BO campaign ★★★
*We have twelve candidate substrates/reagents and capacity for four.*

Rank the set electronically (HOMO/LUMO gap, charges, Fukui) and hand the shortlist to
`experiment-design`. This is the highest-leverage use of a *fast* method: the value is
in the ranking, which is exactly what a semiempirical method is good at, and the cost of
being wrong is one wasted experiment rather than a wrong conclusion.

### 2.3 Explaining an anomalous result already in the ELN ★★★
*The nitration went to the wrong position — why?*

Retrieval says *what* happened; the descriptor says whether the electronics predicted
it. When calculation and observation agree, the mechanism hypothesis firms up; when they
disagree, that discrepancy is itself the finding (sterics? directing group? the reagent's
own selectivity?). A question the knowledge graph alone cannot answer.

### 2.4 Ranking acidity within a congeneric series ★★
*Which of these six phenol analogues is the most acidic?* — `predict_pka`.

**Ranking only.** Benchmarked against 12 experimental values (the pKa suite, which moved to `Chemclaw3-mcp` with the predictor in `D-2026-08-16-the-physics-leaves-the-cache-stays`):
Spearman ρ **0.965**, but individual errors reach **±2.1 units**. See §4 — this
distinction is the whole content of the `ionization-and-partitioning` skill.

### 2.5 Comparing electronic character across analogues ★★
*Which aryl halide in this series is most activated toward oxidative addition? Which
amine is the strongest nucleophile?* — `compute_electronic_properties`.

Frontier orbital energies and partial charges, compared *within* a series. Useful for
building intuition and for narrowing a screen; never as an absolute descriptor.

### 2.6 Sanity-checking a proposed impurity or degradant structure ★★
*Is this proposed structure electronically plausible as the oxidation product?*

Fukui indices say whether the proposed site of attack is the one the molecule actually
exposes. A cheap plausibility filter on a structural hypothesis before anyone spends
LC-MS time chasing it.

---

## 3. Tier X3+ — what the later phases unlocked

Ordered by decision impact, which is how X3's scope was cut. Everything here is built except a real
barrier (item 6): there is no transition-state search, so a relaxed-scan maximum is a sketch.

| # | Use case | Needs | Value |
|---|---|---|---|
| 1 | **Tautomer / protomer ranking** — which form dominates, and therefore which structure the report, the pKa, and the solubility all refer to | X3 (+X6 for microstates) | ★★★ |
| 2 | **Reaction thermodynamics (ΔG)** — is this esterification/amidation/deprotection thermodynamically allowed, and where does the equilibrium sit at process temperature | X4 | ★★★ |
| 3 | **Solvent selection by ΔG_solv** across a real solvent set — connects directly to the existing green-chemistry and solvent-heavy eval cases | X4 (+X5 for CPCM-X) | ★★★ |
| 4 | **Atropisomer / rotational barrier** — is this a controllable stereoisomer under ICH, or does it interconvert freely at process temperature? A regulatory question with a computable answer | X3 (scan) + X5 (`--bhess`) | ★★★ |
| 5 | **Conformational preference** — amide rotamers, ring conformation, the shape a crystallization or a binding argument depends on | X3, properly X6 | ★★ |
| 6 | **Barrier estimates for competing pathways** — rough selectivity between two products, via a relaxed scan | X3/X5 | ★★ |
| 7 | **Thermal-hazard *pre-screen*** — decomposition energetics of energetic motifs, as a **triage input to `screen_hazards`, never a substitute for calorimetry** (see §5) | X4 | ★★ |
| 8 | **Macrocyclization / ring-strain feasibility** | X3 | ★ |
| 9 | **Large systems** (peptides, catalyst–substrate complexes) via GFN-FF | X5 | ★ |

**What this implies for X3's scope.** Items 1 and 4 are the highest-value entries and
both need *thermochemistry*, not just optimization — a geometry alone answers neither.
That argues for X3 shipping optimization **and** the Hessian/RRHO path together rather
than optimization first, which is a change from the proposal's implied ordering.

---

## 4. The pKa accuracy finding (measured, not assumed)

Benchmarked `predict_pka` against 12 experimental values spanning pKa 0.2–15.9 across
carboxylic acids, phenols, thiols and alcohols:

| Metric | Value | Reading |
|---|---|---|
| Spearman ρ | **0.965** | Ranking a series is reliable |
| MAE / RMSE | 1.10 / 1.25 | The reported ±1.6 uncertainty is **honest**, even conservative |
| Worst error | **+2.08** (benzoic acid) | A single value is *not* reliable |
| Basic amines | **unsupported** | Errors out — no O-H/S-H site to deprotonate |

**Why the worst-case number matters more than the average.** Process decisions that use
pKa — the pH for an extraction or a wash, whether a salt will form — turn on rules of
the form "work two units away from the pKa". An error of 2 units *inverts* such a
decision while looking entirely plausible. So the honest rule is: **rank with it, never
set a pH with it.** Both halves are asserted in the pKa suite — which moved to `Chemclaw3-mcp`
with the predictor itself — so a future recalibration cannot quietly break the claim the skill
rests on.

**The gap worth naming — half closed by X11 (D-104).** Most pharmaceutical APIs are
**basic amines**, and pKa v1 covered only neutral O-H/S-H acids. Extending to protonated
bases was recommended here as a calibration question rather than a new capability. It was
done, and the calibration answered it in two halves rather than one:

- **Aromatic and aryl nitrogen** (pyridines, imidazoles, azoles, anilines) predicts at
  Spearman **1.000**, R² 0.993, RMSE 0.17 over pKa 1.0–6.95 — *better* than the acid
  calibration this section describes — and ships with ±1.0.
- **Aliphatic amines** rank at **−0.17** and are refused. The cause is the solvation
  model, not the fit: gas-phase GFN2 reproduces the experimental proton affinity order
  exactly, ALPB reverses it, and the true aqueous order is non-monotonic because it is set
  by the ammonium ion's hydrogen bonding to water. No linear recalibration recovers that.

So the most common pharma pKa question is now partly answerable and, where it is not,
declined with a reason. Closing the remaining half needs explicit-solvent or
cluster-continuum treatment — a genuine new capability, not a recalibration. N-H acids
remain out of scope.

---

## 5. What xTB must not be used for here

Guardrails, because each of these is something a user will plausibly ask for and a
semiempirical number will plausibly-looking answer.

- **Thermal safety / process hazard assessment.** A computed decomposition energy is not
  a DSC, an ARC, or a hazard evaluation. It may *triage* which compounds get sent for
  calorimetry; it may never appear in an answer as reassurance. `safety-screening`'s
  rule — the screen flags, it never clears — extends to computation unchanged.
- **Any reportable number without its provenance.** A computed value enters the knowledge graph
  as a note citing its `calc_ref`, written directly and correctable rather than pre-approved
  (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`) — so the citation is what a chemist
  checks at the point of use.
- **Absolute pKa, logP, or solubility for a specification.** Ranking, not values (§4).
- **Yields, rates, selectivity ratios.** xTB gives orderings and energy differences, not
  kinetics. A barrier estimate is a hypothesis, not a predicted product distribution.
- **Binding affinity / docking scores.** Different problem, different methods, and
  outside process R&D's scope.
- **Replacing precedent.** If the ELN or the graph already answers the question, that
  evidence outranks a calculation. `computational-evidence` holds this rule.

---

## 6. Cross-cutting integrations — what ChemClaw gets that a bare xTB wrapper does not

These are the use cases that justify xTB living *inside* this system rather than beside
it. None is a new calculator; each is a connection between layers.

1. **Computed values as citable, reproducible evidence.** Every result is content-addressed
   by structure, method, and engine build, so a number in a report can be traced to the
   exact geometry and stack that produced it, and re-derived years later. That is the
   traceability property no ad-hoc calculation has. **Available now.**
2. **Descriptors as BO featurization.** BoFire campaigns currently treat ligand/base/solvent
   as *categorical* — the model cannot generalize to an option never tried. Replacing the
   category with computed electronic descriptors lets it interpolate across the space.
   This is probably the single highest-value integration in this document. **Built**
   (D-096, `science/bo/featurize.py`): a molecular categorical is featurised through cached GFN2-xTB
   descriptors that carry `calc_refs` into the note. Electronic only — sterics (cone angle, buried
   volume) are the stated limit.
3. **Enriching ELN-ingested structures.** Compute descriptors once per ingested substrate
   so the knowledge graph becomes searchable by electronic character, not just by
   substructure. Cheap (cached forever), and it makes retrieval smarter. **Available now.**
4. **The accuracy ceiling is the answer, not a rung.** There is no DFT tier to escalate to
   (`D-2026-08-26-semiempirical-is-the-whole-tier`), so when a decision turns on a difference
   inside GFN2-xTB's error bar the honest move is to say so and propose an experiment, not to
   quote a number the method cannot support. `calculation-selection` states this boundary.
5. **Computation inside the autonomous harness.** Fast calculators run inline with no
   durable job, so a plan step can compute without the harness waiting on Temporal — the
   reason §2.2's triage is practical at all.

---

## 7. Status of the original priority list

1. **Descriptors into BO featurization** (§6.2) — built (D-096).
2. **pKa for bases** (§4) — aromatic/aryl nitrogen built (D-104); aliphatic amines declined for
   a solvation reason; N-H acids out of scope.
3. **X3, optimization *plus* thermochemistry** (§3) — built.
4. **X4 reaction energies and solvent screening** — built.
5. **X5/X6** — CREST ensembles, tautomer and NCI searches built (X6/X11). What remains open is a
   transition-state search, and explicit-solvent treatment for the aliphatic-amine residual
   (`docs/planning/DEFERRED.md`, "CREST's other run types" and "ML interatomic potentials").

---

## 8. Skills that carry this judgment

| Skill | Holds |
|---|---|
| `calculation-selection` | Which calculator answers which question; where the semiempirical tier stops |
| `reactivity-descriptors` | Reading Fukui rankings and frontier orbitals without over-claiming |
| `ionization-and-partitioning` | pKa for ranking, never for a pH; the amine gap; the amphoteric trap |
| `computational-evidence` | Compute vs. retrieve; combining both into one cited answer; recording it as a note |
| `safety-screening` | Unchanged rule, extended: computation never clears a hazard either |
| `experiment-design` | Descriptor pre-ranking before a campaign is framed |
