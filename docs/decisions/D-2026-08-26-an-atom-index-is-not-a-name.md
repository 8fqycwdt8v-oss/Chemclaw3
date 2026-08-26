# D-2026-08-26-an-atom-index-is-not-a-name — Reactivity descriptors: the site is named, the panel is free, the binary refuses

**Status:** accepted · **Date:** 2026-08-26 · Written as a concept and implemented in the same
change; *What the implementation measured* at the end records what building it found. Sits beside
`D-2026-08-26-a-torsion-is-named-not-indexed`, which is the same argument for a bond, and under
`D-2026-08-16-the-physics-leaves-the-cache-stays` (what may be a server).

## The question

*"Can tblite or xtb calculate atom-specific reactivity indices? If yes, which — and how does the
agent get at them in a way that answers a chemist's question?"*

## What already exists, read rather than assumed

`servers/calc` serves `predict_site_reactivity` (condensed Fukui f⁻/f⁺/f⁰ from three single points)
and `compute_electronic_properties` (Mulliken charges, Wiberg bond orders, HOMO/LUMO/gap, dipole).
`skills/reactivity-descriptors` holds the judgment for reading them, and it is good.

So the physics was already right. Running it is what showed that the *answer* was not.

## Gap A — the correct answer arrives unreadable

`predict_site_reactivity("Oc1ccccc1", "electrophilic")` reproduces the textbook pattern for an
activating substituent when the ring carbons are compared with each other:

| ring position | f⁻ | rank in the list the agent receives |
| --- | --- | --- |
| C4 *para* | +0.0845 | **6 of 13** |
| C1 *ipso* | +0.0665 | 9 |
| C6 *ortho* | +0.0515 | 10 |
| C2 *ortho* | +0.0427 | 11 |
| C3 *meta* | +0.0409 | 12 |
| C5 *meta* | +0.0353 | 13 |

The *para* carbon — the answer — is sixth, behind the hydroxyl oxygen and four hydrogens. Both
*meta* carbons rank **below four hydrogens**. An agent that reads the top of the ranking and reports
it says "the oxygen and several C–H positions", which is a non-answer to the question asked.

`top_n` is not the missing knob. Its default is 15 and phenol has 13 atoms, so the agent already
receives every row and still cannot find the answer. **What was missing is a name and a scope**, not
a length.

The result says `index=4, element=C`. The chemist asked about *para*. Nothing in either repository
mapped between the two: `describe_topology` returns counts, and the skill instructed the model to
"name the top sites by their position in the molecule rather than by bare atom index" — which asks
it to do a graph analysis in its head, from a SMILES string, with no tool. That is
`D-2026-08-26-a-torsion-is-named-not-indexed`'s defect exactly, one dimension down: there, `(4, 5)`
was the amide C–N of one writing of acetanilide and an aromatic ring bond of another, with no error
anywhere.

## Gap B — a spread the size of the signal, reported as chemistry

Grouping every atom by its RDKit topological symmetry class and measuring the spread *inside* each
class:

| molecule | class | mean f⁻ | within-class spread |
| --- | --- | --- | --- |
| toluene | ring C *ortho* | +0.0394 | **0.0000** |
| toluene | ring C *meta* | +0.0369 | **0.0000** |
| toluene | methyl H | +0.0981 | 0.0432 |
| phenol | ring C *ortho* | +0.0471 | **0.0088** |
| phenol | ring C *meta* | +0.0381 | 0.0056 |

Toluene's ring is clean to four decimals. Phenol's is not, and the cause is real rather than
numerical: its O–H is planar, so one *ortho* carbon is *syn* to it and the other *anti*. The split
is **0.0088**. The *ortho*-to-*meta* difference a reader would draw a conclusion from is **0.0090**.

So the output supported the sentence "C6 *ortho* (0.0515) beats C2 *ortho* (0.0427)" — a statement
about an O–H rotamer, presented as chemistry — while being unable to say that *para*, with a margin
four times the spread, is the one conclusion that holds.

**The spread across a symmetry class is a free, first-party error bar.** It is computed from atoms
the calculation already ran, it needs no second conformer, and nothing was reading it.

