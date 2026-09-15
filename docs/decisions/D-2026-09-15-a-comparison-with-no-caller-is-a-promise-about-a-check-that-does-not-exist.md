# D-2026-09-15-a-comparison-with-no-caller-is-a-promise-about-a-check-that-does-not-exist — A comparison with no caller is a promise about a check that does not exist

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the analytical tier's first module.
Supersedes nothing. It is the same finding as
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`, one layer over, and it is
closed the other way: by building the producer rather than deleting the claim.

## Context

`core/units.Measurement.compare` returns -1, 0 or 1 and refuses across dimensions and across two
stated bases that disagree. Its docstring says why the refusal matters:

> The refusal is the point. Python would happily order two floats whose units disagree, and a
> specification check written that way passes a batch that is out of limits.

Measured: `compare` had **zero callers in `src/`**. Every use of it was a test calling it directly.
That is the `reject_widening` shape `CLAUDE.md` records — *"a guard with no caller, kept alive by a
test that calls it directly … a claim that a control exists"* — with an extra turn of the screw,
because the docstring does not merely imply a caller, it describes what that caller protects
against, in the present tense.

The basis half is the sharper case. `compare` refuses an `area%` against a `% w/w` because they are
the same unit, the same dimension and different facts. Nothing in this system had ever asked it to.

`D-2026-08-15` deleted `reject_widening` rather than finding it a caller, and that was right there:
the invariant survived in a merged ADR and the function was a claim. Here the caller is a capability
this product is missing — "does this batch meet specification" is the single most common question in
analytical development — so the honest close is to build it.

## Decision

`src/chemclaw/analytical/` — a new tier, prescriptive about **results** where `protocols/` is
prescriptive about what to run. One module, `specification.py`, and the narrowest import edge in the
tree: `core` and nothing else. There is no chemistry in "is this number under that number".

**The verdict is not a boolean, and that is the whole design.** A check written as "compare each
result to its limit" has nowhere to put the two answers that are neither pass nor fail, so both
silently become *pass*:

- **A criterion nothing measured.** A twelve-row specification with nine results has three
  unanswered questions. Scored as a filter over the *results* it has nine passes and the batch looks
  tested. So `evaluate` returns one row per **criterion**, in the specification's order.
- **A result the limit cannot be compared with.** The `area%`-against-`% w/w` case, reaching a
  caller for the first time. `indeterminate`, never `within`.

Caught **per criterion** rather than at the top: raising would leave a caller with nothing for a
twelve-row specification because one row was entered in the wrong unit, and a caller with nothing is
a caller who wraps the call in a bare `except` and takes the pass.

**A `within` verdict can still be an investigation.** 0.48 ± 0.05 % against a 0.50 % maximum is
inside the specification and indistinguishable from outside it at the method's own precision — the
case an analyst escalates, and the one a bare comparison reports as a clean pass.
`limit_within_uncertainty` says so **without changing the verdict**: whether the number is under the
limit is arithmetic, whether to investigate is a judgment, and folding the second into the first
would be this module deciding something about a batch. A result that reported no uncertainty never
sets it, because "nobody said" is not "the spread is zero" — `Measurement.uncertainty` already makes
that distinction and this inherits it rather than restating it.

Limits are **inclusive**, per USP General Notices 7.20, said out loud because the alternative is a
silent off-by-one on precisely the values people argue about.

`agent/analytical_tools.check_against_specification` is the tool, read-only in `authz`, in-process
in the registry, and **exported** on the MCP face rather than withheld — the question that surface is
organised around is whether a tool says something about this deployment's people or its chemistry,
and this one reads no store and opens no session, so an external caller learns only the arithmetic
they supplied the inputs for.

## The second module, because one with no caller would be the same defect again

`stability.py` answers the question that follows a specification check: given the same attribute at
several timepoints, when does the trend reach the limit? It is the second caller for
`AcceptanceCriterion`, which is what makes the tier a tier rather than a module.

Three refusals carry it, and each is a number that would otherwise look reasonable:

- **It is not a shelf life.** ICH Q1E derives a retest period from a procedure this implements one
  step of — poolability across batches, the worst-case batch, and whether a linear model suits the
  attribute are all outside a single regression. The sentence rides on **both** the crossing and the
  non-crossing branch, because a note is easy to write on the path somebody tested.
- **Extrapolation is capped** at the lesser of twice the observed period and twelve months beyond it
  (Q1E §2.4). Past it the answer is "not within what this data supports" — a statement about reach,
  not about the attribute, and deliberately not spelled as a very large number.
- **Fewer than three timepoints is refused**, because two fit a line with zero residual degrees of
  freedom: the band comes back infinitely narrow and a caller gets their most confident-looking
  answer from their least informative data.

**A defect found by driving a real stability shape rather than a generated one.** Every timepoint
identical — what a well-behaved impurity at the reporting threshold really produces — fits a slope
of exactly 0.0, and `slope > 0` classified that as *falling*, looked for a minimum, and refused a
specification that correctly stated only a maximum. With no drift the confidence band still widens,
so a bound does move and the only question is which; it now comes from the criterion.

## What this cost, stated because a new tool is never free

The two tools measure **316** and **414** tokens, and they did not fit: the `__default__` ceiling
went 67,200 → 68,000 and the two compaction defaults with it (`agent_tool_result_clear_trigger`
108,200 → 109,000, `agent_context_token_budget` 118,700 → 119,500), leaving the default profile at
67,930 and 70 tokens of headroom.

**Trimming came first and could not close it**, which is the part worth recording.
`check_against_specification`'s description went from 1,266 characters to 844 and the prefix was
still 316 over, because a tool's description is counted **twice** — once as itself and once inside
the schema that embeds it — so a 300-token overage cannot be trimmed away without gutting the
prompt the model reads. At 316 and 414 both tools sit below this repository's own median.

What it costs is stated rather than absorbed, and one half is the direction
`tests/test_compaction.py` exists to make somebody state: a maximal request now leaves **4,404**
tokens of headroom under a 128k model rather than 5,204. The thread allowance is unchanged at
30,000 — the request bound moved to keep it so. What buys the margin back is a narrower prefix
(profile routing, or `D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for`'s deferred
schemas), not a further raise, because the next one is measured against the same unmoved window.

`data/evals/probes/analytical.yaml::an-35` and `an-36` are real probes rather than `EXEMPT` rows, on
the same argument Wave C took for `op-34`: an exemption with a pointer is still a tool nothing asks
for. `an-36` is built so the interesting answer is not the obvious one — the bound reaches the limit
at 11.6 months against a last pull at 12, so "12 months" is wrong for a reason a grader can check.

## What keeps it true

- `tests/test_specification.py::test_measurement_compare_now_has_the_production_caller_its_docstring_describes`
  — the absence test, in D-2026-08-26's shape. It scans for the call rather than asserting
  behaviour, because every other test in that file would still pass if `_score` were rewritten to
  compare floats itself, and the docstring's claim would be false again in silence.
- `tests/test_specification.py::test_a_criterion_nothing_measured_is_reported_and_is_never_a_pass`
  and `test_a_result_the_limit_cannot_be_compared_with_is_indeterminate_not_a_pass` — the two
  verdicts a boolean cannot hold, which is what this module exists for.
- `test_one_incomparable_result_does_not_cost_the_verdicts_of_the_others` — asserted as the other
  rows *surviving*, because that is the property, not "no exception escaped".
- `test_the_flag_is_only_ever_set_on_a_result_that_is_within` — all four verdicts in one
  evaluation, so a future branch that forgets to set it False is caught.
- `test_the_limits_may_be_in_a_different_unit_from_the_result` — 480 ± 50 ppm written as
  0.048 ± 0.005 % against a 500 ppm limit. A straddle check written as "compare the uncertainty to
  the gap" is wrong across units and right in every same-unit test.
- `tests/test_layering.py` — the `analytical → core` and `agent → analytical` edges, declared, with
  the comment saying what question to ask if a third one is ever proposed.
- `tests/test_stability.py::test_a_flat_attribute_has_no_direction_and_is_not_read_as_falling`
  — the zero-slope defect, caught by a real stability shape.
- `tests/test_stability.py::test_an_extrapolation_past_what_q1e_permits_is_refused_rather_than_returned`
  and `test_the_window_is_twelve_months_beyond_rather_than_twice_when_that_is_shorter` — the second
  drives a 24-month study, where Q1E's two rules disagree (48 against 36) and an implementation
  taking only the first extrapolates a year too far. The fixtures above cannot tell them apart.
- `tests/test_stability.py::test_the_bound_crosses_earlier_than_the_fitted_line_does` — asserted as
  the inequality against the line computed from the returned fit, so it cannot drift with the
  fixture. A fit reporting the *line's* crossing would always give the longer number.
- `tests/test_stability.py::test_every_estimate_says_it_is_not_a_shelf_life` — both branches.
