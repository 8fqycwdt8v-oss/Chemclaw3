# D-2026-08-27-eighteen-names-for-a-primitive-set — the seventeen are probed, the pair is two capabilities, and the reduction that exists is a profile edit

## Status

Accepted.

## Context

`docs/planning/BACKLOG.md` carries a row opened by the two gates that landed on 2026-08-25. The GFN
multi-step merge (`096cd5a`) added eighteen agent-callable names in one change and took the
`default` profile's static prefix from **18,805 to 24,838 tokens — +32% on what every turn costs
before the user says anything**. `tests/test_context_floor.py` was on a branch at the time, so the
first thing the ratchet did was report a cost that had already been paid, and its ceiling was raised
rather than the merge blocked. `tests/test_probe_coverage.py` recorded the same event from the other
side: the names went into `_GRANDFATHERED_AT_INTRODUCTION`, a list that claims nothing and that the
suite forbids growing.

The row owed two things — the probes, and a look at whether the eighteen need to be eighteen
*advertised* names — and named `run_bond_strength_survey` beside `survey_bond_strengths` as the
example. It also said which commit would prove the second half happened: the one that brings the
ceiling back down.

Four measurements were taken before anything was decided, and three of them change what the row
says.

**The eighteen are seventeen, and the eighteenth is already accounted for.** `096cd5a` added seven
templates, four `calc` durable jobs and seven `chem` endpoint tools. `transform_structure` was the
seventh `chem` name and was deleted from the manifest rather than implemented — it had no caller, no
template, no skill reference and no signature in either repository — which is why the frozen
baseline holds seventeen and why `§ 1`'s row in `BACKLOG.md` already says so. The row's "eighteen"
was accurate when written; the debt to drain is seventeen.

**The ceiling is not 27,500 and has not been since the day after.** It is **29,500**, raised three
times, each by a different tool on a branch that could not see the others (`profile_rotation`,
`rank_species_across_solvents`, `predict_pka_ensemble`), each argued in its own ADR and each
recorded in the constant's comment. The `default` profile measures **28,114 tokens** on this
checkout.

**Only eleven of the seventeen are in the number the ratchet gates on.** `_floor()` counts
`_capability_tools`, which is in-process tools plus templates plus job wrappers. The six
`enumerate_*`/`describe_topology` names are `chem` **endpoint** tools, and an endpoint tool's schema
comes from a running server, so `test_context_floor.py` cannot see them at all. The four `calc` jobs
and the seven templates are what the default profile actually pays for, and they are 5,787 tokens of
it.

**The two names the row pairs are not the same shape as each other.** `survey_bond_strengths` is a
`CalcJobWorkflow` job whose `BondSurveyJobSpec.cleavages` is `min_length=1` and mandatory;
`run_bond_strength_survey` is a core `TemplateWorkflow` over three steps —
`chem.enumerate_bond_cleavages`, then that job, then a written report.

## Decision

### 1. The seventeen are probed, and the grandfathered list is deleted rather than emptied

`data/evals/probes/multistep-calculation.yaml` holds seventeen probes, one per name, each a question
whose natural answer requires that tool. Two shapes, deliberately:

- **A probe for a primitive hands it the set.** `ms-07` supplies the two methylpyrazole tautomers
  and asks which is present in DMSO; `ms-08` asks for the bond survey re-run at `standard` level and
  313 K; `ms-09` asks for ten members re-weighted by free energy. Each of those is a control the
  wrapping template does not expose, which is the same evidence §2 turns on.
- **A probe for a protocol starts from a SMILES and names both routes.** `expects_tools` is ANY-OF
  precisely so a probe does not grade routing taste, so `run_tautomer_resolution` sits beside
  `rank_species` in `ms-11` — either is a good answer, and what is graded is whether the ranking was
  over an *enumerated* set rather than over forms the model wrote out itself. That failure is the
  one worth building a corpus around: it is silent, and it reads exactly like the right answer.

`_GRANDFATHERED_AT_INTRODUCTION`, `_grandfathered()` and `test_the_grandfathered_set_can_only_shrink`
are **deleted**, not left holding a drained set — the rule `DEFERRED.md`, `BACKLOG.md` and
`test_context_floor.py::KNOWN_OVERSIZED` all run on, that a debt list outliving its debt reads as
live state. Enforcement is unchanged in both directions: the list was a closed, dated record that
nothing could ever be added to, so removing it takes nothing away, and
`test_every_agent_callable_tool_is_probed_or_exempt` now admits no grandfathering at all.

### 2. `run_bond_strength_survey` and `survey_bond_strengths` are two capabilities, and are not collapsed

`D-2026-08-26-a-tool-name-is-one-capability-or-it-is-neither` is the rule to apply and it is not
violated here: it says one *declared name* is one capability across the enabled set, and these are
two names over two implementations with no collision — `registry._declared_tool_names()` has nothing
to say about them. What is genuinely in question is its sibling, the rule that deliberate overlap
under *different* names must be argued in writing. Read against both implementations, the argument
holds:

- **The required inputs differ, and not cosmetically.** `cleavages` is mandatory, so the job
  *cannot* answer from a SMILES; the template starts from one. That split is
  `D-2026-08-25-the-loop-is-a-composite-not-a-template`'s decision and it is enforced nowhere else:
  a ranking over a set nobody enumerated is confident about the wrong universe and looks correct.
- **The exposed controls differ.** The job takes `level` and `temperature_k`; the template pins both
  and exposes `smiles` and `solvent`.
