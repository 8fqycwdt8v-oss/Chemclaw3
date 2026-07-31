# D-167 — Trust is a distribution, not a number: the residual listing, and the property table behind it

## Status

Accepted. Implements W2.4 of the dataflow review's plan.

## Context

The calibration ledger (IDEA-2, migration `016`) records every prediction and fills in the
measurement when one arrives, so "how far should I trust this calculator?" has a numeric answer
instead of a paragraph. One tool read it — `calculator_trust` — and it returned six aggregates.

Two problems, one of which was a live defect.

**The dispatch was a conditional, not a lookup.** The tool read:

```python
solubility = property_name == "solubility"
return await calibration_for(
    property_name,
    solubility_calc_version() if solubility else pka_calc_version(),
    unit="log S" if solubility else "pKa",
)
```

Every name that was not `"solubility"` was answered with pKa's current version and pKa's unit.
Ask about `logd`, or a calculator added next month, and the response was a confident, well-formed
calibration report about the wrong calculator, in the wrong unit — for the one question whose
entire purpose is deciding how much to trust a number.

**An average cannot say what a calculator is bad *at*.** A solubility model can sit at 0.3 log
units of mean absolute error while being near-perfect on neutrals and two units low on every
carboxylic acid; the two populations average into one reassuring figure. "This predictor is
unreliable for *this kind* of molecule" is what trust means when a chemist acts on it, and nothing
could express it. `predictions.subject` has held the canonical SMILES of every row since `016` —
the data was there and unreachable, the same shape as D-163 and D-165.

## Decision

**A table replaces the conditional, and a second tool exposes the residuals.**

### `_CALIBRATED`: one row per calculator that logs predictions

`{"solubility": (solubility_calc_version, "log S"), "pka": (pka_calc_version, "pKa")}`. Adding a
calibrated calculator is one row; asking about an uncalibrated one **raises**, naming what does
exist. Two entries is not an abstraction with one caller — it is data replacing a branch that
produced a wrong answer for every input outside it, and the wrongness was silent.

The version is looked up rather than pooled, preserving REV-12: a v1 that ran high averaged with a
v2 that ran low reads as well-calibrated while neither is.

### `calculator_outliers`: the misses, ranked, optionally filtered by class

Signed error (matching `Calibration.bias` — consistently high is correctable, scattered is not),
the molecule, the measurement, and whether the calculator's own ±1σ covered it. `within_uncertainty`
is `None` rather than `False` when no uncertainty was claimed: nothing was asserted, so nothing
failed.

The intended use is two calls. First unfiltered, to see the worst misses and look for what they
share; then `matching="C(=O)O"` to test that idea against the whole ledger. The tool's docstring
says this, because the second call is the one that turns an observation into a claim.

### One read of the ledger, two readers of the rows

`reconciled_for` returns `Residual`s; `calibration_for` summarizes them. Previously the aggregate
had its own SQL, and a listing with its own would have been a second predicate to keep in step with
the first. Now the number and the list are the same rows, so they cannot disagree about which.

The read is deliberately unbounded. The filter is `observed_value IS NOT NULL`, and an observation
is a measurement somebody made and typed in — the growth is bounded by bench work, not by how often
the calculator runs. A cap would silently drop measurements from the calibration, which is a worse
failure than the read it would protect against.

### Substructure matching is a `core.chem` primitive

`substructure_pattern` — SMARTS first, then SMILES, rejecting the zero-atom pattern RDKit would
otherwise match against every molecule — moves to `core.chem` beside `canonical_smiles`. The
fingerprint index's substructure search had the identical four lines; it now calls the shared one
and re-raises as `FingerprintError`, so its failure type is unchanged while the rule for "what is a
valid pattern" exists once. Two callers is enough for a *primitive*: the alternative is two copies
of a chemistry rule that drift apart without anything noticing.

Matching runs off the event loop under `substructure_match_timeout_seconds`, for the reason the
fingerprint scan documents: the ledger is small, but a short adversarial recursive SMARTS matches
for minutes regardless of corpus size.

### No filter by tag

The plan said "substructure **or** tag". A prediction's subject is a molecule and the row carries no
tags — a tag filter would need a column nothing writes, and inventing one to satisfy the phrasing
would ship an always-empty filter. Substructure is the filter the data supports.

## Consequences

- Asking about an uncalibrated property is now an error naming the calibrated ones, where it was a
  wrong answer. That is a behaviour change for any caller passing a third name — there is none in
  the repo, and the previous behaviour was not worth preserving.
- Only `solubility` and `pka` log predictions today, so the table has two rows. That is the
  *reason* it is a table: the third calculator is now a one-line change instead of a third branch
  in a conditional that was already producing wrong answers at two.
- `calc_outliers_max_results` (default 25) caps a page. The listing exists to be read — a hundred
  rows is scrolled past while spending the model's context.
- A short list means few measurements, not a well-behaved calculator. Both tools' docstrings say
  so and point at `n`, because the empty-ledger reading is the one that flatters the calculator.
- The unfiltered listing does not itself say "the acids are the problem" — the model reads that off
  the rows. That is deliberate: clustering residuals by scaffold is a statistical claim on a
  handful of points, and a tool that asserted it would be manufacturing confidence the data has not
  earned.
