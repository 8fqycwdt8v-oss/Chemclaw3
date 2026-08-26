# D-2026-08-26-a-torsion-is-named-not-indexed — Rotational profiles: the bond is chosen by enumeration, the barrier is a composite

**Status:** accepted · **Date:** 2026-08-26 · Written as a concept, implemented in the same change,
then **run against the real GFN2 server**, which found two defects a synthetic surface could not
express — see *What the live server said* near the end, which also corrects this ADR's own claim
that doing so needed a cluster. Sits under
`D-2026-08-16-the-physics-leaves-the-cache-stays` (what may be a server), under
`D-2026-08-25-the-loop-is-a-composite-not-a-template` (what may be a template), and under
`D-2026-08-21-a-geometry-is-an-address-not-a-payload` (what crosses a wire).

## The question

*"Rotational energies for a compound, and the barrier between its rotamers — and especially, how
does the user tell the agent which bond to rotate?"*

**"Rotational" here means internal rotation about one bond**, not the rigid-rotor rotational
partition function that `science/calc/thermo.py` already computes for entropy. The two share a word
and nothing else; a `symmetry_numbers` argument is about the second, and this concept is about the
first. Worth stating once, because a molecule's "rotational energy" is ambiguous in exactly the
place where both meanings are implemented in the same module.

Concretely, three answers are wanted about one compound:

1. **The profile** — energy as a function of the torsion angle about a named bond.
2. **The rotamers** — the minima of that profile, their relative energies and their populations
   at a temperature.
3. **The barrier between them** — how much it costs to interconvert, and what that implies:
   a rate, a half-life, and for a hindered biaryl or amide, an atropisomer classification.

## What already exists, read rather than assumed

- **`scan_coordinate`** — a durable job on the `calc` bundle. `ScanJobSpec` takes `smiles`,
  `atoms` (2–4 indices), `values`, `solvent` and an optional `structure_id`.
- **`compose.scan_profile`** drives each point as a separately-keyed `scan_point` primitive on the
  remote calculator, each driven from the *input* geometry rather than the previous point, so the
  profile does not depend on the direction it was walked. A re-run with two extra values pays for
  two points.
- **`ScanResult`** carries the points, `minimum_value`, `maximum_relative_kcal` and
  `minimum_structure`.
- **`science/calc/thermo.py`** has `boltzmann_populations` (with degeneracy),
  `free_energy_populations`, `thermochemistry_from_hessian`, `ensemble_entropy`.
- **`publish/project.py::_scan`** projects a profile into the result store as a `PointFact` series.
- **The judgment already exists and is good**: `skills/conformational-analysis` on reading a
  torsion profile, `skills/atropisomer-assessment` on turning a barrier into a half-life and an
  ICH class, including the Eyring anchors and the honesty rules.

So the physics, the caching, the durability and the judgment are all in place. **Two things are
missing, and they are the two the question names.**

## Gap A — there is no way to say which bond

The only way to name a torsion today is four 0-based atom indices, and
`connector.yaml` says so plainly: *"Give two atom indices for a bond, three for an angle, four for
a dihedral (0-based, as RDKit numbers them)."*

No chemist has those numbers. So one of two things happens: the request stops, or **the model
supplies them** — it has the SMILES, it can count, and it is disposed to be helpful. The second is
the dangerous one, and the reason is not that it would fail.

### The measurement

RDKit 2026.3.5, the version `pyproject.toml` pins:

```python
from rdkit import Chem
AMIDE = Chem.MolFromSmarts("[CX3](=O)[NX3]")
a, b = Chem.MolFromSmiles("CC(=O)Nc1ccccc1"), Chem.MolFromSmiles("c1ccc(NC(C)=O)cc1")
```

| | the amide C–N is | those two integers in the other writing are |
|---|---|---|
| `CC(=O)Nc1ccccc1` | `(1, 3)` | — |
| `c1ccc(NC(C)=O)cc1` | `(4, 5)` | `C4–C5`, **aromatic, in a ring, really bonded** |

