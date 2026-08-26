# Concept — atom-addressable reactivity

**Status:** proposal, not implemented. No code changed.
**Question it answers:** the fleet computes correct per-atom reactivity numbers and the agent
still cannot answer "where does this nitrate?". This says why, and what to build.

---

## 1. The gap, measured

Run today's `predict_site_reactivity("Oc1ccccc1", mode="electrophilic")`. The physics is right:

| ring position | f⁻ |
|---|---|
| C4 *para* | +0.0845 |
| C1 *ipso* | +0.0665 |
| C6 *ortho* | +0.0515 |
| C2 *ortho* | +0.0427 |
| C3 *meta* | +0.0409 |
| C5 *meta* | +0.0353 |

*para* > *ortho* > *meta* — textbook EAS for an activating group. Now what the agent
actually receives, in rank order over all 13 atoms:

```
index= 0 O   f-=+0.1593      <- the lone pair
index=10 H   f-=+0.0990
index= 9 H   f-=+0.0894
index=11 H   f-=+0.0883
index=12 H   f-=+0.0875
index= 4 C   f-=+0.0845      <- the answer, at rank 6
index= 8 H   f-=+0.0843
index= 7 H   f-=+0.0707
```

The *para* carbon is rank 6. The two *ortho* carbons are ranks 10 and 11. Both *meta*
carbons rank **below four hydrogens**. An agent that reads the top of the list and reports
it says "the oxygen and several C–H positions", which is a non-answer to the question asked.
The default `top_n` of 15 does not help — the molecule only has 13 atoms, so the agent
already sees everything and still cannot find the answer.

Three distinct gaps sit behind that:

### 1.1 Addressability
The result says `index=4, element=C`. The chemist asked about *para*. Nothing anywhere in
either repository maps an atom index to a chemist's name for that atom. `describe_topology`
returns counts, not labels. The skill already instructs the agent to "name the top sites by
their position in the molecule rather than by bare atom index" — it is asking the model to
do a graph analysis in its head, from a SMILES string, with no tool. That is exactly the
kind of silent-failure step this codebase removes elsewhere.

### 1.2 Discrimination — and a noise floor nobody reports
Symmetry-equivalent atoms should carry equal indices. Measured, over RDKit topological
symmetry classes:

| molecule | class | mean f⁻ | **within-class spread** |
|---|---|---|---|
| toluene | ring C *ortho* | +0.0394 | **0.0000** |
| toluene | ring C *meta* | +0.0369 | **0.0000** |
| toluene | methyl H | +0.0981 | 0.0432 |
| phenol | ring C *ortho* | +0.0471 | **0.0088** |
| phenol | ring C *meta* | +0.0381 | **0.0056** |

Toluene's ring is clean to four decimals. Phenol's is not — its OH is planar, so the fixed
conformer makes one *ortho* carbon *syn* to the O–H and the other *anti*. That split is
**0.0088**, and the *ortho*-to-*meta* difference the agent would report is **0.0090**.
The signal and the geometry noise are the same size.

So today's output supports "C6 *ortho* (0.0515) beats C2 *ortho* (0.0427)" — a statement
about an OH rotamer, presented as chemistry. And it cannot say that *para* (margin 0.037,
four times the spread) is the one solid conclusion.

**The spread across a symmetry class is a free, first-party error bar.** It is computed from
atoms the calculation already ran, it needs no second conformer, and it is the natural
noise floor for every ranking claim. Nothing currently reads it.

### 1.3 Coverage — three SCFs run, two energies thrown away
`compute_fukui` runs three single points (N, N−1, N+1) and its inner `charges()` helper reads
**only** `result["charges"]`. The two ion *energies* are discarded. Reading them gives vertical
ΔSCF ionization potential and electron affinity, and from those the entire global panel:

```
IP = E(N-1) - E(N)      EA = E(N) - E(N+1)
mu = -(IP+EA)/2         eta = IP - EA        S = 1/eta        omega = mu^2 / 2eta
```

Measured on the existing three SCFs, zero additional compute:

| molecule | IP | EA | μ | η | ω |
|---|---|---|---|---|---|
| phenol | 13.52 | 3.00 | −8.26 | 10.52 | 3.24 |
| *N,N*-dimethylacrylamide | 13.48 | 3.38 | −8.43 | 10.10 | 3.52 |
| pyridine | 13.59 | 3.66 | −8.62 | 9.93 | 3.74 |

