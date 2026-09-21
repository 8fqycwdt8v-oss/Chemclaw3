# D-2026-09-21-a-design-is-a-criterion-not-a-second-tool — model-based and space-filling designs, folded into the tool that already pays for the schema

**Status:** accepted · **Date:** 2026-09-21 · Closes the `DEFERRED.md` row *Model-based optimal
design and true space filling*, whose row is deleted in this commit.

## Context

`generate_screening_design` enumerates the corners of a space and **refuses a problem that carries
a constraint**, saying so in its own message. That left the ordinary process-development ask with no
design at all: *"I have twelve runs, and base plus acid has to stay under three equivalents."* A
factorial cannot honour the limit; `suggest_next_experiment` answers a different question (where to
go next given results you already have); and a chemist with a budget and a constraint was told to
filter the corners by hand.

`DEFERRED.md` had this as **Model-based optimal design and true space filling**, and its trigger was
*"a story asking to design within a constrained continuous space with a stated run budget"*. That
row had already done the hard part: it recorded, measured on 2026-08-17, that the dependency
objection was false — SCIP ships transitively with `bofire[optimization]`, cyipopt is optional, and
both `DOptimalityCriterion` and `SpaceFillingCriterion` returned a design on a constrained
continuous domain with cyipopt absent. What survived was the use-case objection alone, and this ask
supplies the use case.

## Decision

**The capability is BoFire's `DoEStrategy`, and the surface is a `criterion` argument on the tool
that already exists.**

`science/bo/engine.optimal_design` wraps the strategy: `d-optimal`, `a-optimal`, `i-optimal` and
`space-filling` over a domain that carries the problem's constraints, filling a stated run budget
exactly. `OptimalDesign` is its return, the sibling of `ScreeningDesign`, carrying the three things
the rows cannot say for themselves — the `formula` the design is optimal *for*, `n_terms` against
the run count, and `duplicate_runs`.

### Why a criterion and not a second tool, decided by measurement

A standalone `generate_optimal_design` MCP tool was built first, and it cost **1,435 tokens**
against `MAX_SINGLE_TOOL_TOKENS = 900`, whose own assertion message says: *"Narrow the arguments or
paginate the result; do not add them to KNOWN_OVERSIZED to make this pass — that list is debt
already taken on, not a place to put more."*

Measured, **1,367 of those 1,435 were the `OptimizationProblem` input schema** — the same schema
`generate_screening_design` (1,395) and `suggest_next_experiment` already pay for. Trimming the
docstring twice took the tool from 1,733 to 1,435 and could not touch the rest, because the cost is
a nested union of parameter and constraint types, not prose. **No tool taking an
`OptimizationProblem` can be under that cap**, which is why its two siblings are in the debt list.

So the design family became an argument on the tool that already holds the schema. Folding it
passed both ratchets — the per-tool cap and the whole-prefix ceiling — with no raise at all, where
the standalone tool needed 1,147 tokens of ceiling. `criterion="factorial"` is the default, so the
shipped behaviour is byte-identical for every existing caller.

### Three refusals, two of them BoFire's absence rather than its behaviour

- **A budget below the model's term count is refused.** Measured on bofire 0.4.1: a
  `fully-quadratic` criterion over three continuous factors, asked for **3** runs against a 10-term
  model, returns three rows and no error. The information matrix is singular, so no coefficient is
  estimable and nothing in the frame says so — a chemist runs them, fits nothing, and concludes the
  chemistry is noisy. The refusal names the term count and a simpler formula, and says that a
  budget *exactly* at the term count leaves no residual degrees of freedom.
- **A budget passed to `factorial` is refused rather than ignored**, which is the rule this tool's
  docstring already applies to `n_center` and `n_repetitions`. A factorial's size is the product of
  its level counts; being handed 128 rows after asking for 24 is the failure.
- **A returned run outside a declared constraint is refused**, which turns this design's whole
  selling point from a sentence into a checked property. Added after CI failed where local passed:
  the first test asserted feasibility to 1e-6, and BoFire's DoE is a continuous optimization
  (SLSQP through `scipy.minimize`, cyipopt being absent here) that satisfies an active constraint
  to its own tolerance rather than exactly. Measured over 20 seeds x 4 criteria, the worst
  excursion is **7.5e-06** — arithmetic, not infeasibility. `_CONSTRAINT_TOLERANCE` is 1e-4: an
  order of magnitude above that and orders below anything a chemist can set, so a breach of it is
  real. BoFire warns "please check if the results lie within your tolerance"; now something does.
- **The term count is BoFire's own**, through `get_formula_from_string`, rather than the arithmetic
  re-derived here. The arithmetic is easy for continuous factors and that is exactly why: a
  categorical contributes one column per level *minus one*, so two definitions agree on the easy
  case and diverge on the case a chemist brings.

## Consequences

**The tool's name is now narrower than what it does**, and that is taken deliberately rather than
overlooked. `generate_screening_design` with `criterion="i-optimal"` and a quadratic formula
produces a response-surface design, which is not a screen. A rename touches 35 files and every
stored profile naming it, and buys only the name; the default is still a screening design and the
argument that widens it is explicit at the call site. A reviewer who disagrees has a mechanical
follow-up.

**Four criteria ship and three do not.** E, G and K optimality are absent deliberately: nothing
here can tell a chemist what they would buy over D, and a criterion offered without that is a coin
flip wearing a Greek letter. `DESIGN_CRITERIA` is the closed set.

**`ExcludeConstraint` reaches the design** for free, because the domain translation already carried
it; no separate work and no separate claim.

**What this is still not.** It carries no resolution and no alias structure, so unlike a fractional
factorial it cannot say which effects are confounded — what it offers instead is that every run is
feasible, and those are different guarantees. It is also not blocking or `NChooseKConstraint`: that
`DEFERRED.md` row stands, its premise about there being no plate concept now stale but its own
trigger unmet.

## Revisit when

A chemist asks for a design property none of the four criteria expresses — a blocked design, "at
most 3 of these 8 additives", or an alias structure over an optimal design. The first two are the
standing `NChooseKConstraint`/blocking row; the third would mean this design family has been
stretched past what `OptimalDesign.summary` can honestly say, and the file that would show it is
that model's `_clauses`, which currently ends by stating the absence.
