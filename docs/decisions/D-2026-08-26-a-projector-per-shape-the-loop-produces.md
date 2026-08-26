# D-2026-08-26-a-projector-per-shape-the-loop-produces — the four multi-step results become queryable records

## Status

Accepted. Completes `D-2026-08-26-the-envelope-is-not-the-result`, which routed the calc bundle's
jobs correctly and left four of their nine result shapes with nowhere to route *to*.

## Context

`D-2026-08-25-the-loop-is-a-composite-not-a-template` added seven durable jobs whose results are
new shapes: `RefinedEnsemble`, `EnsembleProperty`, `SpeciesDistribution` and
`BondDissociationSurvey`. `PAYLOAD_PROJECTORS` had an entry for none of them, so once the envelope
fix let those jobs reach the publish path at all, they reached it and were dropped — a job that
costs a Hessian per conformer, publishing nothing.

That was declared rather than hidden: the previous change named all four in
`tests/test_publish_reaches_the_hooks.py::_NOT_YET_PUBLISHED`, an exclusion set with the explicit
property that *a shape not named there must route*. This empties it.

## Decision

Four projectors, each mapping the model onto the fact shape that already fits it rather than onto a
new one. Where a choice existed, what decided it:

**A refined ensemble's `energy_hartree` is the electronic energy, not the Gibbs energy** — even
though the refinement ranks by G. `ConformerFact` holds one absolute energy, and the electronic one
means the same thing in both ensemble shapes, so "the same conformer, E-weighted and G-weighted" is
a comparison on one column instead of two that silently differ. The free energy is not lost: it is
what `relative_kcal` and `population` express, with `TheoryLevel.treatment` naming the weighting.
The per-member *absolute* G is the one thing not published, and that is stated in the projector
rather than left to be discovered.

**The refined entropy and correction keep their `refined_` names.** `RefinedEnsemble` renamed them
because they are computed over the subset and renormalized within it — the ensemble-wide names mean
something else one model away — and publishing them under the shared names would put two meanings in
one column. This is the model's own argument, carried across the boundary instead of being undone
at it.

**A Boltzmann-averaged property lands on the same registered name a single-point calculation of it
lands on.** `dipole` is `dipole` whether or not an ensemble was averaged; `members_averaged`,
`population_covered` and the treatment are what say an average was taken. A `dipole_averaged`
beside `dipole` is precisely the registry split
`test_no_two_properties_of_one_dimension_land_on_the_same_subject` exists to catch.

**The spread of an averaged property is not published.** `WeightedValue` carries min, max and
spread, each in the averaged property's own unit — debye here, eV there, dimensionless for a Fukui
index — so one registered `property_spread` would have no canonical unit, and a companion name per
property is the bloat the registry exists to avoid. The mean is what the job was asked for;
`population_covered` says how much of the ensemble stands behind it.

**A ranked species set is `candidates`, not subject members.** A subject's members are what the
calculation was *about*; these are what it *produced* — an open-ended set whose length is the
enumeration's. `CandidateFact` is the shape for exactly that, and it shipped with the schema with
**no producer at all**; this is its first. The subject is the enumeration as a `system`, so a
compound's tautomer set cannot collide with the compound.

**A bond is a `SiteFact` pair.** `atom_j >= 0` is the pair representation a bond order already uses.
A `PropertyFact` per bond would be the cardinality mistake `SiteFact`'s docstring argues against —
dozens of bond rows in the table that answers "pKa between 4 and 6". The weakest bond is *also*
hoisted to a calculation-scope scalar, because "which bond breaks first" is the question the survey
exists to answer and should not need a window function.

Thirteen registry rows land with them, and an unmappable average is a `ProjectionError` rather than
a value stored under the tool's own vocabulary: an unregistered name is refused at write time
anyway, and a made-up one puts a value nobody can find beside values they can.

## Consequences

Every shape either bundle can hand the publish path now routes:

| | before the review | after the envelope fix | now |
| --- | --- | --- | --- |
| primitive calculators publishing | 8 of 10 | 9 of 10 | 9 of 10 |
| durable jobs publishing | 1 of 11 | 6 of 11 | 10 of 11 |

`_NOT_YET_PUBLISHED` is empty and **kept**, which is the point: an empty exclusion is what makes the
next unroutable shape fail loudly rather than be added to a list nobody re-reads. Verified by
deleting one projector — three assertions turn red, in the routing test, the envelope test and that
shape's own.

The eleventh is `bo`, still stamping `CampaignResult` with no projector. That is a question rather
than a gap and it keeps its backlog row: a campaign is an optimization outcome rather than a
computed value, and `schema/result-store/` is molecule/reaction/ensemble-shaped. Either write the
projector or say in `publish/README.md` that a campaign deliberately does not publish; what is not
acceptable is a state where the two readings are indistinguishable from the code.