## Gap C — three single points run, two of their energies discarded

`compute_fukui` ran three SCFs and its inner helper read `result["charges"]` and nothing else. The
ion *energies* were thrown away. Reading them gives vertical ΔSCF quantities on a fixed geometry:

```
IP = E(N-1) - E(N)        EA = E(N) - E(N+1)
mu = -(IP+EA)/2    eta = IP - EA    S = 1/eta    omega = mu^2 / 2eta
```

and with those in hand every local descriptor is one multiplication: local softness `s±(k) = S·f±(k)`,
local electrophilicity `omega(k) = omega·f⁺(k)`, dual descriptor `Δf(k) = f⁺−f⁻`. Free valence comes
from the Wiberg matrix `compute_properties` already returns.

No fourth SCF. `tests/test_reactivity_panel.py::test_the_panel_costs_no_extra_single_point` pins the
count at three, because "free" is what justifies computing these at all and the cheap way to lose it
is a fourth call added by someone who did not know the energies were already in hand.

## The decision

**A site is a value with a name, and a symmetry class rather than an atom.**

`servers/chem` gains `describe_sites` — one entry per symmetry-distinct heavy atom, carrying a
content-addressed `site_id` (the `torsion_handle` construction: RDKit build, canonical SMILES,
symmetry class), a chemist's `label`, the ring relationship, the functional-group `kind`, the
hydrogen indices a calculator will have put its numbers on, and the `scopes` the site answers. Free,
structural, `read_only` — so "which positions would you even be comparing?" is answerable *before* a
plan is approved, which is where that question belongs.

Grouping by class is the load-bearing half. Toluene's two *ortho* carbons are one question asked
once, and reporting them separately invites a comparison between two atoms that are the same atom.

**Scope replaces truncation.** `ring_carbons`, `ch_sites`, `heteroatoms`, `electrophilic_carbons`,
`all`. The fix for Gap A is the right rows, not more rows.

**Three tiers, ordered by cost, and each honest about what it needs.**

| Tier | What | Cost |
| --- | --- | --- |
| 0 | `describe_sites` — structural labels | free, no SCF |
| 1 | global ΔSCF panel + local descriptors + free valence | free, SCFs already run |
| 2 | `compute_atomic_descriptors` — polarisability, C6, coordination number, atomic multipoles, ESP extrema | needs the `xtb` binary |

**A key derives without the binary; only computing refuses.** `calculation_key` exists so a caller
can ask what a calculation *would* be stored under before committing to it, and deriving an identity
is not running one — so an `xtb.atomic` key derives on a binary-less deployment naming `xtb-absent`,
exactly as the two CREST searches already do. The refusal belongs where it is actionable. Getting
this backwards was caught by `test_deriving_a_key_runs_no_scf`.

**Tier 2 refuses rather than approximates.** Measured against tblite 0.7.0, the exposed result
properties are `energy`, `gradient`, `virial`, `charges`, `bond-orders`, `dipole`, `quadrupole`,
`orbital-energies`, `orbital-occupations`, `orbital-coefficients` and `density-matrix` — and nothing
else. There is **no overlap matrix and there are no atomic multipoles**, so there is no in-process
fallback to fall back to (and, separately, no Mulliken-condensed frontier density either: that
needs `S`). With no binary the answer is a `ValueError` naming the missing program and saying what
still works without it. Not a payload of nulls: a caller cannot tell "this deployment has no xtb"
from "this atom has no polarisability" by looking at a null, and the first is an operator fact while
the second is a chemical claim.

**Fukui indices are deliberately not taken from the binary**, although `--vfukui` computes them.
`compute_fukui` already answers that question and a second implementation would be a second answer
to it — the failure `connectors/README.md` records as two live definitions of `predict_pka`. They
are not even the same quantity: measured, xtb reports all three indices *negative* for phenol where
the finite-difference definition here reports them positive, because it differentiates charge where
this differentiates population.

