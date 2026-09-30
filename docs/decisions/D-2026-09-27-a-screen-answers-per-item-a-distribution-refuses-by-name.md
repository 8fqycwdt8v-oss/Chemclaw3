# D-2026-09-27-a-screen-answers-per-item-a-distribution-refuses-by-name — what a list-taking calculation does when one item fails

**Status:** accepted · **Date:** 2026-09-27

## Context

Four durable calc composites take a list and loop over it: `bond_dissociation_survey` (bonds),
`solvent_comparison` and `species_solvent_comparison` (media), and `species_ranking` (species).
None had a per-item boundary, so the **first** item that raised ended the job, and a chemist
learned about the one item that failed first and nothing about the rest. One unparameterised
solvent cost a five-solvent screen; one fragment the server would not embed cost a twenty-bond
survey.

The pattern that answers this is from El Agente Potente (arXiv 2609.14840, §A.3-A.4): in its
high-throughput graph *every input ends as a typed outcome*: a result, or a typed failure
(`__RELAX_FAILED__`, `__FILTERED__`) retained for inspection. Nothing is silently dropped, and one
bad structure does not cost the campaign. The rest of that paper was either already here (typed
dispatch over `XtbJobSpec`'s discriminator, per-primitive caching, provenance) or already decided
against (`D-2026-09-19-the-condition-was-met-and-the-answer-is-still-no` for MACE,
`D-2026-08-25-a-sandbox-is-a-server-not-a-verb` for a coding mode that reaches validated
functions).

## Decision

**1. The per-item boundary is `ValueError` and nothing wider** (`compose._attempt`). That is this
repository's existing "bad data" contract: `ChemclawError` is documented as the thing to catch "at
batch boundaries (reject-and-continue)", `CalcToolError` (the server refusing) is one, and
`durable/publish.py::_BAD_DATA_TYPES` registers the family non-retryable because the identical call
fails identically. An outage is the opposite claim: `CalcServerError` and `CalcBusyError` are
`SubsystemUnavailableError`s, they pass through the boundary untouched, and Temporal retries the
activity. A pod restart must not be reported as N chemistry failures.

**One refusal in that family is not about the input.** The calc server's inline time budget stops
a run with a plain `ValueError`, which arrives as `CalcToolError` like any refusal, although the
same item can pass on an idle pod and be stopped on a busy one. It was already misclassified as bad
data before this decision, which failed the whole job with it; now it is one item's `failed` entry
carrying the server's sentence. Telling it apart needs a server-side marker, a cross-repository
contract change filed as its own `BACKLOG.md` row rather than taken here.

**2. Independent items end as typed outcomes.** A survey's bonds and a screen's media are separate
answers, so a refused one becomes a `FailedBond` or `FailedMedium` in a new `failed` field, carrying
the server's own sentence as `reason`, and the rest are still reported. The aggregates are stated
over what was computed, and the warnings say so:
- the weakest bond is the weakest *of the rest*, and a failed bond may be weaker;
- a screen left with one medium makes no "does not distinguish" claim, because a spread over one
  row is zero by construction;
- `considered == len(bonds) + len(failed)`.

The warnings are what reaches a published record: the projection already turns them into
`calculation_flag` rows. Each job summary names the count, because the summary is the line people
read.

**3. A distribution refuses, by name, after trying every species.** A population is normalised
over the set, so ranking the survivors hands the missing forms' share to the forms that were
computed. That is the "confident about the wrong universe" error the composite already warns about,
this time made by the composite rather than by the enumeration. `species_ranking` therefore attempts
every species and then raises one `ValueError` naming each failed SMILES and its reason. Every form
that did compute is cached (D-011), so the rerun without the offenders recomputes none of them.
Inside `species_solvent_comparison` that refusal is one medium's `FailedMedium`, so a reported
distribution always ranks the whole set.

**4. Nothing computed is no answer.** A screen in which every item failed raises one `ValueError`
naming them all. It never returns an empty ranking.

The fields are defaulted, so a payload written by a run in flight across the deploy still decodes.

**Two things are the input rather than items, and fail the job as before.** The survey's parent is
the left-hand side of every bond's reaction, and a refusal is not cached, so as an item it was
asked for once per bond; it is computed once, up front, and its failure ends the survey. A screen's
equation checks (balance, the sigma map) are the same in every medium and run once before the
fan-out. A screen that nothing failed in says what it always said, even over one medium: only a
failure can lose a comparison.

## Options not taken

- **Drop the failed item and rank the rest.** This is the fan-out's current behaviour for its
  children. It is right for nothing here. For a screen it hides the gap. For a distribution it
  changes every population.
- **Return a distribution over the survivors with the populations withheld.** This makes
  `population` optional on `RankedSpecies`, and a `SpeciesDistribution` is consumed by the species
  screen, three templates, the job summary and the publish projection, all of which read it as
  present. A refusal naming every form gives the chemist the same information, and the cache makes
  acting on it free.
- **Catch every exception per item.** This turns an outage into a completed job that confidently
  omits items, and removes Temporal's retry from exactly the fault a retry fixes.

## Not changed

`durable/orchestrator.fan_out` still logs, counts and drops a child that exhausts its retries
(D-030). Its report caller already reconciles the gap into a visible `retrieval_failed` marker, and
its memory caller's drop is counted on `chemclaw_fan_out_children_dropped_total`. Making it return
typed outcomes changes the helper's contract for both callers, and neither has asked for that.
Template waves still fail on a failed step, because a step's output is the next step's input.

Revisit when: a `fan_out` caller needs to report *which* child failed rather than that one did
(the signal would be a caller reconstructing that from ids, as `report_workflow._reconcile` does),
or a consumer of `SpeciesDistribution` can act on a distribution with withheld populations.

## What keeps it true

`tests/test_calc_screen_outcomes.py` drives every case through `FakeCalcServer.overrides`, so a
refusal arrives down the real wire path as `CalcToolError`, a full pod as `CalcBusyError` and a
fault as `CalcServerError`. For each screen it covers:
- a refused item reported beside the computed ones;
- every item refused, naming each;
- an outage propagating as itself.

For the ranking it covers the refusal after every form was tried, the rerun recomputing nothing,
and every refused form being named. It also checks that a payload without `failed` still decodes.
Measured against the two files together: mutating `_attempt` to catch `Exception` turns the four
outage tests red, and removing the boundary turns eleven red — nine per-item tests there and the two
summary tests in `tests/test_calc_jobs.py`
(`test_a_survey_that_lost_a_bond_says_so_in_its_summary`,
`test_a_screen_left_with_one_medium_does_not_summarise_a_comparison`). The review fixes (the parent
computed once, the equation checked once, a one-medium screen nothing failed in, the standard-state
caveat over a lone gas row) have tests of their own that are red on the first cut.
