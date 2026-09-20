# A discriminating check that can name a template — plan

**Status:** plan. Closes the `BACKLOG.md` row
`D-2026-09-20-a-swept-axis-is-a-choice-an-invented-argument-is-a-lie` opened: *"a check can name a
note, and cannot name a tool's output."*

The previous occupant of this file was the dispatcher's own plan and review, merged as #425.

## The problem, restated

A check may name a **tool** (one structure) or a **job** (structures by role, one swept axis). Both
draw every argument from notes already in the corpus. So a question whose subject is a *derived*
set — this molecule's tautomers, its protonation states, its breakable bonds — cannot be asked at
all, because none of those structures is a note anybody wrote down. That is what leaves
`survey_bond_strengths`, `profile_rotation` and `scan_coordinate` refused, and it is what stops the
chemist's "which molecule will be generated" question being a check rather than a lab proposal.

## What I found, and why it changes the shape

The ADR's rewritten trigger says the missing piece is chaining a tool's output into a job's params.
**That chaining already exists, as a `Template`** — and five of the nine shipped templates *are*
this exact pattern:

| template | chain |
| --- | --- |
| `tautomer-resolution` | `enumerate_tautomers` → `rank_species` |
| `microspecies-profile` | `enumerate_protonation_states` → `rank_species` |
| `stereoisomer-ranking` | `enumerate_stereoisomers` → `rank_species` |
| `bond-strength-survey` | `enumerate_bond_cleavages` → **`survey_bond_strengths`** |
| `degradant-triage` | `enumerate_degradants` → `screen_hazards` |

`bond-strength-survey` is one of the three jobs the ADR refused, already wired to the enumerator
whose entries its spec is documented as copying. The sibling repo's `SpeciesSet.smiles` says so in
as many words: *"the field Chemclaw3's templates pass straight into `rank_species`, by value."*

**So the fix is not a new chaining mechanism. It is a third thing a check may name.** Building a
second enumerate-then-rank path would duplicate a reviewed seam and lose what that seam already
carries — `tautomer-resolution` pins `level: thorough` on a *measured* finding (acetylacetone ranks
99.9% keto from one embedding per tautomer and is ~80% enol in reality, because the enol's
intramolecular hydrogen bond exists in one planar conformer). A hand-rolled chain in the dispatcher
would get that wrong and look entirely reasonable.

## The shape

**A check may name a template, and supplies only its structure input.** Everything the rule already
says holds unchanged:

- the model **selects** a template name from a set derived offline, and **selects** a subject note;
- the structure is read off the resolved `compound` note, exactly as now;
- every other declared input stays unset, so the template's own reviewed defaults apply;
- a template declaring a required input that is *not* the structure is **refused** — fail closed,
  by the same derivation the job half uses.

The pre-flight is the template's own, not a second one: `unrunnable_reason` (this deployment's
connector set) plus `_params_model(template).model_validate(...)`, which is what
`templates/registry.py` runs before any launch. `TemplateRunInput` already carries `roles`, so the
authorization gap the last round found on the job half cannot reappear here.

## Steps

- [x] 1. `hypotheses/dispatch.py` — the template half, pure: `ground_template_inputs(declared,
      structure)` returning the inputs mapping or a `Refusal`. Fail closed on a required input that
      is not the structure.
- [x] 2. `CheckCall` gains `template: str`. A call naming more than one of tool/job/template is
      refused rather than resolved by precedence — precedence is a silent choice.
- [x] 3. `ground_check_template` activity: resolve the subject, run the template's own pre-flight,
      return a `_GroundedTemplate` carrying the **resolved** template pinned into it (the same
      rule `TemplateRunInput.template` states: an edit afterwards cannot change a live run).
- [x] 4. `_settle_templates`: launch `TemplateWorkflow` as a child under the shared budget. A
      template runs a job inside it, so it costs one calculation.
- [x] 5. `report.py` / `_template_line`: the `ran:` line names the template, the subject note and
      the inputs left at their defaults — same disclosure rule as the other two halves.
- [x] 6. `derive_check` prompt: teach the third shape, and that a template is **preferred** where
      one fits, because it carries defaults a check cannot supply.
- [x] 7. Tests: a ratchet deriving the dispatchable template set from the shipped catalogue; a
      refusal for a template needing a non-structure input; a refusal for a call naming two
      targets; end-to-end through a stubbed child workflow; the budget covering templates.
- [x] 8. ADR superseding nothing and closing the trigger, `BACKLOG.md` row deleted in the same
      commit, `SKILL.md` updated.
- [x] 9. `make lint type test` green with the daemon up, then PR and merge.

## What this will still not do

`scan_coordinate` stays refused and should: it needs `values`, a coordinate grid, which no
enumerator produces and which a model would invent. Saying so is the point — the other two unlock
because something real produces their arguments, and this one does not.

Ranking substitution products nobody has written down still needs a substitution-product
enumerator, which is a `Chemclaw3-mcp` question rather than this repo's.

## Review

**The plan survived contact, which is unusual here and is worth saying why.** The design work was
done before any code: reading the sibling repo's enumerators to see what they actually return, and
then finding that `data/templates/` already held five chains of exactly the needed shape. The
implementation after that was mechanical, because the decision — *a third target, not a second
mechanism* — had already been made against the evidence.

**What the reading changed.** My first sketch was `CheckCall.enumeration`: name an enumerator, call
it in the grounding activity, parse its output into the job's field. That would have worked and
would have been wrong. It rebuilds `TemplateWorkflow`'s substitution and per-step audit, and it
leaves the check choosing `level` and `ranking` — the two arguments whose correct values *are* the
reviewed finding. `tautomer-resolution`'s own comment is the evidence: acetylacetone ranks 99.9%
keto from one embedding per tautomer and is ~80% enol in reality. A dispatcher-side chain gets the
textbook case backwards and looks entirely reasonable.

**Two guards exist because of what is absent, not what is present.** No shipped template declares
`write_tools`, and no shipped template requires a second input. Both are refused anyway, because a
set that is safe today and dispatchable *by omission* is the shape the last review round found four
times in one diff.

**One thing the plan got wrong and the type checker caught**: `_GroundedTemplate.template` started
as `Any` with `arbitrary_types_allowed`, which crossed the Temporal wire as a bare dict and failed
at the child launch. Typing it `Template | None` round-trips it as the model `TemplateRunInput`
expects, and `mypy --strict` then forced the `None` to be handled at the launch site rather than
assumed away — which is a real refusal path, not a formality.

**Verification.** `make lint` and `make type` (944 files) green; `skill-validate` and
`prose-validate` pass; 199 tests across the dispatch, tournament, hypotheses and templates suites,
including an end-to-end launch of a stand-in `TemplateWorkflow` child — the previous job-half tests
all refused at grounding, so nothing had ever driven a real child launch before. Full `make test`
runs against Postgres and Temporal started locally so the Postgres-backed set is not skipped.
