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

**That chaining already exists, and it is a `Template`.** Five of the nine shipped templates are
exactly an enumerator feeding a calculation:

| template | chain |
| --- | --- |
| `tautomer-resolution` | `enumerate_tautomers` → `rank_species` |
| `microspecies-profile` | `enumerate_protonation_states` → `rank_species` |
| `stereoisomer-ranking` | `enumerate_stereoisomers` → `rank_species` |
| `bond-strength-survey` | `enumerate_bond_cleavages` → `survey_bond_strengths` |
| `degradant-triage` | `enumerate_degradants` → `screen_hazards` |

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

**Fail closed twice.**

- A template requiring any input beyond the structure is refused — nothing in the record supplies
  it, so the model would. All nine shipped templates require `smiles` and nothing else.
- A template whose `agent` step declares `write_tools` is refused outright. **No shipped template
  declares one, and that is precisely why the guard is here**: `AgentStep` says a template is not
  plan-gated because it *is* the pre-approved plan — approved for a person to run, which is not the
  same as approved for a tournament to start unattended. Without the guard, the first template to
  gain a write tool becomes tournament-launchable by omission.

**The pre-flight is the template's own, not a second one.** `unrunnable_reason` (the runtime half
of `make template-validate`: can this deployment's connector set execute these steps) plus
`_params_model(template).model_validate(...)` — the same pair `templates/registry.start_template_run`
runs before any launch, so a tournament run and a chat run are checked by one authority.
`TemplateRunInput` already carries `roles`, so the authorization gap the job half had cannot recur
here.

**A call naming two targets is refused rather than resolved by precedence.** A model that named
both a tool and a template did not decide, and picking one for it is this system making a silent
choice about which calculation runs — the same class of hidden assumption the dispatcher refuses,
made by us instead of by the model.

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

A template check costs one calculation against `hypothesis_max_calculations`, which is right — the
job inside it is the expensive part — and two of the nine end in an `agent` step, so those spend a
metered model call on prose the tournament then re-reads. Accepted rather than special-cased: the
alternative is a second dispatchable-template rule nobody would remember.

**Revisit when:** a check needs to name *one member* of an enumeration rather than the whole set —
`profile_rotation` takes a single `torsion`, and `TorsionSpec.label` is a human-readable handle the
enumerator produces, so selecting one is a selection from a validated vocabulary in exactly the way
a swept solvent is. Nothing needs it today. The file that shows it has fired is
`tests/test_hypothesis_dispatch.py::test_the_dispatchable_job_set_is_exactly_what_is_pinned`, whose
pinned set of 9 would grow to include `profile_rotation`.
