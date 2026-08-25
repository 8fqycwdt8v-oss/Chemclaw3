# D-2026-08-25-the-loop-is-a-composite-not-a-template — multi-step GFN protocols, and where the fan-out lives

**Status:** accepted · **Date:** 2026-08-25 · Builds on
`D-2026-08-16-the-physics-leaves-the-cache-stays` (primitives move, composites are decomposed) and
`D-2026-08-21-a-geometry-is-an-address-not-a-payload` (a geometry crosses as a handle).

## Context

Almost every question a chemist asks about a flexible molecule takes more than one calculation, and
this tree answered exactly one of them end to end: `data/templates/conformer-refinement.yaml`.
Everything needed for the rest already existed and was unused. `_species_energy` is already
*embed → (thorough: CREST → lowest) → relax → Hessian → G with the conformational-entropy
correction* for one SMILES. `structure_id` already carries a chosen geometry into the next
calculation. `docs/guides/xtb-skill-catalogue.md` already enumerates twenty-eight skills and marks
nineteen gated on capability that has since shipped.

Two things were genuinely missing, and one thing was broken.

**Missing: a species set.** `chemclaw.core.chem.standardize` *collapses* a tautomer set, because it
answers "is this the same compound?". Nothing in either repository produced the opposite — the set a
ratio, a speciation profile or a bond survey is computed *over*. No tautomer enumeration, no
protonation microstates, no stereoisomer expansion, no bond homolysis.

**Missing: the arithmetic over a set.** `ensemble_from_members` weights by electronic energy, which
D-101 recorded as a deliberate limit ("one Hessian per member, half an hour each at 76 atoms").
Nothing weighted by free energy, and nothing averaged a *property* over an ensemble at all.

**Broken: the searches could not run.** `Chemclaw3-mcp/servers/calc/Containerfile` installed neither
`xtb` nor `crest`, so `require_crest()` refused every conformer, tautomer, protomer, deprotomer and
non-covalent search. This repository removed the same two binaries in the physics split and handed
the GPL-3.0 distribution question to that repository, where it had not been taken. `sample_conformers`,
`compute_interaction_energy`, `compute_reaction_energy(level="thorough")` and the one shipped QM
template were all dead at runtime.

## Decision

**Three tiers, and one rule that places every piece of work: the loop lives in the job spec, the
sequence lives in the template.**

Templates deliberately have no conditionals, no loops and no expressions
(`templates/README.md`), and the agent's own loop is capped at `harness_max_loop_iterations`. A
pKa over N microstates or a survey over N bonds is a fan-out and fits neither. So:

1. **Primitives** stay individually keyed on `Chemclaw3-mcp`. Two were added: `compute_fukui_at`
   (closing the `DEFERRED.md` row whose trigger was already written) and the seven structure
   enumerations, which went to `chem` rather than `calc` because they are not calculations — pure
   maps from one SMILES to a set of SMILES, no `calc_version`, nothing to cache.
2. **Composites** hold the fan-out, in `connectors/calc/compose.py`: `refined_ensemble`,
   `ensemble_property`, `species_ranking`, `bond_dissociation_survey`. Each reaches parts that are
   separately cached and has no key of its own.
3. **Protocols** are four new `XtbJobSpec` union members — a spec member, a dispatch branch, an
   optional result field and a `jobs:` entry each, with **no new Temporal workflow type** — and
   seven `data/templates/*.yaml` files that sequence them.

**The enumerations are `read_only`, and that is load-bearing rather than tidy.** The plan gate
refuses `state_changing` tools under an unapproved plan. An enumeration is what *decides* which
species the expensive machinery runs on; gating it would put the human in front of the question
rather than in front of the cost. The agent enumerates freely, and approval sits on the search that
runs once per species it hands back.

**A budget preflight, not a longer clock.** Every fan-out counts its remote primitives before
making the first call (`science/calc/budget.py`) and refuses above `calc_max_primitive_calls` with
the count in the message. A `ValueError`, so `BAD_DATA_RETRY` treats it as non-retryable and an
over-budget request fails in the first second rather than three times over four hours.
`xtb_job_timeout_seconds` was deliberately **not** raised: it is one number shared by every calc
job, and sizing it for the worst fan-out degrades failure detection for the two-second reaction.

## What was measured rather than assumed

Verified against CREST 3.0.2 and xtb 6.5.1 installed from conda-forge, running the shipped engine:

| | measured |
|---|---|
| n-butane conformer search, `--quick` / `--normal` | 46.7 s → **4** conformers · 189.7 s → **2** |
| ibuprofen (33 atoms), `--quick` | **1142 s** → 13 conformers, 111 rotamers |
| `--tautomerize` / `--protonate` / `--deprotonate` with `crest` present, `xtb` absent | all three exit non-zero in **~0.1 s**: `binary: "xtb" / status: not found!` |
| the same three with `xtb` present | 30.2 s / 3.5 s / 0.7 s, correct member counts |
| system libraries `crest` needs beyond its own tree | `libgfortran5`, `liblapack3`, `libblas3`, **`libgomp1`** |

Four findings follow, each of which prose would have got wrong:

**CREST alone does not turn on three of the five searches.** It implements tautomer, protomer and
deprotomer sampling as legacy driver scripts that shell out to the **`xtb` binary**, not through its
own internal tblite. `require_crest(search)` now refuses per search and names the binary that is
actually missing, instead of sending an operator to install one they already have. It also means
RDKit enumeration on `chem` is not the cheap alternative to a CREST proton search — on the image
this fleet builds, it is the only route there is.

