# D-2026-08-26-a-solvent-is-an-argument-not-a-job — a species ranking fans out over media like a reaction does, and the ranking is what publishes

## Status

Accepted.

## Context

The GFN2/ALPB stack takes a solvent nearly everywhere: eight of `Chemclaw3-mcp`'s `servers/calc`
primitives and all nine of this bundle's durable jobs carry one. `compare_solvents` fans a
*reaction* out over media and ranks them. But `rank_species` — the job that answers which tautomer,
microstate or stereoisomer dominates — takes `solvent: str | None`, one medium per run.

So "which tautomer dominates in water against toluene", which is among the most common questions a
process chemist asks of a calculation, was N jobs and a comparison the caller assembled by hand.
Worse, the comparison is not a diff of two payloads: `species_ranking` returns its forms sorted by
relative energy, so the same index is a different species in two media exactly when the ranking
reorders — which is the case worth detecting.

That reordering is the whole point. A pKa, a Fukui ranking, a dipole and a reaction free energy all
describe whichever form was drawn. If the major form changes with the medium, every one of those
numbers is about a different species depending on where it was computed, and nothing in the system
said so.

**This is also the calculation implicit solvation is best at**, which is why it is worth building
rather than approximating. Every medium ranks the same species — same formula, same atoms, same
level — so the continuum model's systematic error largely cancels in the relative energies. That is
not true of an absolute solvation free energy, which is why this ADR builds the ranking fan-out and
declines the per-compound ΔG_solv job that looks adjacent to it.

**Building it surfaced a second thing, and this ADR is not the one that fixed it.**
`SpeciesDistribution` had no entry in `publish/project.py::PAYLOAD_PROJECTORS` and no `calc_type`
prefix that reaches one, so `rank_species` published nothing at all. Measured at the time:

```
SpeciesDistribution        -> None
RefinedEnsemble            -> None
EnsembleProperty           -> None
BondDissociationSurvey     -> None
SolventComparisonResult    -> <function _solvent_screen>
```

Four of the then-nine calc jobs, silent by construction: `enqueue_payload` never raises and skips
an unknown shape with a debug line, deliberately, because a deployment legitimately holds rows from
calculators that no longer ship. So nothing distinguished "this release cannot read that shape"
from "that shape was never wired up". It is the same shape as
`D-2026-08-26-a-route-is-not-a-shape` — a composite's `calc_type` is `<connector>.<job>`, a route
that names no shape and matches no prefix — and it stood against
`D-2026-08-25-a-cache-is-not-a-record`'s claim that this seam "projects every result — primitive or
composite".

`D-2026-08-26-a-projector-per-shape-the-loop-produces` closed all four while this branch was in
flight, having found the same gap independently. What remains here is the consequence for this
change, recorded below: a distribution's projector is a *prerequisite* for a job that publishes one
per medium, so this could not have shipped on top of a part that published nothing.

## Decision

**`rank_species_across_solvents`**: `SpeciesSolventScreenJobSpec` with `solvents: list[str]` where
the ranking job has `solvent`, composed by `compose.species_solvent_comparison`, which is
`solvent_comparison`'s shape applied to a distribution. The gas phase is prepended as a reference
because "the medium barely matters here" is a real answer and invisible without one; the media run
under the existing `calc_screen_max_parallel` bound; the budget is counted over `species x media`,
because that product is what surprises — eight tautomers in five solvents at `standard` is 120
remote primitives from a request that looks like one call.

The result carries the per-medium distributions **whole** plus their transpose. The transpose earns
its place rather than being derivable clutter: it is keyed by SMILES, which is what makes it correct
across a reordering, and it is the row a chemist actually reads. `dominance_changes` is the headline
and `largest_swing_kcal` is checked against the method's uncertainty before anything is reported as
a difference — the same discipline `SolventComparisonResult` already applies.

**`_species_distribution` is added as a projector**, which fixes `rank_species` publishing as well,
and **`records_from_species_solvent_screen` emits the aggregate plus one distribution record per
medium** — `records_from_solvent_screen`'s rule, that an aggregate whose parts are not stored makes
the per-medium question answerable only over screens that happened to include that medium. The parts
are the distributions verbatim rather than reconstructed, because this composite holds them whole.

Eleven property names are registered rather than reused. `species_relative_energy` and
`species_population` exist because `relative_energy` and `population` are declared at *conformer*
scope and a species is not a conformer; reusing them would have put two meanings under one name in
the store this seam exists to make queryable.

**`tests/calc_server_fake.py` gains `solvent_shifts`**, a `(smiles, solvent) -> Hartree` map. The
fake's energy is a function of atom count alone, so without it every medium returns the same number
and `dominance_changes` — the one finding this job exists to report — could never be observed in a
test. A fake that cannot express the behaviour under test is not evidence for it
(`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`).

## What is declined, and why

**A per-compound solvation free energy job** — "in which solvent is this compound most stabilised".
Nothing computes one today and it is expressible as gas-phase against solvated single points. But
for one solute the answer is a solubility argument that `props`' Hansen `solvent_swap_candidates`
already makes from *measured* data, and ALPB's absolute solvation energies are much weaker than its
relative ones. Adding it would put a worse answer beside a better one under a more inviting name.

**pKa in a non-aqueous solvent.** `pka_solvent` is fixed at `water` and is one of seven calibration
settings folded into `calc_version`. A second solvent needs a calibration set that does not exist
here. It is a data problem, not a code one, and building the knob without the data would produce
numbers with a calibration silently borrowed from water.

## Consequences

- **The gap was real and somebody else closed it first.** While this branch was in flight,
  `D-2026-08-26-a-projector-per-shape-the-loop-produces` (#231) gave all four composites a
  projector *and* wrote the test proposed here — `tests/test_publish_reaches_the_hooks.py`
  parametrises over `XtbJobResult`'s own member fields and names unroutable shapes in
  `_NOT_YET_PUBLISHED`, now empty. Two branches finding the same silent gap independently is the
  strongest evidence available that it was one.
- **So this change keeps none of its own `SpeciesDistribution` projector.** Both existed in the
  merged file for a moment, and only the later definition would ever have run — a duplicate that is
  silent by construction, which is the failure mode this ADR's own subject matter is about. #231's
  is also the better projection: it publishes the ranking as `CandidateFact` rows (a shape that had
  existed with no producer since the schema shipped) with the species set as a `system` subject,
  where this one had made every form a subject member with per-member property facts. Deleted here,
  and `_species_solvent_screen` re-pointed at its vocabulary — `kind="system"`,
  `distribution_kind` — because a comparison whose subject kind differed from its own parts' would
  not join to them. Four property registrations went with it; `records_from_species_solvent_screen`
  needed no change, because it delegates to whatever projects a distribution.
- The job is `expensive: true` like its siblings, so it sits behind the same role gate.
- `compute_xtb_energy` still has no `solvent` argument while its docstring invites comparing "the
  same molecule in another solvent". It is reachable through `compute_electronic_properties`, which
  returns the total energy, so this is a prose defect rather than a capability gap — left for the
  change that touches that tool.
