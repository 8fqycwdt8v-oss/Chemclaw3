# D-2026-09-15-a-suggestion-is-points-and-a-design-needs-labels — the BO→protocol gap was retyping, and it was the model doing it

**Status:** accepted · **Date:** 2026-09-15

## Context

This system can propose experiments and it can draft protocols, and nothing joined the two.

`suggest_next_experiment` returns `ExperimentSuggestion.candidates`, each a `Candidate` whose
`params` is a bare `dict[str, float | str]` keyed by parameter name. `generate_screening_design`
returns `ScreeningDesign.runs`, the same shape in bulk. Neither carries a unit, a level label or a
species role, because an optimiser has no use for any of them.

`draft_experiment_protocol` needs `Factor`s whose levels carry labels, structures and numeric
values, and `ProtocolArm`s whose `levels` map factor name to level **label**. So between the two
sat a transcription, and the thing doing it was the model — reading a candidate table and writing
the arms out by hand, one level at a time. Measured by reading the code: there is no path connecting
them anywhere. `grep -rn "from chemclaw.protocols" src/` reaches `agent/`, `api/` and `protocols/`
itself, and nothing under `science/bo/` or `connectors/bo/`. The only bridge was prose in two
skills telling the model to "hand the design over".

A transposed value in that transcription is a different experiment, run at a different condition,
with nobody able to see it — the design is internally consistent and every check passes.

## Decision

**Translate the mechanical half in `protocols/from_bo.py`, and translate nothing else.**

What it produces: which parameters the runs actually vary, the distinct settings each takes, and
which runs repeat one another. What it does not produce is the `ProtocolBody` — the charge table,
the steps, the analytics, the hazards — because those are judgment over the chemistry. So it returns
the two collections `draft_experiment_protocol` takes as arguments rather than an `ExperimentDesign`
it could not honestly fill.

**Where it lives is forced and is also right.** `tests/test_layering.py` allows
`chemclaw.protocols -> chemclaw.science` and allows neither `chemclaw.science -> chemclaw.protocols`
nor `chemclaw.connectors -> chemclaw.protocols`, so the translation can only sit on this side. It
is the correct side on the argument that file already makes: `protocols` imports neither `ingest`
nor `kg` because "a design is prescriptive and their shapes are descriptive". An
`OptimizationProblem` is a design space — prescriptive — so reading one is the rule applying rather
than an exception to it.

**Four refusals**, each of which would otherwise reach a chemist as a plausible-looking plate:

- Two parameters whose names slug to one `Factor.name`. Merging them produces a design that is
  internally consistent, passes every check, and describes experiments nobody planned — each arm
  carries one column holding whichever parameter was written last. **Nothing downstream can detect
  it**, because by then there is only one factor.
- A parameter over more than `Factor.levels`' 96 settings, named with its count — otherwise the
  model meets a pydantic error against a field it never wrote.
- Runs that name a parameter the problem does not declare, or omit one it does. Both mean runs and
  problem from different campaigns; only the second would ever surface, as a
  `factor_levels_declared` **blocker**, against a design the model had already written a body for.
- A parameter the runs never vary. That is a *setpoint* — `Factor.levels` requires two — so it is
  reported in `constants` for the body, never dropped.

**Repeats become `replicate_of`**, which is what a screening design's centre points and replicates
are. `arms_are_distinct` warns about two arms at identical settings unless one says it is a
replicate, and the model reading a run table cannot see that run 7 repeats run 2 without comparing
every column by eye — which is the error this module exists to remove.

**One function formats both halves of a level.** `factor_levels_declared` matches an arm's level
against the factor's declared labels by string equality, so a float written `80` in the factor and
`80.0` in the arm fails a design that is in fact correct. `_label` is `%.10g`, the same format
`protocols/export._cell` writes to the run sheet, so a level reads the same in the factor table, the
arm and the CSV a chemist opens.

## Consequences

- `agent/protocol_design_tools.experiment_arms_from_campaign` makes it reachable, taking a
  `campaign_id` rather than an `OptimizationProblem` — `read_campaign_thread` already returns both
  the problem and the last candidates, and passing the problem in would have put a schema already
  over `MAX_SINGLE_TOOL_TOKENS` into a second tool.
- Classified **read-only**, explicitly rather than by omission. The plan gate lets a read run while
  a plan is still being built, and "what would this campaign's next experiments look like as a
  plate" has to be answerable before somebody approves drafting them, not after.
- **The prefix cost was caught and paid down rather than waived.** The tool's first docstring cost
  **626** tokens against 290 for `read_experiment_protocol` and 191 for `find_experiment_protocols`,
  and pushed `tests/test_context_floor.py`'s observed prefix 26 tokens over its ceiling. Trimmed to
  **431** by moving the rationale into a comment beside the tool
  (`D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`). The ceiling did not move.
- **Units are the one gap it cannot close, and it says so rather than leaving a blank.** An
  `OptimizationProblem` carries none anywhere, and `quantities_are_plausible` reads the *setpoints*
  rather than a factor's levels — so nothing in this system catches a temperature factor whose 80
  might be °C or mol%. `notes` names every continuous parameter that came back unitless.
- A translated design still fails `evidence_present`, deliberately. Arms assembled from a
  campaign's arithmetic have cited nothing, and must not look as though they had.

## What keeps it true

- `tests/test_protocol_from_bo.py::test_a_candidate_becomes_an_arm_whose_levels_the_factors_declare`
- `tests/test_protocol_from_bo.py::test_a_repeated_run_is_a_replicate_rather_than_a_second_arm`
- `tests/test_protocol_from_bo.py::test_two_parameters_that_slug_to_one_factor_name_are_refused`
- `tests/test_protocol_from_bo.py::test_a_translated_design_clears_the_factor_and_arm_blockers`
- `tests/test_authz.py::test_every_advertised_tool_is_classified_write_or_read`
- `tests/test_context_floor.py::test_the_static_prefix_stays_under_its_ceiling`
