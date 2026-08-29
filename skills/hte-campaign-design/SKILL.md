---
name: hte-campaign-design
description: >-
  Use when the chemist wants a plate rather than an experiment — a screen, an array, a
  high-throughput campaign, "24 wells", "screen catalysts and bases", "a 96-well plate for this
  coupling", "what should I put on a plate". Chooses the factors and their levels from what the
  record actually ran, lays them out with controls and a run order, and stores the plate as an
  editable design. For one experiment use protocol-generation; for the next point in an
  optimization you are already running use experiment-design.
tools:
  - structure_experiment_request
  - draft_experiment_protocol
  - read_experiment_protocol
  - find_experiment_protocols
  - generate_screening_design
  - suggest_next_experiment
  - campaign_progress
  - predict_outcome
  - reagent_frequency
  - substrate_precedent
  - conditions_for_similar_reaction
  - conditions_for_similar_product
  - workup_precedent
  - similar_reactions
  - condense_protocols
  - gather_evidence
  - recall_observations
  - resolve_compound
  - screen_hazards
  - ich_impurity_limit
  - stoichiometry_table
  - compare_solvents
  - predict_solubility
  - predict_pka
  - ask_clarifying_question
---

# Designing a plate

A screen is a bet with a fixed budget. Ninety-six wells is one number, and every well you spend on
a factor that does not matter is a well you did not spend on one that does. The chemistry judgment
in this skill is almost entirely **what to vary and at what levels** — the plate arithmetic, the
layout and the checks are `chemclaw.protocols`' problem and you do not have to carry them.

## The budget is the first thing you know and the last thing you should spend

Get it out of the ask before anything else. `structure_experiment_request` has `plate_format` and
`max_runs` slots for exactly this, each carrying its own basis, so a budget you *inferred* from
"a couple of plates" is visibly an inference. Then count backwards:

```
wells = controls + replicates + (levels of factor 1 × levels of factor 2 × …)
```

Four factors at three levels is 81 wells and leaves 15 for controls and replicates on a 96 — which
is a real design. Five at three is 243, which fits a 384 but is three plates' worth of work and a week of
analysis. Do that arithmetic
*before* you choose the factors, not after, because the alternative is proposing a plate and then
quietly deleting levels to make it fit.

## Choose the factors from the record, not from a textbook

**`reagent_frequency` is the workhorse here.** For the named reaction, the RXNO id, or the
product's functional group, it returns what the corpus actually reached for, with counts. That is
what a factor's levels should be drawn from: the ligands, bases, solvents and catalysts this
organisation has genuinely run, ranked by how often they worked. A level nobody in the record has
ever used is not forbidden — a screen is partly for finding those — but it should be a *deliberate*
inclusion you can name a reason for, not the default.

Then narrow with what else the record says:

- `substrate_precedent` — has this substrate been screened before, and what failed? A level that
  has already failed on this exact substrate three times is a well you should not spend again
  unless something has changed.
- `conditions_for_similar_reaction` / `conditions_for_similar_product` — the conditions similar
  transformations ran at, which is where a sensible *centre* for a continuous factor comes from.
- `condense_protocols` over the precedent hits — this is what tells you which factor was being
  varied in the campaigns that came before, and therefore what has already been asked.
- `recall_observations` and `gather_evidence` — a playbook or a distilled observation may already
  say "on deactivated aryl chlorides this ligand class is the one that matters", which is a factor
  choice somebody already paid for.

**A factor is worth a plate column when the record disagrees with itself about it.** If every
precedent used the same base, base is not the question; if the precedents split three ways on
ligand, ligand is. That is the cheapest test you have for whether a factor earns its levels, and it
is a question about the corpus rather than about chemistry-in-general.

## Levels

- **Categorical levels should span mechanism, not shelf.** Four phosphines that differ only in a
  methyl group is one level tested four times. Carry the `smiles` on each `FactorLevel` — it is
  what lets a hazard screen see them and what lets a follow-up Bayesian campaign featurize the
  category rather than one-hot it.
- **Continuous factors in a screen are held at their two ends and nothing between.** That is what a
  two-level screen *is*, and it is fine for "does temperature matter at all" and useless for "where
  is the optimum". If the chemist is asking the second question, the answer is a BO campaign
  (`suggest_next_experiment`), not a wider grid.
- **Write a `rationale` on every level.** One clause. It is the most useful sentence on the whole
  plate six weeks later, and it is the thing nobody can reconstruct afterwards.

## Reducing a design that does not fit

`generate_screening_design` does this properly and you should use it rather than deleting rows by
hand: `n_generators` halves the run count per generator, and what comes back carries a `resolution`
and a `summary` naming exactly which combinations were given up and which effects are therefore
**confounded**.