- **The returns differ.** Ranked dissociation energies against a written forced-degradation study.
- **They are different execution paths** — core's `TemplateWorkflow` against the `connector-calc`
  queue's `CalcJobWorkflow` — and therefore different authorization keys.

Collapsing them would delete a capability rather than a name. `connectors/calc/connector.yaml` and
`connectors/calc/server/tools.py` are therefore untouched by this decision.

### 3. The row's example is not the strongest case, and the measurement names the real one

Two of the nine templates are **single-job wrappers**, which the row's pair is not:

| Template | What it does | Tokens on the default prefix |
| --- | --- | --- |
| `run_ensemble_free_energy` | one `job:` step — `refine_ensemble(smiles, solvent)` — plus a report prompt | 348 |
| `run_regioselectivity_in_conformer` | one `job:` step — `compute_ensemble_property(smiles, solvent, prop=fukui)` — plus a report prompt | 333 |

Neither sequences anything. The second at least pins `prop`, which turns a general property averager
into a named question; the first pins nothing at all and is `refine_ensemble` with three of its
arguments removed (`top_n`, `temperature_k`, `structure_id`) and a report attached. What both add is
*reporting discipline*, which is layer 3's job and is already written down in
`skills/ensemble-workflows/SKILL.md` — including the routing table that points at them.

This is **recorded, not acted on**: deleting a named protocol the shipped skill routes to is a
capability deletion with its own argument, it touches `data/templates/` and
`data/profiles/computation.yaml`, and 681 tokens does not move the ratchet on its own. It is stated
here because the row asked the question and the honest answer is that its example is sound and a
different pair is not.

### 4. The reduction that exists is a profile narrowing, and here is what it is worth

`default` has `tool_names=None` — every in-process tool, every template, every job wrapper. The
`computation` profile already names all seventeen of these and already runs the harness at
`plan_only` for exactly the cost reason. So the narrowing needs no capability to be removed from the
deployment: it is an allow-list on `default` and a routing decision.

Measured on this checkout, each by compiling a real profile and re-running `_floor`:

| `default` carries | Prefix | Δ |
| --- | ---: | ---: |
| everything (today) | 28,114 | — |
| minus the two single-job wrapper templates | 27,433 | −681 |
| minus the seven GFN templates | 25,399 | −2,715 |
| minus the four GFN jobs | 25,042 | −3,072 |
| minus all nine `run_*` templates | 24,828 | −3,286 |
| **minus all eleven GFN names it carries** | **22,327** | **−5,787 (−21%)** |

Two things that measurement says beyond the number. The saving is **flat in the six
`enumerate_*`/`describe_topology` names** — dropping all seventeen measures the same 22,327 as
dropping the eleven, because the other six are endpoint tools this ratchet cannot see. And the
skills listing does not move: 3,034 tokens in every row above, because `ensemble-workflows` stays
listed after every tool it routes to has been removed from the profile. A skill that survives the
removal of its whole tool set is a listing entry describing an unreachable workflow, so the
narrowing is a `default` allow-list *and* a look at the skill gate, not a one-line edit.

### 5. The ceiling is not lowered, and that is the finding rather than an omission

Measured after this change: **28,114 tokens — identical**. This change adds a probe file, deletes a
drained list and touches no tool schema, so there is no reduction to prove. The row says the commit
that lowers the ceiling is the proof that the second half happened; lowering it here would be the
claim without the thing, which is the exact failure mode this repository keeps recording. `CEILINGS`
keeps 29,500 and gains a paragraph saying it was examined on 2026-08-27, what the prefix measured,
and where the −5,787 lives.

## Consequences

- **The probe gate has no grandfathering left.** A new agent-callable tool now needs a probe, or an
  `EXEMPT` entry naming what covers it instead, in the pull request that adds it. That is the
  intended end state and it arrives without a further decision.
- **The corpus is 278 probes and less concentrated**, not more: `gather_evidence` falls from 124 of
  261 (47.5%) to 124 of 278 (44.6%) against the 60% bound in
  `test_the_corpus_is_not_concentrated_on_one_tool`.
- **Seventeen probes name durable work.** Every one carries `expects_job: true`, so a run resolves
  the launched workflow ids against Temporal rather than believing the turn's account of them — the
  distinction `durable.yaml` was written to make. They therefore measure nothing new against a front
  door with no broker, which is the same caveat that file carries.
- **`docs/planning/BACKLOG.md` is not edited here** — it is outside this change. Its row's first half
  is discharged by §1 and its second half is answered by §2 and §3; what should survive it is §4,
  with the measured −5,787 and the skill-gate half attached.

## Notes

The row's framing — "`enumerate_*`/`run_*` reads like a primitive set that a profile could narrow
rather than every turn carrying all of it" — is right, and the measurement is what makes it
actionable rather than plausible. What it could not have known is that six of those names cost the
gated number nothing, so the narrowing is worth 5,787 tokens and not the whole seventeen-name
surface it appears to be. The general point is the one `test_context_floor.py` already makes about
itself in a different register: **the ratchet measures the in-process prefix, and a bundle whose
tools are served rather than declared is invisible to it.** That is not a defect to fix here — an
endpoint schema is not knowable without a running server — but it does mean a merge that adds
capability entirely as endpoint tools passes this gate for free, and the next person reasoning about
"what a tool costs on every turn" should know which half of the bill this file sees.
