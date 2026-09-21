# D-2026-09-20-the-chain-already-existed-and-it-is-called-a-template — a discriminating check may name a reviewed procedure, which is how it asks about structures nobody wrote down

**Status:** accepted · **Date:** 2026-09-20 · Closes the `Revisit when:` of
`D-2026-09-20-a-swept-axis-is-a-choice-an-invented-argument-is-a-lie`. Supersedes nothing: that
ADR's grounding rule is unchanged and this is a third target under it.

## Context

The dispatcher lets a check name a **tool** (one structure from one note) or a **job** (structures
by role, one swept axis). Both draw every argument from notes already in the corpus, and that is
the property the whole design rests on.

It is also a ceiling. A question whose subject is a *derived* set — this molecule's tautomers, its
protonation states, its breakable bonds — cannot be asked, because none of those structures is a
note anybody wrote down. A model listing them would be inventing structures, which is the failure
that ADR exists to prevent, in its worst form: a tautomer that is not the real one gives a pKa, a
dipole and a reaction energy that are each *a number about a different molecule*, with nothing
saying so.

That ceiling is what left `survey_bond_strengths`, `profile_rotation` and `scan_coordinate`
refused, and it is what made the chemist's "which molecule will be generated" question a lab
proposal rather than a check.

The trigger written for it said the fix was chaining a tool's output into a job's params.

## The finding

**That chaining already exists, and it is a `Template`.** Measured by resolving every
`${steps.<id>.result…}` reference in a non-agent step's arguments, **four** of the nine shipped
templates are an enumerator feeding a calculation:

| template | chain |
| --- | --- |
| `tautomer-resolution` | `enumerate_tautomers` → `rank_species` |
| `microspecies-profile` | `enumerate_protonation_states` → `rank_species` |
| `stereoisomer-ranking` | `enumerate_stereoisomers` → `rank_species` |
| `bond-strength-survey` | `enumerate_bond_cleavages` → `survey_bond_strengths` |

A fifth, `conformer-refinement`, chains the same way (`sample_conformers` →
`compute_thermochemistry`) but its upstream is a calculation rather than an enumeration, so it does
not answer a question about structures nobody wrote down. This ADR's first draft counted it, and
counted `degradant-triage` as `enumerate_degradants` → `screen_hazards`, which is simply false:
that template's screen reads `smiles: ["${inputs.smiles}"]` — the parent molecule — and its own
step `purpose` says so ("this one is second because it does not depend on the first"). Four is the
number, and the measurement is one script.