**Repeat that confounding statement to the chemist, in the protocol.** A fractional design
presented as if it were the whole screen is the single way a plate gets over-read, and it is worse
than not running it: the chemist concludes a factor does not matter when its effect was aliased
onto one that does. The `coverage_is_stated` check will note a reduced design; the note is for you
to turn into a sentence, not to satisfy.

Every factor must be at exactly two levels for a reduced design, and a problem carrying constraints
is refused outright — a factorial enumerates corners and honours no limit. If the chemist has a
real constraint ("base plus acid under 3 equivalents"), that is `suggest_next_experiment`, which
does honour it.

## Controls and replicates are not optional decoration

- **A positive control** — conditions the record says work — is what tells a flat plate from a
  failed one. Without it, "nothing worked" and "the stock solution was dead" are the same result.
  Mark it `control: "positive"`; it is exempt from the factor-coverage check for exactly that
  reason.
- **A blank or negative control** where a background reaction is plausible.
- **Replicates** are what give a screen a pure-error estimate. Without any replication, no effect
  the plate shows has a significance you can quote — so if the chemist is going to make a decision
  on a 5% difference, some of the plate has to be replicates. Mark them `replicate_of` so they read
  as intended repeats rather than as duplicated rows.
- The `controls_present` check is a *warning* and not a blocker, because a chemist running a
  discovery plate with a known-good column elsewhere may legitimately not want one. Ask rather than
  assume — but do not silently omit it.

## Layout and run order

`draft_experiment_protocol` takes the `design_id` that `structure_experiment_request` returned, the
shared `base`, the `factors`, the `arms` and the `evidence` — never a plate layout, which it
computes. Pass `plate_format` and it lays the arms out row-major and assigns a run order. `randomize_run_order=True` (with a `seed`) shuffles **the order the arms are run in, not
their positions**, and the distinction is worth telling the chemist: positions stay left-to-right
so the plate is pipettable from the map, while the run order is randomised so a drift over the
session — a decaying stock, a warming room — cannot be read as a factor effect. There is nothing
else in a screen that catches that.

Randomise whenever the plate will take more than an hour or two, or whenever a reagent is known to
be unstable. Tell them to run it in the order the run sheet gives, because a randomised order they
ignore is a randomisation that did nothing.

## Analytics — decide this before the plate, not after

A screen produces one number per well and the whole plate is worth exactly what that number is
worth. Name the analytic, the timing and **what it measures**: `measures` has to cover every
objective the request states or the plate comes back unable to answer the question it was run for.
That check is a warning; treat it as a blocker in your own head.

If the chemist is optimizing a trade-off — yield against an impurity, conversion against ee — then
every well needs *both* numbers, and a plate that measures only the first cannot be handed to
`suggest_next_experiment` later without discarding it.

## Safety scales with the plate

`screen_hazards` over every level's structure, not only the shared body's species — a screen's
whole point is that it introduces reagents the base protocol does not have. `ich_impurity_limit`
where a solvent choice is one of the factors. And say the sentence: this system flags, it does not
certify.

## What comes after the plate

A screen is usually round one. Say so, and say what round two would be: the arms that survive
become the observations `suggest_next_experiment` fits a surrogate to, and the factor space you
declared here is the design space it searches. That is why the factor names and levels are worth
getting right even for a fixed screen — they are the vocabulary the campaign inherits.

If results already exist and the chemist is asking where to go next rather than what to put on a
first plate, stop: that is `experiment-design`, and forcing an answered question back into a grid
spends a plate re-deriving what the record already holds.

## Revising

Chemists edit plates — they drop a ligand they have run out of, they swap a solvent, they add a
column. `read_experiment_protocol` before you revise so you are editing the current head, then call
`draft_experiment_protocol` with the same `design_id`, the head's `parent_revision`, the whole
revised protocol and a `change_note`. A stale parent is refused rather than allowed to discard their
edit; that refusal names the revision to start from.

When a chemist drops one level from a factor, the arms that used it go too — re-send the arm list
you actually want rather than the old one minus a row, because the coverage and distinctness checks
read the arms as given.

**Pass `plate_format` again whenever the arms change.** Omitting it carries the previous plate
forward untouched, which is what you want for a revision that only moves a temperature — a
randomised run order cannot be recreated, so re-laying out with a fresh seed would hand the chemist
a different plate. It is the wrong thing when the arm list has moved: the carried plate names arms
that no longer exist, and `layout_fits` refuses the revision rather than storing a plate that does
not match its own design.

**A replicate is the same conditions run again**, and `replicate_of` is refused on an arm that sets
anything different from the arm it names. Two arms that vary something are two arms; mark them as
replicates and the plate reports a smaller grid than it runs, while the run sheet tells a chemist to
average two experiments that are not comparable.
