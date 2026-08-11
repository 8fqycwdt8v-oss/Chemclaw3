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

One definition also means a case found later is added once and reaches every caller, which is what
happened here: **non-ASCII characters are refused too.** RDKit skips a run of them at either *edge*
of the string while failing on one between two atoms — measured on this build, `"°C"` is methane,
`"CC°"` and `"°CC°"` are ethane, `"C°C"` is a parse error. That is the whitespace truncation
wearing a different character, SMILES is written in printable ASCII, and decision 5 below is what
surfaces it: a note body cut into tokens offers `°C` from `` `80 °C` `` as a candidate structure.

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

**5. The PR-gate cuts a code span into tokens a SMILES could be, and asks the same predicate about
each one.** `science/safety/notes.py::_is_structure` moves onto `parse_molecule` — two predicates
for one question is what let a span like `` `CCO at 80 °C` `` pass a bare `Chem.MolFromSmiles`
here and then be handed to a screen that refuses it, turning a span this gate is documented to
ignore into a `kg-validate` failure on every note containing one. But moving it is only half a
decision, and the first version of this ADR stopped there, which broke the gate in the other
direction.

**What the half-decision did.** `structures_in` uses `_is_structure` as a *filter*, so a span the
strict predicate rejects is not screened narrowly — it is dropped and never screened at all.
Measured on this build, on a body whose code span held `` `CN=[N+]=[N-] (2 equiv)` ``:
`structures_in` returned `[]` and `hazard_problems` returned `[]`, where the lenient predicate
returned the high-severity `organic-azide` problem, and `screen_structure("CN=[N+]=[N-]")` returns
that flag on its own. An agent-authored `experiment-proposal` naming a reagent with its quantity is
not an edge case; it is the input class this gate exists for. **A screen that narrows is the defect
this ADR opens with. A gate that stops screening is worse, and it was introduced by the fix for
it.**

**The rule that resolves both.** A SMILES contains no whitespace and nothing outside printable
ASCII — which is exactly what `require_molecule` refuses, decision 2 — so those characters inside a
code span are prose by construction. They are the one separator that can be split on without a
guess about spelling, and splitting on them first turns "this span is not a molecule" into "these
are the molecules this span names, and these are the words around them". The strict predicate then
classifies a *token*, where it is exactly right, instead of filtering a *span*, where its false
answers cost a screen. RDKit stays the arbiter of what a structure is; the split only decides what
to ask it about.

The tokens are a **superset** of what the lenient predicate yielded — that parser stops at the
first whitespace and skips a non-ASCII run at either end of what remains, so whatever it read is
one of these tokens — and no note can therefore lose a flag relative to the behaviour before this
ADR. `tests/test_safety.py` states that as a generated property, comparing against a
re-implementation of the old extraction over spans built from structures and the prose a model puts
beside them, rather than as a list of the shapes that occurred to whoever wrote the fix. It also
strictly gains: `` `CCO CN=[N+]=[N-]` `` now screens both species where the lenient parse screened
only the ethanol, `` `1.2 equiv of CN=[N+]=[N-]` `` is screened where the lenient parse read `1.2`
and gave up, and `` `CN=[N+]=[N-]—the azide` `` keeps its azide where the lenient parse failed
outright.

**The split errs towards screening, and that direction is chosen rather than accepted.**
`` `80 °C` `` yields the token `C`, which RDKit reads as methane, so the gate screens a molecule the
note never named. There is no rule that avoids this and still recovers the azide from
`` `CN=[N+]=[N-]°` ``, because the two strings have the same shape — an ASCII structure with a
non-ASCII character stuck to it. This is not the "clean screen of a molecule nobody asked about"
that opens this ADR: that one is a *tool result* a chemist reads, and this one is an input to a gate
whose only output is "this note needs a `## Hazards` section". An extra molecule can add a flag,
never remove one, and every rule in the table is a multi-atom motif no lone atom matches.

**Three ways to fix this were on the table, and two were rejected.**

- *Screen the parseable prefix.* Restores the old coverage and re-introduces the truncation this
  ADR removes — and it still loses the second molecule of `` `CCO CN=[N+]=[N-]` ``, because a
  prefix is one molecule by construction.
- *Report an unreadable span as a gate problem* (printed, not raised, as `kg-validate` already does
  with a broken rule table). Safe, and it fires on `` `CCO at 80 °C` `` — the exact spurious
  failure this decision point was created to prevent. The gate's own scope rule is that it must
  fire rarely enough that a firing means something.
- *Screen the prefix **and** report the span as malformed.* The strongest of the three, and still
  wrong, because it calls a correct reading an ambiguity. There is no property separating "prose
  that happens to begin with a valid SMILES" from "a structure with a trailing annotation": both
  spans above are a structure with an annotation, and `` `CCO at 80 °C` `` *does* name ethanol —
  screening it is the right answer, not a truncation to apologize for. So the complaint would fire
  on every annotated span, which is the previous option with an extra step. Tokenizing answers both
  spans correctly, and once both answers are right there is nothing left to complain about.

What tokenizing does not do is read a SMILES fused to *ASCII* punctuation: `` `CN=[N+]=[N-], 1.2
eq` `` yields no structure, exactly as it did before, because `CN=[N+]=[N-],` is not a SMILES.
Recovering it would mean deciding which *printable ASCII* characters this repository treats as
prose — a judgement about spelling, and a second, weaker answer to the question RDKit answers,
which is what this ADR is about. The cut is made where the character set draws the line and not one
character further: outside printable ASCII nothing can be part of a SMILES, so nothing there is a
guess.

**6. A screen of nothing is refused, like a screen of too much.** `require_screenable_size` now
rejects an empty component list as well as an over-long one. `screen_hazards([])` answered
`{"flags": [], "screened": [], "verdict": "No rule in the hazard table matched…"}` — a clean screen
of *nothing*, in the shape a model paraphrases as "I screened it and it came back clear", and the
one case where `screened` (added so a clean result names its subject) has nothing to name. Same
sentence as the truncation, one step further on: an empty result means no rule matched the
structures screened, and with nothing screened there is no such statement to make.

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
  `ingest/eln/validate.py` parses leniently by design and says so.
- **`connectors/bo/knowledge.py::_molecule_in` stays lenient, and its docstring now says why
  instead of claiming to apply "the same arbiter" as the gate.** It did claim that, and the claim
  was false the moment decision 5 was written — but unifying the two would have been the wrong
  direction, not merely a rename. Its answer decides whether a recommended parameter value is
  written into the note *in backticks*, and a backticked value is precisely what the gate reads. A
  campaign level named `CN=[N+]=[N-] (2 equiv)` would, under the strict gate, be emitted as plain
  prose and screened by nobody; under the lenient one it is backticked, tokenized, and its azide
  flagged. Erring towards "this is a structure" costs a pair of backticks around something that is
  not one, and erring the other way costs the screen — so the two predicates are deliberately
  different, in the direction that keeps the gate's input as wide as possible and lets the gate's
  own strict predicate do the classifying.