`bond-strength-survey` is one of the three refused jobs, already wired to the enumerator whose
entries `BondCleavageSpec` is documented as copying ("one bond to break, **as `chem`'s
`enumerate_bond_cleavages` reports it**"). The sibling fleet says the same from its side:
`SpeciesSet.smiles` is described as "the field Chemclaw3's templates pass straight into
`rank_species`, **by value**".

The two halves were built to fit each other. What was missing was not a mechanism; it was that
nothing let a *check* reach one.

## Decision

**A check may name a template, and supplies only its structure input.** Everything the grounding
rule says holds unchanged: the model selects a name and points at a note, the structure is read off
the resolved `compound` note, and every other declared input stays unset.

**A third target, not a second chaining mechanism, and the difference is measured rather than
aesthetic.** `tautomer-resolution` pins `level: thorough` — a conformer search per tautomer — on a
finding its own file records: acetylacetone ranked from one embedding per tautomer comes out 99.9%
keto and is **~80% enol** in the gas phase, because the enol is stabilised by an intramolecular
hydrogen bond that exists in one planar conformer and in none of the others. A chain assembled in
the dispatcher would carry none of that and would get the textbook case backwards while looking
entirely reasonable, which is this module's definition of the worst kind of wrong. Reaching the
reviewed procedure is how a check inherits decisions a check has no basis to make.

**Fail closed four times, and the set is `enabled()` rather than what is on disk.**

- **`enabled()`, not `discovered()`.** `templates_enabled` is a deployment's own switch, and it is
  what `registry.py` builds the `run_<template>` launchers from — so it is what
  `authz.side_effecting_tools()` covers, what a `tool_role_gates` entry can name, and what the plan
  gate sees. Grounding against the wider set let a tournament start a procedure that was turned
  off, through the one path the switch did not reach. Measured before the fix: with
  `CHEMCLAW_TEMPLATES_ENABLED=hazard-briefing`, `enabled()` returned one template and
  `discovered()` returned all nine.
- A template requiring any input beyond the structure is refused — nothing in the record supplies
  it, so the model would. All nine shipped templates require `smiles` and nothing else.
- A template that **acts** is refused outright: any step naming a tool in
  `authz.STATE_CHANGING_TOOLS`, or an `agent` step declaring `write_tools`. **No shipped template
  does, and that is precisely why the guard is here** — `AgentStep` says a template is not
  plan-gated because it *is* the pre-approved plan, approved for a *person* to run, which is not
  the same as approved for a tournament to start unattended. The first version of this guard read
  `write_tools` alone, which is `AgentStep`'s field: a `tool` step naming `record_knowledge_note`
  was invisible to it. The set is the in-process write set rather than `side_effecting_tools()`,
  which counts every declared job as durable work and would have refused the four chaining
  templates this feature exists to reach.
- A template that runs **no durable job** is refused. Two of the nine are a lookup and a narration;
  dispatched, each would spend a calculation slot and hand the verdict stage prose to read as
  though a calculator had produced it.

**The pre-flight is the template's own, not a second one.** `unrunnable_reason` (the runtime half
of `make template-validate`: can this deployment's connector set execute these steps) plus
`_params_model(template).model_validate(...)` — the same pair `templates/registry.start_template_run`
runs before any launch, so a tournament run and a chat run are checked by one authority.
`TemplateRunInput` already carries `roles`, so the authorization gap the job half had cannot recur
here. The launch matches it too: the same `execution_timeout`, and **no** retry policy, because
`BAD_DATA_RETRY` would re-run a whole procedure — metered model turn included — up to five times
on a transient in any one step.

**What the model is offered is derived from that same set**, not written into the prompt. A
hardcoded list is a second declaration of the dispatchable set: it does not track
`templates_enabled`, and a new template file would be invisible until somebody edited the string.

**A call naming two targets is refused rather than resolved by precedence.** A model that named
both a tool and a template did not decide, and picking one for it is this system making a silent
choice about which calculation runs — the same class of hidden assumption the dispatcher refuses,
made by us instead of by the model. **Refused, not *raised*:** the first version rejected it in a
`model_validator`, and since these calls are the model's own structured output — where a JSON
schema cannot express mutual exclusion — a `ValidationError` in `derive_check` is non-retryable bad
data, so the hypothesis lost its check entirely: no outcome, no code, no row, indistinguishable
from the model never having answered. Reported with a code, like every other grounding failure.

## Alternatives considered

**Ground an enumerator's output directly into a job's params.** This is what the trigger literally
asked for, and it is the wrong half of the problem: it rebuilds `TemplateWorkflow`'s substitution,
its per-step audit and its ordering, and it still leaves a check choosing `level` and `ranking` —
the arguments whose correct values are the reviewed finding. A second path through the same
chemistry is how two paths come to disagree, which `template_activities` was written about.

**Let a check select which enumerated entry to use** (a torsion by label, a cleavage by bond). This
would unlock `profile_rotation`, which takes exactly one torsion. Declined for now: selecting from
an enumeration is defensible on the swept-axis argument, but nothing needs it yet, and
`bond-strength-survey` shows the better shape — take the whole enumeration and let the calculation
rank it. Revisit below.

**Allow templates with a write tool, gated by the roles the request carries.** Declined: the
budget and the plan gate bound how much a tournament *computes*, and nothing bounds what it would
*do*. A check is an observation.

## Consequences

The chemist's tautomer, protonation-state, stereoisomer and bond-strength questions are now checks
a tournament answers rather than experiments it proposes, and they arrive with the defaults a
reviewer chose. `bond-strength-survey` reaches `survey_bond_strengths`, so one of the three refused
jobs is reachable through the door that grounds its argument.

`scan_coordinate` stays refused and should: it needs `values`, a coordinate grid, which no
enumerator produces and which a model would invent. That it did *not* unlock is the evidence that
this is a grounding rule rather than a list of exceptions.

**The cost is stated rather than waved at, because the first draft understated it.** A template
check spends one slot of `hypothesis_max_calculations`, and what that slot buys is *not* bounded by
a count: `tautomer-resolution` pins `level: thorough`, which is one CREST conformer search per
enumerated species, and `SpeciesRankingJobSpec.species` has no upper bound because an enumeration's
size is a property of the molecule and is chosen by nobody. There is deliberately no analogue of
`hypothesis_max_sweep_values` here — that cap exists because a model *chose* the swept values, and
nothing chose these. What bounds the run is wall clock: `execution_timeout` from
`template_run_timeout_seconds`, which is the same bound `start_template_run` passes and the same
one `run_ceiling_problems` refuses a template against during grounding. Launching without it, as
the first implementation did, gated the run on a limit nothing then applied.

**And every one of the nine ends in an `agent` step** — not two, as this ADR first said, which
understated its own cost disclosure by more than four times. So a template check always spends a
metered model turn, and `CheckOutcome.detail` is always that step's report over its steps' real
results rather than a calculator's output directly. `read_check_result` says so now; it previously
claimed the number always came off a calculator. Two consequences follow and both are taken: a
template with no `job` step is **refused**, so the report is at least a report *about* a
calculation; and the reading is defanged and celled like every other span this system did not
produce.

**Revisit when:** a check needs to name *one member* of an enumeration rather than the whole set —
`profile_rotation` takes a single `torsion`, and `TorsionSpec.label` is a human-readable handle the
enumerator produces, so selecting one is a selection from a validated vocabulary in exactly the way
a swept solvent is. Nothing needs it today. The file that shows it has fired is
`tests/test_hypothesis_dispatch.py::test_the_dispatchable_job_set_is_exactly_what_is_pinned`, whose
pinned set of 9 would grow to include `profile_rotation`.
