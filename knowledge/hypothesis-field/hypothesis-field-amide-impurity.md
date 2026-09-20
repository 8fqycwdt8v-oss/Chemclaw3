---
artifact_refs: []
calc_refs: []
confidence: 0.5
created_by: agent
id: hypothesis-field-amide-impurity
relations:
- confidence: 0.5
  rel: cites
  to: rxn-amide-edc
source: seed-corpus
tags:
- amide-coupling
- hypothesis
type: hypothesis-field
---

Competing explanations generated and ranked for: where is the late-eluting impurity in the EDC
amide coupling coming from?

**Next: repeat the coupling in sieve-dried DMF and record the water content** (proposed; the
impurity drops below 2% if water is the cause, and is unchanged if it is not)

**The field does not separate.** The leading hypotheses sit inside their own uncertainty, so the
order below is not yet evidence — it says which comparisons have been run, not which explanation is
right.

1. **Adventitious water is hydrolysing the O-acylisourea** — 1712 ± 94 over 4 comparison(s)
   - refuted if: the impurity persists after the solvent is dried and the water content is below 50 ppm
   - mechanism: the activated ester hydrolyses readily and DMF is hygroscopic
   - objection: no run on file records a water content, so this is untested rather than supported
   - to settle in the lab: repeat in sieve-dried DMF and record the water content
2. **The amine base is degrading at the reaction temperature** — 1605 ± 88 over 4 comparison(s)
   - refuted if: the impurity is unchanged when DIPEA is replaced with 2,6-lutidine
   - to settle in the lab: swap the base and hold everything else
3. **The product decomposes during the concentration step** — 1183 ± 101 over 4 comparison(s)
   - refuted if: the impurity is absent when the concentration is run below 30 C
   - to settle in the lab: concentrate below 30 C

_8 comparisons; 1 candidate rejected for having no usable refutation condition._

---

_Ratings are on the Elo scale and come from pairwise judgements of these hypotheses against each
other, seeded with retrieved evidence. A rating is a preference ordering over this field — not a
probability that a hypothesis is true, and not evidence about the chemistry. Only the experiments
settle that._

Seed content: the chemistry is illustrative, the shape is what matters — a field that records the
alternatives considered and how they placed, so a later session can see what was ruled against
rather than only what was chosen.
