# D-2026-08-01-unknown-is-not-fine — "Unknown" is not "fine": one shape for how much to trust a number

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** F8-T1 · **Extends:** D-009 (the
eval/metric layer), REV-12 (the calibration ledger)

## Context

`implementation-tickets.md` specified a `science/calc/uncertainty.py` with
`value + uncertainty + in_domain + method`, and conformal prediction where feasible. None of it
existed: no such module, and zero occurrences of `in_domain`, `applicability` or `conformal`
anywhere in the tree.

The consequence is specific rather than abstract. `predict_solubility` runs Delaney's ESOL equation
— a linear fit over four RDKit descriptors — and attaches a constant 0.75-log RMSE to whatever comes
out. Hand it an ammonium salt, an anion, or an organometallic and it returns a confident number with
that same error bar, because RDKit computes a Crippen logP for anything it can fragment. The
`calculation-selection` skill, whose job is to advise on which calculation to trust, had nothing
machine-readable to consult.

## Decision

**One `Estimate`: `value`, `unit`, `uncertainty`, `method`, `in_domain`, `domain_reasons`** — carried
*beside* each calculator's own result model, not replacing it. The domain-specific fields a chemist
reads (a pKa's site, a solubility's model id) stay where they are; the uniform part is what a skill,
a note writer or a retrieval excerpt consults regardless of which calculator answered.

**`in_domain` is three-valued, and `None` is not `False`.** A calculator with no declared domain
reports "unknown". `Estimate.trustworthy` requires an affirmative `True`, so a consumer cannot read
a missing answer as a positive one — which is the specific bug a two-valued flag would introduce on
the day a fifth calculator is added without a domain.

**`method` says where the uncertainty came from**, and this is not decoration. `reported` is the
model's published error, a constant that knows nothing about the molecule in hand. `conformal` is a
split-conformal interval over *this deployment's own* reconciled residuals — the calibration ledger
(REV-12) already records exactly the `(predicted, uncertainty, observed)` triples that needs.
`propagated` is an input's uncertainty carried through arithmetic, which `logd` already does. A
reviewer weighs a claim about a paper's test set differently from a claim about this system's
chemistry, and until now the record could not tell them apart.

**The conformal rank is `ceil((n + 1) · coverage)`, and the `(n + 1)` is the guarantee.** It is what
makes the interval cover the *next, unseen* prediction rather than summarising the ones in hand.
When the required rank exceeds `n` the function returns `None` rather than the largest residual: with
5 residuals there is no finite 95% conformal interval, and reporting one would state a guarantee the
data cannot support. `calibration_conformal_min_samples` is a second floor beneath the arithmetic
one, because an interval computed from nine residuals is *valid* and still badly estimated — its
value is the largest of nine numbers, so one unusual compound sets it.

## Why not the alternatives

**Declare ESOL's statistical applicability domain — descriptor ranges or a leverage cutoff.** This is
what an applicability domain normally means, and it needs the training set. This repository ships
none, and neither does any corpus in `data/`. Writing plausible bounds and labelling them "the
training ranges" would put a **fabricated threshold** into a GxP system, where a number in a
validated record is expected to have a provenance. Worse than no check, because a check that exists
gets trusted. The statistical half stays open, named in the backlog, with what it needs.

**Assert the structural domain anyway** — and this is what shipped, because it rests on a different
kind of claim. That a salt is out of domain is not a statement about where Delaney's compounds fell;
it is that the equation describes one molecule and has no term for a counter-ion. Same for a charge
(Crippen contributions are parameterised for neutral species) and for a non-organic element (there
is no contribution to sum, and RDKit adds up whatever it recognises rather than refusing). Those
follow from the model's definition, are citable without its data, and catch the cases where a
confident number is most wrong.

**Widen the uncertainty when out of domain** instead of flagging it. Tempting and wrong: it implies
the error is merely larger and still drawn from the same distribution. Extrapolating a linear fit
does not widen its residual distribution, it leaves it. There is no multiplier that makes an
undefined answer defined, and picking one would hide the problem behind arithmetic.

**Replace the calculators' result models with `Estimate`.** It would flatten away the fields a
chemist actually reads and force every consumer to change at once for no gain. Beside, not instead.

## Consequences

- A prediction now says whether it can be relied on, in one shape, across calculators — and says
  "unknown" where nobody has declared a domain, rather than staying silent.
- `predict_solubility` still returns a number for a salt. Refusing would break the calculator's
  contract and hide a value a chemist may still want; what changed is that the number arrives
  labelled.
- The conformal machinery is in place and unused on the inline path: it needs a database read, so it
  belongs on the cached path, and it needs `calibration_conformal_min_samples` reconciled
  measurements before it will say anything. A deployment that has measured nothing sees `reported`
  and knows it.
- **Not closed: the statistical applicability domain.** It needs the ESOL training set (or any
  labelled solubility corpus) to derive descriptor bounds or a leverage cutoff from, and that is a
  data-acquisition decision rather than a coding one.
- **Not closed: uncertainty reaching notes.** `qm/knowledge.py` still writes
  `total energy: {x:.6f} Hartree` with no error bar. `Estimate` is the shape that fixes it; wiring
  the note writers is its own row.