(eV. Absolute values are uncalibrated — GFN2 vertical ΔSCF puts phenol's IP ~5 eV high — so
these rank a series, they do not measure an IP. §6 says what to do about that.)

And with the global set in hand, every **local** descriptor is one multiplication:

```
local softness        s±(k) = S · f±(k)
local electrophilicity ω(k) = omega · f+(k)
dual descriptor       Δf(k) = f+(k) - f-(k)
free valence          F(k)  = V_max(element) - Σ_j W(k,j)   [Wiberg matrix already returned]
```

**No new SCF is required for any of it.** The whole global + local panel is post-processing of
single points already paid for — the same argument `compute_properties` already makes for
itself ("reading the result we were already discarding").

---

## 2. The idea: a site label is a value, not a sentence

Introduce **`SiteLabel`** — a chemist-readable identity for one atom, derived purely from the
molecular graph, costing nothing, and used as the **join key** for every per-atom number the
fleet produces.

```
SiteLabel
  index               int      # the existing contract: canonical-SMILES order, H appended
  element             str
  label               str      # "C4 (para to OH)" · "carbonyl C" · "benzylic CH2" · "aniline N"
  symmetry_class      int      # RDKit CanonicalRankAtoms(breakTies=False)
  ring                str|None # ring size + aromaticity, or None
  ring_position       str|None # ipso / ortho / meta / para, relative to the ranking substituent
  functional_group    str|None # SMARTS-matched group this atom belongs to
  role_in_group       str|None # "carbonyl carbon", "amide N", "leaving group"
  hybridisation       str
  attached_hydrogens  list[int]  # so a C-H question is asked on the carbon and answered there
  is_ch_site          bool
```

Two properties make this the right primitive rather than a formatting helper:

- **It is free and structural.** No SCF, no geometry, no network. Pure RDKit over the graph.
  So it is `read_only` and callable *before* a plan is approved — which matters, because "which
  positions would you even be comparing?" is a question a chemist wants answered before
  authorising minutes of CPU.
- **It is the join key.** Fukui indices, Mulliken charges, Wiberg orders, BDEs from
  `enumerate_bond_cleavages`, structural alerts from `safety`, highlight sets for
  `render_structure` — every one of these is already keyed by atom index. One label table makes
  them all speak about the same atoms in the same words.

### 2.1 The aggregate the agent should actually receive

Not one row per atom. **One row per symmetry class**, scoped to the question:

```
SiteProfile
  label            SiteLabel        # the class representative
  members          list[int]        # every atom in the class
  f_minus/f_plus/f_zero  float      # class MEAN
  dual, s_minus, s_plus, omega_local, charge, free_valence  float
  spread           float            # max - min within the class  == the noise floor
  resolved         bool             # does this class separate from the next by > spread?
```

`resolved` is the honesty flag, and it is computed rather than asserted. For phenol:
*para* → `resolved=True` (margin 0.037 vs spread 0.009); *ortho* vs *meta* →
`resolved=False`. The agent then says, correctly and without being told to:

