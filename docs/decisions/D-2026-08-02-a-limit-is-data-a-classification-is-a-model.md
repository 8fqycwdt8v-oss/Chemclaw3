# D-2026-08-02-a-limit-is-data-a-classification-is-a-model — A limit is data; a classification is a model

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-080 (the committed SMARTS hazard
table), D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet

## Context

The single most dangerous thing the live run did was recite a **correct** palladium PDE from
training as though it were the record. Correct makes it worse, not better: a correct recalled limit
teaches a reader to trust the next one, and there is nothing behind either. The same slice invented
mutagenic-impurity classifications, acceptable-intake limits and a worked purge factor. Grepping
this package for ICH M7, Ames, TTC or nitrosamine returned nothing — the model was answering
regulatory-toxicology questions from a table of energetic motifs, because that was the only safety
table there was.

The capability audit called this the **highest value-per-effort item in the whole 106 stories**:
Q3C and Q3D are published, static, transcribable tables. No schema change, no model, no corpus.

But the same section contains a trap that is easy to cost as data and is not. A structural *alert*
is a motif with a long published history. An **ICH M7 class**, a **purge factor** and an
**acceptable intake** are outputs of a model this system does not have — two complementary (Q)SARs
plus an Ames corpus and expert review. Shipping the alerts while letting the answer drift into a
classification would replace an uncited fabrication with a *cited-looking* one, which is strictly
worse.

## Decision

**Three tables, three modules, three questions, deliberately not merged.**

- `screen.py` (existing, D-080) — process safety: "is this safe to run today?" Its flags gate an
  agent-proposed procedure through the PR-gate.
- `genotox.py` + `genotox_alerts.yaml` — regulatory toxicology: "will this need a control
  strategy?" DNA-reactive motifs plus the nitrosamine formation route.
- `ich.py` + `ich_q3c.yaml` + `ich_q3d.yaml` — the numbers: Q3C residual-solvent limits and Q3D
  elemental-impurity PDEs, each row carrying guideline, revision and source table.

Not more rows in `rules.yaml`, and the reason is chemical rather than tidy: **nitrobenzene is an
ordinary reagent that the process-safety table is right to pass and the genotoxicity table is right
to flag.** One shared flag list would trip the `## Hazards` PR-gate on every nitration procedure —
so merging them would degrade the screen that works, not just muddle the one being added.

**The refusal ships in the payload, not in a docstring.** `AlertResult.verdict` names, on every
result, the exact four things the system cannot produce (M7 class, acceptable intake, purge factor,
(Q)SAR prediction). It is a `computed_field` so `model_dump()` carries it — the payload is what is
in the context window when the answer gets written, and a tool docstring is not. `ImpurityLimit`
does the same with its citation.

**A miss says what an absence means.** "No alerts" reads as "not mutagenic", which is a (Q)SAR
conclusion drawn from a nine-row table. Both result types therefore state the limit of the empty
case in the same words the hit uses for the limit of a flag. A substance the ICH tables do not
carry returns an explicit miss — never a nearby value, never a recalled one.

**These tables are package data, not settings, and that asymmetry is the point.** `safety_rules_path`
is configurable because a site extends the process-safety table with its own knowledge. **Nobody
has their own Q3C.** The values are fixed by a published guideline, and a deployment quietly
substituting a different PDE table is the failure mode rather than a feature — and a swapped table
would change what the disclaimer is attached to. They resolve against `__file__`, the way
`science/bo/benchmarks/reizman_suzuki.py` resolves its pinned benchmark data. A malformed table is
consequently a packaging fault and raises pydantic's own validation error rather than a bespoke
operator-facing one.

## Consequences

- Stories 8.14 and 8.16 are served outright, 8.15 improves and 8.10's remaining gap closes. The
  most dangerous fabrication class measured in the run has a cited answer to lose to.
- **ICH M7 classification, purge factors and acceptable intakes remain `MISSING-MODEL`** and are
  named as such in `docs/reference/user-story-capability-map.md`. The alerts do not partially
  deliver them; shipping alerts is not progress toward a classifier.
- **A Class 3 number is not the same kind of number as a Class 2 one, and the payload has to say
  so.** Q3C assigns Class 2 solvents a specific PDE; Table 3 is a *list of names* with no numeric
  column, and the 50 mg/day figure is a general statement — and a floor ("50 mg **or more** per
  day"), not an assignment. Rendered under a bare `basis="PDE"`, the machine-readable half asserted
  a solvent-specific limit the guideline does not contain, under a real citation. The basis strings
  are therefore per class and live in the YAML beside the numbers they label.
- The transcribed numbers are the risk this ADR carries. The 62 Q3C values, the 18 Q3D rows and
  their class assignments were checked row by row in an adversarial review; the citation on every
  row is what makes a transcription error findable by a human instead of invisible. **The one field
  that could not be verified offline is the Q3C revision label itself** ("R9, Step 4, 2024"), which
  appears on every Q3C citation — a backlog row, not a silent assumption.
- `GenotoxAlert` deliberately has **no severity field**, unlike `HazardFlag`: ranking alerts is the
  first half of a classification, and the published alert sets do not rank them either.