**`ldd` on a workstation gives three library names and ships a broken image.** A developer machine
already has `libgomp1`; `python:3.11-slim` does not, and the failure is at *run* time from a build
that succeeded. The layer now runs `crest --version` inside the same `RUN`, turning that class of
omission into a failed build. This was found by building the image, not by reading `ldd`.

**Running the proton searches found two defects nothing could have caught.** `--protonate` writes
`protonated.xyz` and the map read `protomers.xyz`, so the search failed as "wrote no ensemble file"
after terminating normally in 3.5 s; and `_read_ensemble` reused the *input* structure's element
list, which is right for xtb (it echoes the same atoms) and wrong for a search whose entire purpose
is to change the atom count — `--deprotonate` on phenol returned 12-atom structures against a
13-atom input and raised `12 positions for 13 elements`. Both were unreachable code paths until the
binaries existed.

**A tautomer ranking without a conformer search inverts the textbook case.** Running the whole
chain — `chem`'s enumeration into a GFN2 relaxation per tautomer, Boltzmann-weighted — puts
acetylacetone at **99.9% keto**. It is roughly **80% enol** in the gas phase. The enol's stability
is an intramolecular hydrogen bond present in one planar conformer and absent from the others, so a
ranking that never searches conformational space cannot see what makes the enol favourable, and the
answer it gives is not imprecise but backwards. `run_tautomer-resolution` therefore ranks at
`level="thorough"` — a conformer search per tautomer, the most expensive default in the catalogue.
A cheap tautomer ranking that inverts the answer is worse than none, because every downstream
number then describes the wrong form with nothing indicating it.

**The sampled-count trap fired again, on schedule.** n-butane returns **4** conformers at `--quick`
and **2** at `--normal`; the textbook answer is 2 and the quick pass simply dedupes less. D-101
recorded this exact failure ("passed twice and returned 4 on the third run"), and it is why nothing
in the new tests asserts a member count.

## Two things that looked obvious and are not

**A unified `state_change()` over pKa, redox, BDE and tautomerisation was not built.** Traced, the
four share only argument marshalling: BDE and tautomerisation are already `reaction_energy`, redox
adds one constant, and pKa needs a *fitted* linear free-energy relationship that lives in the
server's `pka.py` config and whose ±1.6/±1.0 accuracy is the only measured number in this area.
Building it would have put a second, competing pKa predictor in this repository. `species_ranking`
by contrast has three simultaneous callers at ship time — tautomers, microstates, stereoisomers,
differing in nothing but the SMILES set and the label — so it is real and the other was an
abstraction with one caller in disguise.

**"logD over an ensemble" was not built.** Crippen logP is a two-dimensional atom-contribution sum,
conformer-independent by construction; the only conformer-sensitive half of logD is a pKa whose
calibration was fitted on single-conformer energies, so averaging it applies a fit outside its own
domain. The gap `logd.py` actually names is *microstates* — amphoteric and polyprotic molecules —
and that is what `run_microspecies-profile` addresses.

## Consequences

- **`crest` ships behind `--build-arg INCLUDE_CREST=true`, default off**, because it is GPL-3.0 and
  distributing it in an image is the product owner's decision. Adding it orphans nothing:
  `CrestSpec.calc_version()` answered `crest-absent`, so no CREST row could exist.
- **`xtb` is a separate and larger decision, deliberately not folded in.** `resolve_backend()`'s
  `auto` default would find it on PATH and move **every** `calc_version` that names a backend, so
  two pods would derive different keys for one molecule and every cached row would be orphaned. An
  operator who wants it adds it *and* pins `CHEMCLAW_XTB_ENGINE`.
- **`CALCULATION_EPOCH` is not bumped.** Every new capability is a new `calc_type` or a new params
  value and writes rows that cannot collide with old ones; a defensive bump would discard every
  cached CREST search, the most expensive thing in the system.
- **The enumeration tools return container models with a hoisted `smiles` list.** Measured:
  `templates/resolve.py` walks a dotted *attribute* path with no indexing, so a bare
  `list[Tautomer]` is unreachable from a template — `${steps.forms.result.smiles}` raises
  `UnresolvedReference`. `ConformerEnsemble.lowest_structure_id` is the precedent and the same
  argument.
- **`make template-validate`'s `unchecked_arguments` count rose from 1 to 5**, because `chem` is a
  bundle this repository declares and does not run, so its tools are name-checked and
  argument-unchecked. That is the known gap, stated rather than discovered; `make
  connector-validate` against a running server is what covers it.
- **The harness is on for the `computation` profile alone, at `plan_only`.** One turn can now
  commission six conformer searches, and `plan_only` puts a chemist in front of the plan before the
  first of them. Not globally, and not at a higher autonomy — `harness_max_loop_iterations` was
  sized for a chat agent, which is a second reason the fan-out is a composite rather than a chain of
  turns.

## References

- `docs/decisions/D-101-x5-x6-x7-the-binaries-and-what-they-change.md` — the free-energy weighting
  this closes, and the sampled-count trap it predicted.
- `docs/guides/xtb-skill-catalogue.md` — the nineteen gated skills these protocols unblock.
- `Chemclaw3-mcp/servers/calc/README.md` — the binary layer, the per-search requirement, and the
  measurements above.
