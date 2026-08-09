# D-2026-08-09-a-valid-prefix-is-not-a-molecule — A valid prefix is not a molecule, so a hazard screen refuses it

**Status:** accepted · **Date:** 2026-08-09

## Context

`D-2026-08-09-a-preview-is-not-a-result` gave `ScreenResult` and `AlertResult` a `screened` field —
the canonical SMILES of every structure the result covers — because a clean screen used to serialize
to `{"flags": [], "verdict": …}`, naming nothing it had looked at. Its own pull request then wrote
down what that field had made visible and deliberately did not fix:

> **A related truncation is now visible rather than fixed.** The safety screens use
> `parse_molecule` directly rather than `require_canonical_smiles`, so `screen_hazards("CCO junk")`
> silently screens ethanol. `screened` surfaces that to a client; fixing the parse is a separate
> change.

This is that change.

**The mechanism, measured against this build.** RDKit's SMILES parser treats whitespace as the end
of the structure and ignores everything after it. `Chem.MolFromSmiles("CCO junk")`,
`Chem.MolFromSmiles("CCO 1")` and the tab-separated form are all ethanol; `Chem.MolFromSmiles("")`
is a molecule with no atoms. So a malformed, mistyped or concatenated string does not fail — it
*narrows*, to a different and smaller molecule than the caller submitted.

**What that produced.** `screen_structure("CCO CN=[N+]=[N-]")` — an organic azide sitting in the
tail the parser discards — returned `flags=[]`, `screened=["CCO"]` and the verdict "No rule in the
hazard table matched. This is not a safety assessment." `screen_genotoxic_alerts(["CCO
O=[N+]([O-])c1ccccc1"])` dropped the nitroarene the same way and answered with no alerts, under a
verdict that spends three lines explaining that an empty list is not a negative mutagenicity
prediction — about a molecule the payload never identified.

For a pair of tools whose entire documented discipline is that an empty result must never read as a
clearance (`science/safety/screen.py`'s module docstring, `ScreenResult.verdict`,
`AlertResult.verdict`, the `safety-screening` skill, both tool docstrings), a clean result *about a
molecule nobody asked about* is the worst outcome available. It is also the one a model is most
likely to report with confidence, because nothing in the payload looks wrong.

**The knowledge was already in the tree, one import away.** `core.chem.require_canonical_smiles`
rejects both inputs and its docstring names this exact truncation. Every molecule-taking calculator
goes through it at its cached-compute boundary. The safety screens were the one first-party consumer
that did not — they called `Chem.MolFromSmiles` directly and checked only for `None`.

## Decision

**1. A screen refuses input it cannot parse in full.** Not a new rule — the removal of an exception
to one the rest of the tree already keeps. `science/safety/screen.py::parse_molecule` now delegates
to the shared gate and raises rather than returning a molecule that is only part of what was asked
about.

Refusing rather than warning, and refusing rather than screening the prefix with a note attached:
this package already refuses an over-long component list for exactly this reason
(`require_screenable_size`, "Refused rather than truncated: a hazard screen that silently dropped
components would report 'no rule matched' for chemistry it never looked at"). The argument was
written down; it had simply never been applied to the parse.

**2. The gate is factored out as `core.chem.require_molecule` rather than restated.** Restating is
what produced the divergence. `require_canonical_smiles` and `require_standard_smiles` each spelled
the same four lines — strip, reject embedded whitespace, parse, reject zero atoms — which is exactly
the shape that invites a third, weaker copy somewhere else. `require_molecule` is now the one
definition of "RDKit read this string, all of it"; the two SMILES helpers are written on top of it,
and it returns the *molecule*, which is what a caller like a SMARTS screen actually needs and what
sent the screens looking for their own parse in the first place. `tests/test_ids.py` pins that the
three agree on every input.

**3. `InvalidSmilesError` is translated to `SafetyRulesError` at the package boundary.** The safety
package promises a caller one exception type, and `science/safety/notes.py`, `evals/metrics.py` and
`cli/validate_safety.py` each catch exactly that. Both are `ChemclawError`s, so either way the
refusal reaches the model as a deliberately-worded `ValueError` through `connectors/server.py`'s
sanitizer rather than as "an internal error occurred".

**4. A reaction's refusal names which component failed.** `parse_components` — shared by both
screens, as `require_screenable_size` is — reports "component 2 of 3", counted in the list as the
caller wrote it rather than in the deduplicated mapping the screen works on. "One of these nine
components is unusable" is not something a chemist can act on, and a tool error a model cannot act
on is one it will retry unchanged.

**5. `science/safety/notes.py::_is_structure` moves onto the same predicate, in the same change.**
It decides which inline code spans in a knowledge note are structures, and it promises that a span
which is not one "simply yields nothing". It asked a bare `Chem.MolFromSmiles`, so a span like
`` `CCO at 80 °C` `` read as a structure — and would then have been handed to a screen that now
refuses it, turning a span this gate is documented to ignore into a `kg-validate` failure on every
note containing one. Two predicates for one question is what made that possible.

## Consequences

- A malformed or concatenated SMILES is now an error from `screen_hazards` and
  `screen_genotoxic_alerts` where it used to be a clean screen of the prefix. That is a behavior
  change a caller can see, and it is the point. Both tool docstrings say so, in the imperative the
  model reads: a string this cannot read in full is refused, fix it and ask again, never report the
  refusal as a result.
- `screened` keeps its job and gains a second one. It was added so a clean screen would name what it
  had looked at; it is now also the evidence that what it looked at is what was asked about, which
  is why the tool docstrings point a reader at it.
- **One first-party parse of caller-supplied structure text keeps the old behaviour**, deliberately
  and not silently: `science/fingerprints/molfp/fingerprint.py::_parse`. Measured,
  `ecfp_bitstring("CCO junk")` and `ecfp_bitstring("CCO")` are the same bitstring, so
  `find_similar_molecules("CCO junk")` answers about ethanol's neighbours under the caller's own
  label. It is not folded in here because the failure is a wrong *search result* rather than a false
  clearance, and because `_parse` also indexes ELN labels, where refusing is the opposite trade —
  one odd label must not abort ingestion. It is a `docs/planning/BACKLOG.md` row carrying that
  measurement.
- Everything else in `science/` was checked rather than assumed. Every calculator reaches RDKit
  through `require_canonical_smiles` at its cached-compute boundary, so
  `run_cached_solubility("CCO junk")` already raised before `predict_solubility` saw it;
  `ingest/eln/validate.py` and `connectors/bo/knowledge.py` parse leniently by design and say so.
