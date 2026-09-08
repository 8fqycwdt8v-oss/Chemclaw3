---
artifact_refs: []
calc_refs: []
confidence: 0.7
created_by: agent
id: playbook-route-scoring-is-not-per-step
relations: []
source: 10.1021/jacs.6c08434
tags:
- playbook
- route-design
- retrosynthesis
type: playbook
---

## A synthetic route is not judged one step at a time

Read a proposed or historical route as a whole. Do not rank routes, and do not dismiss a step, by
scoring each intermediate and differencing the result.

AbSynth digitized 3,000 classical total syntheses — close to 60,000 steps — and measured three
things about that habit (JACS 2026, `10.1021/jacs.6c08434`):

- Known complexity metrics and scoring functions **do not vary monotonically** along real
  synthetic trajectories. A route a chemist judged sound goes down as often as up.
- Conventional bond-disconnection heuristics fire only sporadically, and are not specific enough
  to automate.
- **Nearly half of all steps build no skeletal complexity at all** — protections, deprotections,
  functional-group interconversions, redox adjustments — and are strategically essential anyway.

So a step that adds nothing to the skeleton is not evidence of a bad route, and a monotone
improvement in a complexity score is not evidence of a good one. What justifies a step is usually
what it sets up several steps later, and that is only visible over the whole sequence.

**What this does not say.** The corpus is academic total synthesis of complex natural products,
where novelty and step count are the objectives; a process route is judged on cost, safety,
throughput and impurity control instead, and nobody has measured whether process routes plateau the
same way. The ~50% is their figure over their corpus — do not quote it as ours. What transfers is
the negative result: the non-monotonicity is a property of the scoring functions themselves rather
than of the target class, and it holds wherever one of them is used to rank a sequence.

Confidence here is about that transfer, not about the finding. The measurement is over 3,000 routes
and is not in doubt.
