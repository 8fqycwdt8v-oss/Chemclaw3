# D-2026-08-01-one-equilibrium-or-no-number — One equilibrium, or no number

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** the full-codebase review's `logd`
finding · **Extends:** D-2026-08-01-unknown-is-not-fine (a number carries what to trust about it)

## Context

`predict_logd` applies one Henderson–Hasselbalch correction from one pKa — the most ionisable site
`predict_pka` reports. That is exact for a monoprotic acid or base and wrong for anything else.

Measured on shipped code at pH 7.4:

| molecule | reported | true |
|---|---|---|
| succinic acid | log D −1.483 ± 1.6 | ≈ −5 |
| glycine | log D −2.812 ± 1.6, no error | — |

The glycine row is the sharper one. `predict_pka` takes the acid branch whenever any O–H is present,
so an amino acid never reaches the aliphatic-amine branch — and `tests/test_logd.py` explicitly pins
that the calculator **refuses** piperidine, because it cannot do aliphatic amines. The refusal that
exists was silently evaded by any molecule that also carries a carboxyl.

`LogdResult` carries no domain flag, and ±1.6 does not begin to cover a 2–5 log-unit systematic
error. That is the actual hazard: not an inaccurate number, but a confidently-reported one with an
uncertainty asserting it is fine.

A correct polyprotic logD is **unreachable from the current surface** — `predict_pka` never exposes a
second pKa — so computing one would be inventing chemistry.

## Decision

**Refuse when more than one equilibrium is in play, and only then.**

- **Amphoteric** (at least one acidic site *and* one basic nitrogen) → refuse at every pH. The acid
  branch is always taken when an O–H exists, so the basic site is never evaluated and nothing bounds
  its contribution. This is what closes the glycine bypass.
- **Polyprotic** (two or more sites of one kind) → refuse *only when the reported site is
  substantially ionised*. `predict_pka` reports the most ionisable site, so every other site's
  ionisation ratio is at most that one's; when the reported site is essentially neutral the omitted
  terms are bounded by a geometric series — about 0.0012 log units at a 5 % ionised fraction, which
  is far inside the reported uncertainty.

That second clause is the whole design. A blanket "more than one site → refuse" would have banned
ethylene glycol, sugars and succinic acid at pH 1, all of which the model handles correctly. The
threshold is config (`logd_negligible_ionised_fraction`), with the arithmetic bound in its comment.

**Refusal, not an out-of-domain `Estimate`.** Both conventions exist in the tree, but every domain
limit `logd` has ever had is a `ValueError`, and the aliphatic-amine case this closes is already one
— flagging would put one hazard in a field and its twin in an exception. The claims also differ in
kind: an ESOL prediction for a salt is a number of unknown validity; this is a number known to be
wrong by orders of magnitude.

## Consequences, including one this decision caused and had to fix

Applying the amphoteric rule immediately refused **paracetamol** and **imidazole** — the latter in
this calibration's own reference set, with exactly one basic centre. The gate was right and
`_basic_nitrogens` was wrong: it tested free valence only, so any neutral nitrogen with a lone pair
counted.

That is a pre-existing `predict_pka` defect this decision exposed. **Acetamide** — whose only
nitrogen is an amide — took the base branch and returned a conjugate-acid pKa with the same stated
±1.0 as pyridine, for a compound with no basic centre.

`_lone_pair_is_available` now excludes, each for its own reason:

- **amide / carbamate / urea / sulfonamide** — lone pair conjugated into the adjacent C=O or S=O;
  protonated acetamide is pKaH ≈ −0.5 and protonates on the oxygen;
- **nitrile** — sp nitrogen, pKaH ≈ −10;
- **pyrrole-type aromatic N** — three σ-bonds, lone pair in the aromatic sextet, pKaH ≈ −4.

**Aniline is deliberately left in**: its ring bond is aromatic rather than the C=O single bond the
amide rule looks for, and it is genuinely basic at pKaH 4.6.

The third exclusion is what restores imidazole, and it is well-founded independently: two σ-bonds
with an in-plane lone pair is a base, three σ-bonds with the lone pair in the π system is not, and
imidazole's two ring nitrogens are one of each. It also collapses caffeine's count from four basic
nitrogens to the one that is real.

Benzoic acid and pyridine are bit-identical before and after, and the base calibration's Spearman
> 0.95, in-sample RMSE < 0.5 and held-out ±1.0 bounds all still hold under the narrowed enumeration —
measured, because narrowing the enumeration could plausibly have broken the calibration.

## Alternatives rejected

- **Compute the polyprotic form.** Requires a second pKa the surface cannot produce. Estimating one
  would be a number with no provenance dressed as a correction.
- **Return a flagged number instead of refusing.** See above; and a flagged number is copyable into a
  report in a way an exception is not.
- **Leave `_basic_nitrogens` alone and accept the over-refusal.** Would have made a logD tool that
  refuses paracetamol, and would have left `predict_pka` reporting basic pKa values for amides.