> *Para* is the clear electrophilic site (f⁻ 0.085 vs 0.047/0.038 for *ortho*/*meta*).
> *Ortho* and *meta* differ by 0.009, which is inside this calculation's own 0.009 spread
> over chemically equivalent positions — this calculation does not resolve them.

### 2.2 Scope replaces truncation
`top_n` is the wrong knob — the phenol case returns every atom and still buries the answer.
Replace it with a **chemical scope** the question selects:

| scope | returns | answers |
|---|---|---|
| `ring_carbons` | aromatic/ring C only | EAS, SNAr regiochemistry |
| `ch_sites` | C–H carbons, H indices folded in | radical abstraction, CYP soft spots, C–H activation |
| `heteroatoms` | N, O, S, P, halogen | oxidation liability, metal binding, HSAB |
| `electrophilic_carbons` | C attached to O/N/halogen, sp² C=O/C=C–EWG | chemoselectivity, Michael acceptors |
| `all` | everything, class-aggregated | the escape hatch |

---

## 3. Where each piece lives

The split follows this family's existing boundary rules, not a new one.

| Piece | Repo | Why |
|---|---|---|
| `describe_sites(smiles) -> list[SiteLabel]` | **Chemclaw3-mcp**, `servers/chem` | Pure function of its argument, stateless, free, no SCF. `chem` is already the free-and-structural server and already `read_only` throughout. |
| Global ΔSCF panel + local descriptors on the existing three SCFs | **Chemclaw3-mcp**, `servers/calc` | A *primitive* whose identity is derivable from its inputs. Extends `compute_fukui`'s existing key — no new calc type, the SCFs are the same ones. |
| CM5 charges, atomic polarisability/C6, ESP surface | **Chemclaw3-mcp**, `servers/calc`, gated tier | Needs the `xtb` binary, which is **absent in this environment** (`xtb: command not found`; `binary_version()` returns `"absent"`). Must degrade to "not available here", never silently omit. |
| `profile_reactivity(...)` — run, join on labels, aggregate classes, apply the noise floor | **Chemclaw3**, `connectors/calc` | Composition of primitives is orchestration, and the D-011 cache lives here. Each primitive stays separately keyed, so a second scope on the same molecule is a re-aggregation, not three more SCFs — the same split `fukui_inputs` already makes for `mode`. |
| Which descriptor answers which question; how to phrase the caveat | **Chemclaw3**, `skills/reactivity-descriptors` | Judgment is layer 3. The skill stops asking the model to do graph analysis in its head and starts telling it how to read `resolved`. |

The one **composite** temptation to refuse: do not ship a `predict_regiochemistry` tool that takes
a reagent and returns a product. Its key would name its own output. It is a durable job at most,
and more honestly it is a skill composing the primitives above.

### 3.1 Closing the loop visually
`servers/chem` already serves `render_structure(smiles, highlight_atoms)`. With labels as the
join key, the agent's answer becomes a ranked table **plus the molecule drawn with those exact
atoms highlighted**. That is the difference between a number and something a chemist can check
at a glance — and it needs no new capability, only the index set the profile already carries.

---

## 4. Use cases

Ordered by how directly the existing tree already supports them.

### Already served by the primitives — unlocked by labels alone
1. **EAS regiochemistry** — nitration, halogenation, sulfonation, Friedel–Crafts. f⁻ over
   `ring_carbons`. The flagship case, currently broken by presentation.
2. **SNAr regiochemistry** — which chlorine of a 2,4-dichloropyrimidine goes first. f⁺ over
   ring carbons bearing a leaving group. A daily process-chemistry question.
3. **Chemoselectivity between two electrophiles** — which of two esters aminolyses, which
   ketone reduces. f⁺ over `electrophilic_carbons`, with `resolved` deciding whether the
   answer is "this one" or "these are comparable; sterics will decide".
4. **Radical C–H abstraction site** — f⁰ over `ch_sites`. Pairs with the existing
   `bond-strength-and-radicals` skill: Fukui gives the polar term, BDE the thermodynamic one,
   and the two disagreeing *is* the interesting result.
5. **Acidic proton attribution** — cross-check `predict_pka`'s chosen site against f⁻ on the
   conjugate base. Two independent methods agreeing is evidence; disagreeing is a flag.

### Compositions with capabilities that already exist
6. **Rank enumerated degradants.** `chem.enumerate_degradants` proposes transforms; each
   transform acts on a named atom; the profile ranks them. Turns an unordered hypothesis list
   into a triaged one. Feeds `skills/degradation-liabilities`.
7. **Oxidative-liability screen.** HOMO + f⁻ on `heteroatoms` — amine N-oxidation, thioether
   S-oxidation — plus benzylic `ch_sites`. The forced-degradation study you would run, predicted.
8. **Downgrade a structural alert.** `servers/safety` flags a genotoxic alert as a substructure
   match. The alert atom's f⁺ and local electrophilicity say whether it is electronically
   *activated* or deactivated by its neighbours. A structural alert with a dead electrophile is
   the commonest false positive in that screen.
9. **Metabolic soft spot (CYP).** f⁰ over `ch_sites`, weighted by accessibility. Same machinery
   as (4), different framing, and it lands in `skills/degradation-liabilities`.
10. **Side-product hypotheses.** Feed the top f⁻/f⁺ sites to `rxnpredict` as the attacked atom,
    and compare with what the ELN actually recorded via `reaction_records`.

### Descriptors that only become usable once the free arithmetic is added
11. **Covalent warhead tuning.** Local electrophilicity ω(k) at the β-carbon of an acrylamide is
    *the* accepted descriptor for warhead reactivity, and unlike raw Fukui it carries a global
    scale factor — so it is the one quantity in the panel with a *chance* of ranking across
    molecules. High value, and the claim needs calibrating (§6).
12. **Ambident nucleophile regiochemistry (HSAB).** Local softness s⁻(k) on an enolate (C vs O),
    thiocyanate (S vs N), nitrite (N vs O). Hard reagent → hard end. Classic, and unanswerable
    from f⁻ alone because it needs the global softness scale.
13. **Cycloaddition regiochemistry.** Dual descriptor Δf on both partners gives the
    large-with-large / small-with-small pairing rule for Diels–Alder *ortho/para* selectivity.
    Needs two profiles joined — a natural composed job.
14. **Ligand and catalyst electronic tuning.** Charge and HOMO on the donor atom across a
    phosphine or NHC series, labelled by donor identity rather than by index.
15. **Metal-binding / chelation site.** Softness plus charge on `heteroatoms` — which nitrogen
    of a polydentate ligand binds a soft metal.

### Cross-layer, where the descriptors stop being an answer and become a feature
16. **BO campaign features.** Atom-condensed descriptors as continuous features for
    `science/bo`. A substituent series has a natural descriptor axis, and a campaign that
    optimises over it is doing QSAR with physics instead of fingerprints.
17. **SAR narrative.** "Why did the 4-F analogue win?" — the descriptor delta at the labelled
    position, fed to `skills/campaign-narrative-synthesis`. The label is what makes the sentence
    sayable.
18. **Retrospective ELN explanation.** An ingested entry recorded an unexpected regiochemistry.
    Profile it and ask whether the descriptors would have predicted it. Every such case is a
    calibration datapoint, free, from data already in `reaction_records`.
19. **Publish as scientific record.** A descriptor panel is a typed result — it belongs in
    `publish/` and the result-store schema, not only in `calculation_results`, which by its own
    design refuses any predicate on the payload. "Find me every compound whose most
    electrophilic carbon is a nitrile" is a query the cache structurally cannot answer.
20. **Knowledge-graph evidence.** A labelled, resolved profile is exactly the shape
    `skills/computational-evidence` wants to cite — a claim, its number, its error bar, and its
    `calc_key`.

---

## 5. What this deliberately does not do

- **No cross-molecule Fukui comparison.** Each Fukui function sums to 1 by construction. The
  `SiteProfile` schema should make this structurally hard, not merely warn about it.
- **No rates, yields, or selectivity ratios.** The output is an ordering with an error bar.
- **No reagent, no solvent, no sterics.** A site can be electronically preferred and sterically
  unreachable. `resolved=False` is the honest output far more often than it is comfortable.
- **No new expensive path.** Everything in Tier 0 and Tier 1 is free. The moment this concept
  requires a fourth SCF it has drifted.
- **One conformer.** The class spread exposes conformer noise; it does not remove it. A
  genuinely flexible molecule wants `search_conformer_ensemble` and a Boltzmann-weighted
  profile — which is a *durable job*, on the Chemclaw3 side, not a tool here.

---

## 6. The open question, and the mechanism that settles it

Use case (11) claims local electrophilicity ranks *across* molecules. Measured ω is 3.24 eV for
phenol, 3.52 for *N,N*-dimethylacrylamide, 3.74 for pyridine — directionally plausible, and on an
absolute scale that is demonstrably wrong (IP 5 eV high).

This repository already owns the mechanism for that: the **calibration ledger** —
`report_measurement`, `calculator_trust`, `calculator_outliers`. The honest position is to ship
ω as a ranking quantity, register it as a calibratable calculator, and let measured warhead
reactivities decide whether the cross-molecule claim survives. Not to argue it in a docstring.

Which is the rule this whole concept follows: the phenol result above was not an opinion about
presentation. It was one script.
