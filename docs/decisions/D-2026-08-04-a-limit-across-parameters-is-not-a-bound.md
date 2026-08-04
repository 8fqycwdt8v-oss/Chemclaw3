# D-2026-08-04-a-limit-across-parameters-is-not-a-bound — A limit across parameters is not a bound

**Status:** accepted · **Date:** 2026-08-04 · **Implements:** W4 of
D-2026-08-04-what-bofire-does-when-you-actually-run-it · **Extends:**
D-2026-08-04-a-trade-off-has-no-single-best-point (the campaign-id identity)

## Context

`Domain(constraints=…)` was never passed. So "keep base plus acid under 3 equivalents" had to be
smuggled into a bound — which cannot say it, because a bound on `base` and a bound on `acid` admit
the corner where both are at their maximum — or dropped. The tool told the model to say so, and
probe `op-17` graded it on doing that honestly. Meanwhile the mixture case (three fractions summing
to 1) was unreachable entirely, and `DEFERRED.md` carried a PMI/E-factor objective blocked on
exactly the formulation space a simplex constraint describes.

## The measurement that sized the wave

M-3 asked four things, and the one that mattered was not about SOBO. **`initial_candidates` uses
`RandomStrategy`, and it seeds every cold-start campaign.** Had it ignored `Domain.constraints`, the
schema would have claimed a limit was honoured while every first point violated it — and a chemist
would have found out in the lab, not from an error. Measured:

- `LinearInequalityConstraint(coefficients=[1,1], rhs=3)` means **a + b ≤ 3** (20 of 20 random
  points satisfied it; none satisfied the reverse);
- `RandomStrategy`: **0 violations of 20**;
- SOBO: **0 violations of 5**;
- an equality put **10 of 10** random points exactly on the simplex;
- BoFire *itself* refuses a constraint naming a categorical feature.

So no rejection-sampling path is needed, and our validator exists for the *message* rather than the
safety.

## Decision

**One `LinearConstraint{kind, parameters, coefficients, relation, rhs}`, covering `<=`, `>=` and
`==`.** A discriminated union of five constraint types would be the single biggest comprehensibility
regression available to an LLM-facing schema, and the linear family is the only one any *continuous*
story asks for.

**`>=` is a negation, and the negation is pinned by a test.** BoFire has no `>=` class, so
`a + b >= 3` becomes `-a - b <= -3`. Getting that backwards would silently invert a limit the
chemist stated — the optimizer would return points *below* a floor they asked to stay above, with no
error anywhere. It is the one bug in this wave that produces a confidently wrong experiment rather
than a failure, so the test asserts the relation the **caller** wrote, not the one BoFire received.

**A limit on one parameter is that parameter's bound, and is not a constraint.** Writing "T under
80 °C" as a constraint is a worse way to say the same thing; the tool and the skill both say so,
because the schema now permits it and would not complain.

**Two shapes, not one, and not five.** The linear form is continuous only, because that is what
BoFire's linear constraint accepts. The one thing a category list genuinely cannot say is a
forbidden *pairing* — "never Pd(OAc)₂ in DMSO", where each option is fine alone — so
`ExcludeConstraint{kind, parameters, options}` joins it, discriminated on `kind`. Two members is
what `kind` was put there for; a forbidden option on its own is still just one left out of the list.

**An exclusion needs an all-categorical problem, and the message says which parameter broke that.**
Measured: BoFire refuses it on a domain holding any continuous input, because it applies the
constraint by enumerating the search space. `can only be used for pure categorical/discrete search
spaces` names nothing the caller wrote; our validator names their continuous parameters and offers
the two real ways out (fix them to levels, or filter the suggestions yourself).

**The mixture case ships as a mechanism, not as a concept.** Three fractions summing to 1 is
`relation: "=="` and cost nothing. There is still no formulation dataset in this repository, so
nothing in the skill mentions formulations: `DEFERRED.md`'s row is about the *objective*, which
needs data, not about the constraint, which needed a field.

**A factorial screen refuses a constrained problem — and the roadmap was wrong about why.** The
roadmap said the exclusion would be "expressible for a screen"; M-4 had measured it against
`SoboStrategy` and `RandomStrategy` and never against `FractionalFactorialStrategy`. Measured now
(M-4c/M-4d), that strategy rejects **every** constraint class at construction, linear and exclusion
alike: `constraint <class '…LinearInequalityConstraint'> is not implemented for strategy
FractionalFactorialStrategy`. So a screen was never going to silently violate a limit — it was going
to fail with a pydantic error naming a BoFire class. `factorial_design` keeps its refusal, because
the refusal is the *message*: it raises where the caller can act, with the two honest alternatives
(drop the constraint and filter the runs yourself, saying that you did, or use the tool that does
honour it). One sentence of the plan's rationale did not survive the measurement, and one claim of
its scope did not survive either.

**The note gains a "Subject to:" block.** "Searched over:" describes a box; the moment a constraint
reaches the durable path the campaign searched a polytope, and a reviewer reading only the bounds
would believe a corner was available that never was. Same defect D-157 fixed one field over.

**`constraints` joins the campaign identity when non-empty**, by the rule W3 established: a
constraint narrows the space, so a constrained campaign is a different campaign from the
unconstrained one over the same bounds — its runs mean something different. An unconstrained problem
still hashes to the id it had before either wave, pinned against `campaign-6958b7edaa261c83`.

## Consequences

- Story 3.3 is fully served: ranges, objectives and constraints. The tool description, which W3 left
  half-stale on purpose, no longer refuses anything it can do.
- `tests/test_bo_tools.py`'s description test has now broken deliberately twice — once per wave —
  which is what it is for. Its docstring records all three states, so the next person to change that
  prose sees why the assertion is written in both directions.
- **`discrete_candidate_count` had to change, and finding that out is what the tests were for.** It
  returns the product of the category counts, and two callers act on it: `initial_candidates`
  refuses an `n` above it, and `space_exhausted` calls a campaign finished by it. An exclusion
  removes whole cells — a 2×2×2 space minus one forbidden pairing holds **six**, not eight — so
  shipping the constraint without touching that count would have let a campaign loop keep asking for
  points that cannot exist until BoFire's discrete acquisition raised mid-run, which is the exact
  failure the count exists to prevent. It now enumerates the feasible cells when, and only when, an
  exclusion is present; the enumeration is affordable because this is the same space a unique-seeding
  loop already walks one point at a time. `ExcludeConstraint.forbids` is the single definition of
  "excluded", so the accounting and any later filter cannot drift apart.
- `op-17` keeps its refusal grading and is **not** rewritten. It asks for a *coupled* constraint
  ("Pd never above 2 mol% whenever the temperature is over 90 °C"), which is conditional rather than
  linear and remains unrepresentable. The linear half of that probe's question is now serviceable and
  the conditional half is not, which is a distinction the probe's own direction text already draws.
- Nonlinear constraints stay refused (`BotorchOptimizer` does not support them; it would take pymoo
  and a worse acquisition optimizer), as do `NChooseK` and interpoint constraints. `DEFERRED.md`
  carries all three with their triggers.
- `space_exhausted` and `MIN_SEED_OBSERVATIONS` themselves are untouched: a constraint changes how
  large the feasible space is, not how the loop reasons about running out of it. What changed is the
  number they are handed.
