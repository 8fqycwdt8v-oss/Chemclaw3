# D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate — The fraction lives where BoFire will fractionate

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-092 (`factorial_design`, the classical
screen beside the Bayesian strategies)

## Context

`factorial_design` built a `FractionalFactorialStrategy` at the default `n_generators=0`, which
returns the plain Cartesian product. Its docstring said exactly that, and the implementation plan
read it as a one-line fix: thread `n_generators` — already a field on the imported class — through
to the caller, and "96 wells, 7 factors" gets a resolution-IV design instead of a grid that does
not fit.

**The measurement refuted the plan.** BoFire's `FractionalFactorialStrategy` fractionates the
*continuous* half of a domain and crosses the categorical half in full:
`_get_categorical_design` enumerates every combination and never consults `n_generators`. Seven
two-level `CategoricalInput`s give **128 runs at every `n_generators` value that validates at all**
— 128 with 0, 128 with 1, 128 with 2. The parameter is a no-op on exactly the domain shape
`factorial_design` accepts, which is all-categorical by its own D-092 refusal.

## Decision

**Hand BoFire the factors in the form it will actually fractionate.** A reduced screen re-encodes
each two-level categorical factor as a `ContinuousInput` on [0, 1], lets BoFire build the
fractional design at those two bounds, and maps each bound back to its label. `n_center=0`, because
a centre point at 0.5 would decode to neither level. The full-grid path (`n_generators=0`) is
unchanged and still goes through the categorical domain.

**A fractional design is a two-level design, and a factor with a different number of levels is
refused.** Not quietly crossed in full: a three-level factor smuggled into a reduced screen would
make the reported resolution describe only part of the design — the same "looks complete while
omitting a factor" failure D-092's continuous refusal exists to prevent, one level up.

**The design reports its own resolution, and the resolution is computed rather than reported as a
run count.** A run count does not say what was given up. `_resolution` derives the shortest word of
the defining relation from the generator string — three lines of the standard definition — because
BoFire exposes resolution only as a formatted alias listing (`get_alias_structure`), and parsing
prose to recover a number is a worse dependency than restating the definition. `ScreeningDesign`
renders it as "resolution IV" with a sentence saying what is confounded with what, so a reduced
design cannot be presented as an exhaustive one.

## Consequences

- "Seven factors, one 96-well plate" has an answer: 128 runs become 64, 32 or 16, and the chemist
  is told which effects they can no longer separate.
- `factorial_design`'s signature gains a defaulted `n_generators`; every existing caller is
  unaffected.
- The re-encoding is a real coupling to BoFire's internals — specifically to *where* it applies a
  generator. It is documented at the point of use, and the two-level refusal keeps the coupling
  from silently spreading to domains it does not describe.
- **This is the second time in this session that a plan written from a code audit was wrong about
  runtime behaviour and the measurement won.** The audit read the docstring and the parameter's
  presence; only running it showed the parameter was inert. `tasks/lessons.md` records the pattern.