**`CALCULATION_EPOCH` moves to `"2"`, in both repositories in the same change.** No stored number
moved — the same three SCFs run on the same geometry — but every epoch-1 row is now *incomplete*,
and the new fields are required, so one cannot come back validating as a panel it never carried.
That is precisely the case the constant documents, and `tests/test_calc_payload_schemas.py` is what
made it unmissable.

## What the implementation measured

- **The `xtb` binary was available after all.** This environment ships without it, which had been
  read as "Tier 2 cannot be verified here"; `apt` carries 6.6.1. Every format in Tier 2 is therefore
  transcribed from a captured run rather than from documentation — which mattered, because two
  things would have been guessed wrong: the per-atom polarisability table is printed on **stdout**
  and appears nowhere in `xtbout.json`, and an `--esp` run **aborts with SIGABRT after writing the
  grid and before writing the JSON**, so a surface calculation cannot also deliver the atomic
  multipoles. The second is why `surface=True` costs a second SCF, stated in the docstring as a
  measurement rather than a preference.
- **The two backends agree on partial charges to four decimals** (phenol's oxygen: −0.3948 from
  tblite, −0.39480 from the binary). Nothing forces that — one is a Fortran binary's stdout table,
  the other a Python library's array — so it is the strongest available check that the parser lines
  its rows up with the right atoms. It is a test.
- **`GetDefaultValence` is the wrong RDKit call for a free valence.** Sulfur's default is 2, so a
  sulfone's sulfur — using 4.94 of Wiberg bond order — came out at a free valence of **−2.94**, a
  number that reads as a strongly saturated atom and means nothing. `GetValenceList` answers `[2, 4,
  6]` there, and no member of it is "the" normal valence, so an element with more than one now gets
  `None` rather than a negative number.
- **Rounding a derivation separately from its inputs makes a panel disagree with itself.** `f_zero`
  was rounded from the raw difference while `f_minus` and `f_plus` were rounded independently, so a
  caller recomputing `(f_minus + f_plus)/2` from the two reported numbers got a different fourth
  decimal. Every derived value is now computed from the rounded inputs.
- **Naphthalene caught a labelling bug the aromatics did not.** A ring-fusion carbon was being
  described as "bearing the CH substituent" — naming a bond that is not there — and the classical
  *ortho*/*meta*/*para* names were being applied to a fused ring where the positions are alpha and
  beta. A fusion is now named as a fusion and refuses the classical names, as does any ring with two
  heteroatoms: pyrimidine is *numbered*, not related, and applying both conventions at once produces
  locants that disagree with IUPAC while looking exactly like locants that do not.
- **A single-reference ring position cannot separate the two chlorines of 2,4-dichloropyrimidine**,
  because both are *ortho* to a ring nitrogen. What separates them is that one sits **between two**,
  so `adjacent_ring_heteroatoms` is carried explicitly rather than left to be inferred from a
  relationship that cannot express it.

## What this deliberately does not do

- **No cross-molecule Fukui comparison.** Each Fukui function sums to 1 by construction.
- **No rates, yields or selectivity ratios.** The output is an ordering with an error bar.
- **No IUPAC ring numbering.** Reproducing it needs a direction and a substituent-priority rule, and
  a locant that is subtly wrong reads exactly like one that is right. A relationship to a *named*
  reference is reported instead.
- **One conformer.** The class spread exposes conformer noise; it does not remove it. A flexible
  molecule wants a Boltzmann-weighted profile over `search_conformer_ensemble`, which is a durable
  job on this side, not a tool over there.

## The open claim, and the mechanism that settles it

Local electrophilicity `omega(k)` carries a global scale factor, so it is the one quantity in the
panel with any chance of ranking sites *across* molecules — which is what a covalent-warhead
question needs. Measured, omega is 3.24 eV for phenol, 3.52 for *N,N*-dimethylacrylamide and 3.74
for pyridine: directionally plausible, on an absolute scale that is demonstrably wrong (GFN2's
vertical ΔSCF puts phenol's IP about 5 eV high).

That claim is not settled here and this corpus cannot settle it. The mechanism already exists —
`report_measurement`, `calculator_trust`, `calculator_outliers` — so omega ships as a ranking
quantity to be calibrated against measured reactivities, rather than argued for in a docstring.