The same compound, two ways of writing it, and one pair of integers that names the amide bond in
one and a ring bond in the other. `scan_profile` validates `max(atoms) < len(elements)` and nothing
else — a bounds check, not an identity check — so that scan runs, converges, and returns a
well-formed profile with a plausible barrier for a question nobody asked. **This is the
`audit_events.agent` shape** (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`):
not a failure, an answer that is quietly about something else.

A second measurement, on the obvious fix — *"then let the enumerator be RDKit's rotatable-bond
list"*:

| | `CalcNumRotatableBonds` | what it leaves out |
|---|---|---|
| toluene | **0** | the methyl top |
| *p*-xylene | **0** | both methyl tops |
| *tert*-butylbenzene | **0** | the Ar–C(CH₃)₃ rotation |
| acetanilide | **1** | **the amide C–N** — it counts only N–aryl |

The rotatable-bond count is a druglikeness descriptor. It excludes terminal tops (`!D1`) and
amides by definition — which is to say it excludes both classes of bond people actually ask
barriers about. `describe_topology` reports that count and `publish/properties.py` publishes it as
`rotatable_bonds`; neither is wrong, and neither is the list wanted here.

## Gap B — a barrier is not a first-class result

`ScanResult.maximum_relative_kcal` is the highest point of the profile relative to its *global*
minimum. That is one number where the question has several, and the difference is not cosmetic:

- **The wells are not identified as rotamers.** Every scan point is a *constrained* geometry, so
  `minimum_structure` is a frozen-dihedral point handed on as if it were a minimum. A rotamer is
  what you get after releasing the constraint.
- **A barrier has a direction.** Between two rotamers, ΔE‡(A→B) ≠ ΔE‡(B→A) unless they are
  degenerate, and the one that decides shelf-life is the barrier out of the *populated* well.
- **Populations are not computed**, and cannot be without degeneracies — which is exactly what
  `EnsembleMember.degeneracy` warns about: n-butane's anti is 59.2% with them and 73% without.
- **ΔE‡ is not ΔG‡**, and the atropisomer classification is defined on ΔG‡.
- **The half-life is left to the model.** `skills/atropisomer-assessment` gives the Eyring relation
  and a table of anchors and asks for the arithmetic in prose. One kcal/mol is a factor of ~5 in
  t½ and the method's error is larger than one kcal/mol — this is the last place in the chain that
  should be done in a model's head.
- **The three profile pathologies the skill asks a human to spot by eye** — a discontinuity where
  a point relaxed into another basin, a broken symmetry, a maximum that was stepped over — are all
  checks a program can run, and **a check nobody runs is a check that does not exist.**
- **Refining a coarse profile is a loop.** The skill says "rescan the interesting range more
  finely"; the agent's own loop is iteration-capped and a template has no loops.

## Decision

Three pieces, each landing on the side of a boundary this repository has already drawn.

### 1 · `enumerate_torsions` — a free, pure enumeration on `chem`

The sixth member of a family with five members already (`enumerate_tautomers`,
`enumerate_protonation_states`, `enumerate_stereoisomers`, `enumerate_bond_cleavages`,
`enumerate_degradants`), and it earns its place by the same rule they do: *"enumerate, then compute
— and never the reverse"*, and *"the enumeration tools are free; only the ranking costs anything"*.
It is a pure graph operation with no calculation, no cache and no `calc_version`, so by
`D-2026-08-16` it is a **primitive** and lives in `Chemclaw3-mcp`'s `servers/chem`, with its name
added to `connectors/chem/connector.yaml` here as a `read_only` tool. That file's own comment states
the two-repository consequence: the server's copy is authoritative and `make connector-validate`
against a running server is the check that catches drift.

One entry per candidate torsion:

| field | why it is there |
|---|---|
| `torsion_id` | the handle — see below |
| `atoms` | the four indices defining the dihedral, chosen canonically (heaviest substituent each side) |
| `bond` | the two indices of the bond itself |
| `label` | what a chemist calls it: "the amide C(=O)–N", "the biaryl axis", "the *tert*-butyl top" |
| `kind` | `amide` · `biaryl` · `ester` · `ether` · `benzylic` · `alkyl` · `top` |
| `smarts` | the environment that matched, so the label is checkable rather than trusted |
| `in_ring` | a refusal criterion, not information — see *What this does not do* |
| `symmetry_order` | 3 for a methyl top, 2 for a symmetric biaryl, 1 otherwise |
| `period_degrees` | `360 / symmetry_order` — the range a scan actually needs |
| `degenerate_with` | other `torsion_id`s that are the same chemistry by symmetry |

`symmetry_order` does two jobs and is the reason the enumerator returns more than a list of index
pairs. It **shrinks the scan** — a methyl needs 120°, not 360°, which is a third of the points —
and it **supplies the degeneracy** that `boltzmann_populations` already requires and that nothing
in the tree currently produces. Without it a rotamer population has to be guessed, and the n-butane
number says what guessing costs. `degenerate_with` keeps *p*-xylene from being scanned twice.

Because the rotatable-bond descriptor measurably omits the two classes most asked about, this tool
defines its own candidate set — every acyclic single bond between two non-hydrogen atoms with at
least one substituent on each side — and reports the tops as `kind: top` rather than dropping them.

### 2 · `torsion_id` — a content-addressed handle, and the answer to "which bond"

**The handle is derived, never stored**: canonicalize the molecule, take RDKit's canonical atom
ranking, and hash `(canonical_smiles, sorted canonical ranks of the two bond atoms)` — the same
shape as `structure_id` under `D-2026-08-21-a-geometry-is-an-address-not-a-payload`, and for the
same reason: *an address, not a payload*.

Measured over three writings of acetanilide and two of 2-methylbiphenyl, the handle is identical in
every writing while the indices are not:

| writing | bond atoms | handle |
|---|---|---|
| `CC(=O)Nc1ccccc1` | `(1, 3)` | `tor_d139107cd84f9333` |
| `O=C(C)Nc1ccccc1` | `(1, 3)` | `tor_d139107cd84f9333` |
| `c1ccc(NC(C)=O)cc1` | `(4, 5)` | `tor_d139107cd84f9333` |
| `Cc1ccc(C)cc1` | `(0, 1)` | `tor_7b6b88fe5991e188` |
| `Cc1ccc(C)cc1` | `(4, 5)` | `tor_7b6b88fe5991e188` |

The last two are one molecule's two methyls: symmetry-equivalent bonds share a handle, so p-xylene
is one question rather than two. These literals are the cross-repository contract —
`tests/test_torsion_handle.py` here and `servers/chem/tests/test_torsion_handle_contract.py` there
assert the same table, so whichever side moves first turns red instead of the two quietly
disagreeing.

What follows, each clause being a defect it prevents:

- **Rewriting the SMILES does not change it**, so a bond named in turn 1 is the same bond in
  turn 6. Indices are not, and the failure is silent.
- **It is resolved back to indices against the structure being computed**, not carried across a
  wire as integers whose meaning depends on an embedding.
- **The handle carries the RDKit build**, in the hashed payload, because the canonical ranking
  is a function of that build. This is `D-2026-08-16`'s `calc_version` lesson applied one level
  down: a handle presented after a toolchain bump must **fail to resolve loudly** rather than
  resolve to a different bond quietly. A handle is a within-answer address; it is never a cache key
  and never persisted as an identity.
- **It names an input**, so it does not make the enumeration a composite.

### 3 · How the user actually names the bond

Four accepted forms, **one resolution path** — every one of them ends at a `torsion_id`, and
nothing else reaches the scan:

1. **In words** — *"the amide bond"*, *"the biaryl axis"*, *"the bond to the tert-butyl"*. The
   agent enumerates (free, one call) and matches against `label`/`kind`. Then, and this is the
   whole rule: **one match → proceed and name it back; several → ask, listing them; none → say so,
   listing what there is. Never guess.**
2. **By picture** — `render_structure` exists on `chem` today; the concept adds an overlay option
   that draws the enumerated torsions with their labels. This is what makes the choice *checkable
   by a human* rather than merely stated, and it is the form to reach for when the words were
   ambiguous.
3. **By SMARTS** — for a chemist who wants precision. Matched *against the enumerated set* rather
   than used directly, so a pattern hitting three bonds produces a choice rather than an arbitrary
   first match.
4. **By handle** — a `torsion_id` from an earlier answer, which is how *"now do the same in
   toluene"* stays on the same bond across turns.

Raw atom indices remain accepted as an expert escape hatch. They stop being the only door, and the
answer always names back what was chosen — *"the amide C(=O)–N, atoms 1–3"* — so a wrong bond is
visible in the reply instead of only in the number.

**No new confirmation machinery.** `scan_coordinate` is already `state_changing`, so the plan gate
already stands between an unapproved plan and the spend. What changes is that the plan step a human
approves reads *"scan the amide C–N of acetanilide"* instead of `atoms: [4, 5, 6, 12]`, which is
the difference between a gate and a formality.

### 4 · `profile_rotation` — a durable composite on `calc`, here

**Not an MCP server**, because its key would name the wells it settles on: the textbook
*composite* of `D-2026-08-16`, *"not shipped at all: it is decomposed, and this repository composes
the parts so every step is cached."* **Not a template**, because it loops over wells and passes,
and *"a template has deliberately no loops"* (`D-2026-08-25-the-loop-is-a-composite-not-a-template`).
So: a tenth member of the `XtbJobSpec` union, `RotationJobSpec`, on the one `CalcJobWorkflow` the
other nine already share.

```
kind: "rotation"
smiles, torsion_id (or atoms), solvent, temperature_k,
step_degrees   = 30 by default, over period_degrees rather than always 360
level          = quick | standard | thorough
structure_id   — the conformer to measure in; a barrier depends on which one
```

Stages, every computed point being a separately-keyed primitive that the D-011 cache already
fronts:

1. **Coarse profile** over `period_degrees` at `step_degrees`.
2. **Locate wells and passes** — pure arithmetic, `science/calc/`.
3. **Refine each pass**: rescan ±`step_degrees` finely, so the barrier is *resolved* rather than
   stepped over. This is the loop the skill currently asks a human to run.
4. **Release the constraint at each well** and optimize → a real rotamer, with its own
   `structure_id` to carry into any other calculation.
5. **`standard`**: a Hessian per distinct well → ΔG and free-energy-weighted populations, through
   `thermochemistry_from_hessian` and `free_energy_populations` unchanged. **`thorough`**: a
   Hessian at the passes too → ΔG‡, reported in a field named so it cannot be mistaken for an
   optimized transition state.
6. **Populations** over the wells, with degeneracy from `symmetry_order` and from mirror-image
   wells.
7. **Kinetics** — Eyring, as arithmetic in `science/calc/thermo.py` beside RRHO, by the same rule
   that kept RRHO here when the physics left: `rate_from_barrier`, `half_life_from_barrier`, and
   the inverse `barrier_from_half_life` (*"what barrier do I need for a two-year shelf life"* is a
   question a chemist asks). **Always with a band**, propagated from
   `xtb_reaction_uncertainty_kcal` through the exponential — reporting a single half-life from a
   semiempirical barrier is exactly the false precision `compute_ensemble_property`'s own
   description warns about, and here it spans two ICH classes.

The result, `RotationProfile`:

```
torsion       id, atoms, label, symmetry_order, period_degrees
points[]      the profile (ScanPoint, unchanged)
rotamers[]    degrees, structure_id, relative_kcal, population, degeneracy
barriers[]    from, to, forward_kcal, reverse_kcal, at_degrees,
              half_life_seconds + band, basis: "E" | "G" | "G(approx)"
warnings[]    non-smooth · unresolved maximum · broken expected symmetry ·
              a well that relaxed out of its basin · another torsion adjacent to this one
```

Cost is counted before anything starts, through `science/calc/budget.py::require_within_budget`,
as the four existing protocols do — the count is knowable up front (points + wells + passes, times
one Hessian each above `quick`), so an over-budget request fails in the first second rather than
three times over four hours.

### 5 · The judgment layer: two edits, one template, one projector

- **No new skill.** `skills/conformational-analysis` and `skills/atropisomer-assessment` already
  hold precisely the right judgment and would otherwise describe a manual procedure the job now
  performs. Atropisomer step 1 — *"Get the four atom indices that define the dihedral"* — becomes
  *"name the bond; `enumerate_torsions` gives the handle"*. The Eyring table stays as a reader's
  intuition; the number comes from the job with its band. Every honesty rule in both skills is
  unchanged, and the class range becomes computable from the band instead of estimated in prose.
- **A step template** `rotational-barrier.yaml` sequencing enumerate → confirm → profile → report.
  Loop-free, so a legitimate template, and it mirrors `tautomer-resolution.yaml`.
- **A projector** for `RotationProfile` in `publish/project.py`: rotamers as `ConformerFact`s, the
  profile as the `PointFact` series `_scan` already emits, and `rotational_barrier_kcal`,
  `interconversion_half_life_s` and `torsion_label` as properties. A barrier is a per-compound
  scientific number a site will want to query — which is what `D-2026-08-25-a-cache-is-not-a-record`
  built the seam for. Note that the *count* `rotatable_bonds` is already a published property while
  the barrier itself is not.

## What this deliberately does not do

- **No 2D surfaces.** Two coupled torsions is a different question, and
  `skills/conformational-analysis` already says a single scan does not describe it. The job warns
  when another rotatable torsion is adjacent to the one being driven, rather than pretending.
- **No transition-state search.** A constrained maximum is not a saddle point; the field name
  carries that, so no downstream reader can lose it.
- **No ring torsions.** `in_ring` is refused with a message naming the right question — a ring
  conformational search — on the same reasoning that makes `enumerate_bond_cleavages` skip ring
  bonds.
- **No enumeration inside the compute job**, matching `rank_species` and `survey_bond_strengths`:
  a bond that was not enumerated was not scanned, and that must be visible in the answer.
- **The barrier is not a measurement.** Unchanged from the skill: VT-NMR, chiral HPLC and
  racemization kinetics settle it; the calculation says whether they are worth running.

## What the implementation measured

Written as a concept and built in the same change, so the section that would have been "what to
check before this ships" is what checking it found.

**The symmetry-class equivalence is sound, and is checked rather than assumed.** Vertex orbits do
not determine edge orbits in general, so a handle built from a canonical *symmetry class* pair
could in principle merge two chemically different bonds. Measured over 21 molecules — fused,
symmetric, polysubstituted — against the real thing, the automorphism group of each molecule:
**zero false merges**. That comparison ships as a test rather than as this paragraph, so a
counterexample turns red.

**The candidate set had to be defined from scratch, and the numbers say why.** RDKit's
`CalcNumRotatableBonds` reports **0** for toluene, p-xylene and *tert*-butylbenzene and **1** for
acetanilide, and the one it excludes there is the amide C–N. Both exclusions are by definition
(`!D1` for terminal tops, an explicit amide clause), and both are exactly what a barrier question
is about — so `enumerate_torsions` enumerates every acyclic single bond between two heavy atoms
instead, and reports the tops it cannot give a heavy-atom dihedral for rather than dropping them.

**The symmetry order is worth its arithmetic.** Biphenyl and DMF come out 2-fold (a 180° period),
toluene's methyl 6-fold (60°) — measured against the shipped enumerator, and each halving or
sixfolding is that many constrained optimizations not run. The test counts a full turn against a
half turn rather than asserting it.

**The Eyring anchors in `skills/atropisomer-assessment` were wrong by up to two orders of
magnitude.** Its prose table read "27 → about a day" and "30 → a few years"; computed at 298.15 K
with a transmission coefficient of 1, 27 kcal/mol is **80 days** and 30 is **35 years**. That is
the strongest argument in this ADR for putting the arithmetic in code: the table sat in the one
skill whose whole purpose is mapping a barrier onto a regulatory class, and the error was largest
right at the class boundary. The four anchors are now pinned as literals in
`tests/test_calc_rotation.py`.

**The refinement earns its cost, and the release earns more.** On the test surface a coarse 30°
grid reads the barrier as a lower bound; refined, the height matches the analytic barrier to within
0.35 kcal/mol. And on a 45° grid the profile's own minima sit at 45, 180 and 315 while the released
rotamers sit at 60, 180 and 300 — so a composite that reported its scan points as rotamers would
hand back three geometries that are not minima. That is the claim releasing the constraint exists to
make, and the test is built on a grid where the two answers differ.

**And it found a defect in the seam it publishes through.** `CalcJobWorkflow` sends
`payload_kind=type(result).__name__`, and its result is `XtbJobResult` — a nine-optional-field
*envelope*. Measured: `projector_for("calc.compute_reaction_energy", "XtbJobResult")` returned
`None`, so **every one of that bundle's durable jobs published nothing at all**, and
`tests/test_publish_reaches_the_hooks.py` was green because its table asserted
`ReactionEnergyResult` — a `payload_kind` production has never sent. This is
`D-2026-08-26-a-route-is-not-a-shape`'s own finding surviving in the one bundle whose workflow
returns an envelope rather than a result.

**Found independently and fixed better on `main` while this was in flight** (#225): rather than
unwrapping at the projection boundary, which is what this branch first did,
`CalcJobWorkflow` now publishes the *member* — `XtbJobResult.outcome()` picks it out by type, so a
tenth result shape is one field on that envelope and nothing else. `RotationProfile` is that tenth
field and needed no edit anywhere else, which is the property worth having. This branch's own
unwrapping was deleted on the merge; what is recorded here is the measurement, not the fix.

## Risks, and what is still open

One thing this change does **not** settle. **How far a barrier moves with the conformer it is
measured in.** `structure_id` carries a chosen conformer in, and the default is a fresh embedding —
the cheap answer, and the right default. What is not known is the size of the error that default
costs on a flexible molecule, which is what a warning threshold should be set from rather than from
taste. It needs a CREST conformer search, and `crest` is a conda-only binary the fleet's image
cannot carry (`servers/calc/pyproject.toml` states why); so this one waits on a cluster rather than
on a decision.

## What the live server said — and the two defects it found

The item that stood here first — *"every number above is against a synthetic surface, not against
xTB"* — was **wrong about what this environment can do**, and correcting it is the most useful
thing in this ADR. `tblite` *is* the GFN2 Hamiltonian and ships as a PyPI wheel; only the `xtb` and
`crest` binaries are conda-only, which costs speed and the conformer search rather than the physics.
So the run happened: `servers/chem` on 8858 and `servers/calc` on 8860, the handle minted by the
real chem server over MCP, the profile composed against the real calc server, the Postgres cache in
front of it.

| | computed | independently known | |
|---|---|---|---|
| n-butane, gauche above anti | **0.62 / 0.63** kcal/mol | 0.6–0.9 | ✓ |
| n-butane, anti population | **59.1 %** | 59.14 % (this tree's own CREST anchor) | ✓ |
| n-butane, syn barrier at 0° | **5.15** kcal/mol | ~4.5–5.0 | ✓ |
| n-butane, anti↔gauche barrier | 2.65 | ~3.3–3.6 | GFN2 low |
| biphenyl, twist angle | **41.8°** | ~44° | ✓ |
| biphenyl, perpendicular barrier | **2.00** kcal/mol | ~1.6 | ✓ |
| biphenyl, planar barrier | 2.76 | ~1.4–2.0 | slightly high |
| DMA, amide rotation | **19.91** kcal/mol (ΔE‡), t½ 44 s | ΔG‡ ~15–18 | ✓ |

**Every barrier in that table was re-measured after
`D-2026-08-26-a-barrier-is-a-difference-between-two-numbers-measured-the-same-way`.** The values
first recorded here — 5.03, 2.53, 1.51, 2.27, 18.10 — were each understated by the lowest well's
relaxation energy, because the pass was measured from the profile's lowest *constrained* point and
the wells from the lowest *released* one. The correction is 0.12 kcal/mol on n-butane and
1.8 on DMA. The well depths, populations and the twist angle are unaffected: they never involved
the pass. **The conclusion that GFN2 runs low on n-butane's anti↔gauche barrier survives it** —
2.65 against 3.3–3.6 is still the method, not the arithmetic, and that was checked rather than
assumed.

The released wells landed at 64.0° and 296.1°, off the 30° grid the scan used — so releasing the
constraint measurably moves a well on real physics, which is what that stage exists for and what no
synthetic test can establish. Biphenyl used its 180° period and cost 14 points instead of 28.

**Two defects the synthetic surface could not express, both fixed here:**

- **A torsion with one well per period reported no barrier at all.** DMA's profile rises to
  18.1 kcal/mol at 96° and returns to its own symmetry image — one planar amide per 180° — and
  `barriers` came back **empty**: pairing adjacent wells around a ring produces a zero-length arc
  when there is only one of them, so the number was computed and then dropped. That is the shape
  the whole capability exists for, and the fake had three wells and never showed it. A zero-length
  forward arc now means the whole period, and `from_rotamer == to_rotamer` is a documented, real
  case rather than a bug.
- **The discontinuity check fired on exactly the molecules the feature is for.** It compared a step
  against `xtb_reaction_uncertainty_kcal` (3.0), and any barrier steep enough to matter steps
  further than that: DMA was warned that a point had "relaxed into a different basin" while it was
  climbing an ordinary amide barrier. A discontinuity is a step *out of line with its neighbours*,
  not a large step, so the rule is now a ratio to the profile's own typical step
  (`xtb_rotation_discontinuity_ratio`), calibrated against three measured smooth profiles whose
  largest steps were 3.5x, 2.7x and 2.5x their own median.

Both are pinned by tests that fail against the previous behaviour — verified by reverting each fix
in place and watching the new assertions go red.

## Consequences

- `Chemclaw3-mcp` gains one tool and this repository gains one manifest line; the two lists are
  not structurally coupled, so both halves land together or the validator reports it.
- The `XtbJobSpec` union gains a tenth member and the workflow histories in flight are unaffected —
  the union is discriminated on `kind` and additive.
- `science/calc/thermo.py` gains kinetics beside thermodynamics. That is still arithmetic over a
  result rather than a calculation, so the boundary `D-2026-08-16` drew does not move.
- **`atoms` stops being the primary way to name a coordinate** for torsions while remaining the
  only way for bonds and angles, which `scan_coordinate` still serves. That asymmetry is
  deliberate: the enumeration exists because a *torsion* has chemical identity a chemist can name,
  and a stretch of an arbitrary bond does not.
