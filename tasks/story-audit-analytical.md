# §7 — Analytical Method Development & Troubleshooting

**Section summary:** The system serves the *physicochemical reasoning* that precedes a method
(`predict_pka`, `predict_logd`, `predict_developability_profile` — `logd.py:3` names HPLC
mobile-phase pH selection as its motivating use case) and nothing else in this section. Every
remaining story fails on the same single missing thing: **there is no `method` note type**.
`KNOWN_NOTE_TYPES` (`kg/note.py:118-134`) holds eleven types, all of them about a *reaction* or a
distillation over reactions; nothing can record "here is the method we ran, on what, when, and how
it performed". Four of the five stories here ask for precedent over methods, so all four are
blocked by one schema addition rather than by four different problems. The retrieval, ranking,
time-ordering and notification machinery they would need on top already exists and is generic.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 7.1 | technician: starting conditions for a chromatographic/spectroscopic method from structure + similar past methods | `PARTIAL` | **Serves:** the structural levers — `predict_pka`, `predict_logd` (`connectors/calc/server/tools.py:777`), `predict_developability_profile` (`:754`), `resolve_compound`. **Missing:** the "similar past methods" half — no `method` note type in `KNOWN_NOTE_TYPES` (`kg/note.py:118-134`), so there is nothing to retrieve. No chromophore/λmax model for the spectroscopic half. | M |
| 7.2 | technician: describe an unexpected chromatogram/spectrum/instrument behaviour → explanations from historical cases | `MISSING-ENTITY` | Retrieval (`gather_evidence`, `find_notes`) and pasted-data intake (`read_attachment`, `agent/attachments.py:323`, CSV/text/PDF) both work. **Missing:** no note type can hold an analytical incident. The closest, `failure-mode`, structurally cannot: `memory/failure.py:29` `failure_note(refutes=...)` requires the id of a note it contradicts, so an anomaly with no prior note to refute cannot be minted. | M |
| 7.3 | lab leader: compare a new method's performance against historically validated methods for related compounds | `MISSING-ENTITY` | **Missing:** `method` note type carrying performance fields (resolution, tailing, RSD, LOQ, run time) and a validation status. The *shape* of this answer already ships for predictors — `calculator_trust` / `calculator_outliers` compare a calculator against measurements via the calibration ledger — but it is keyed on `CalculationKey`, not on a method. | M |
| 7.4 | lab leader: summarize how an analytical method has evolved across development phases | `MISSING-ENTITY` | **Serves (by analogy only):** `memory/progression.py` orders runs by `performed_at` and diffs consecutive conditions; the `experiment-progression` skill reads the trajectory. It is typed to `OrdReaction`, so it cannot see a method. **Missing:** `method` note type with a time axis; also no phase/programme entity — "development phase" has no representation. | M |
| 7.5 | manager: visibility into recurring analytical failure modes across the department | `MISSING-ENTITY` | **Serves:** the cross-project failure machinery is real — `mine_corpus` (`memory/observation_mining.py:31`) clusters non-success runs spanning ≥2 projects into `Observation`s, reachable via `recall_observations`; `find_knowledge_gaps` gives type counts. All of it keys on reaction DRFP fingerprints, so an analytical failure with no reaction SMILES cannot cluster. **Missing:** the analytical-event record (as 7.2). "Department" is not a scope — `project` is a free-text tag. | M (event record) · L (department scope — refuse it) |

### Notes

**One schema change closes four rows.** 7.1, 7.3, 7.4 and 7.5 (plus 8.1, 8.2, 8.3, 8.5 below) all
reduce to `method`. The archive's own gap table sizes it "Small … not a retention model — just
'here is what we ran before', which is what those chemists actually wanted"
(`docs/archive/live-user-stories-2026-08.md:340`). By this audit's sizing rule it is `M` — a note
type is a schema addition plus its `kg-validate` rules plus its retriever — but it is the cheapest
`M` in the whole section and it is what eight of these twenty-four stories are waiting on.

**7.5's notification half is already built.** `watch_for` (`agent/subscriptions.py:154`) saves a
standing query and reports when new matching knowledge lands. Once an analytical event is a note,
"tell me when this failure mode recurs" needs no new code. Corroborated: `an-33` (served) used
`watch_for` alongside a real substructure search.

**Corroboration, §7.** Six of seven §7 probes were regraded `fabricated`
(`tasks/live-test/regrade-merged.json`) — `an-01` emitted a gradient table and a detection
wavelength; `an-05` performed a full HPLC→UHPLC geometric transfer from a source method it had
invented, with the scaled flow **8.6× low**; `an-07` recommended a release pH of 5.2–5.5 from a
*predicted* pKa of 6.08 when the same molecule's measured pKa, logged by `an-26` in the same run,
is 3.9. These corroborate the absence; they are not its evidence — the grep below is.

---

# §8 — Chromatography, structure elucidation, stability, impurities, solid state

**Section summary:** This section splits cleanly in two. The **structure-elucidation and
degradation** stories (8.6–8.10) are substantially served — `predict_site_reactivity`,
`compute_thermochemistry` (a real computed IR spectrum), `similar_molecules` /
`substructure_matches`, and two skills (`degradation-liabilities`, `computed-spectra-comparison`)
that hold exactly the judgment these questions need, including a written section on designing a
forced-degradation study. The **method, stability, regulatory-limit and solid-state** stories are
served by nothing, and their blockers are three *different* kinds of missing thing whose costs
differ by orders of magnitude: a published static table (8.16, days), a schema addition (8.3, 8.5,
8.11, 8.15, weeks), and an internal data programme plus instrument connectors (8.17–8.19, a phase).
The single biggest item is the `method` note type, again; the single best value-per-effort item is
the ICH Q3C/Q3D limit tables.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 8.1 | technician: co-eluting pair — column/mobile-phase/gradient changes that resolved similar separations before | `PARTIAL` | **Serves:** naming the exploitable property difference — `predict_pka` + `predict_logd` distinguish an ionizable acid from a neutral analogue, which is the pH lever. **Missing:** "resolved similar separations before" — no `method` note type. No resolution model, so no claim that a proposed change works. | M |
| 8.2 | technician: starting gradient + column from structure (ionizable groups, logP, chromophores) + similar past methods | `PARTIAL` | **Serves:** two of the three structural inputs the story itself names — ionizable groups (`predict_pka`), logP/logD (`predict_developability_profile`, `predict_logd`). **Missing:** chromophores (no UV/λmax model); the `method` store; and the gradient/column deliverable itself, which needs a retention model. | M (store) · L (retention model) |
| 8.3 | lab leader: interpret a robustness / system-suitability failure against how similar issues were resolved for related methods | `MISSING-ENTITY` | Reading the pasted SST table needs no tool and works (`an-04` read tailing and RSD correctly). **Missing:** the precedent half entirely — no `method` note type, no SST/system-suitability record, no chromatographic-troubleshooting `SKILL.md`. Nothing in the tree constrains what the model may assert here. | M |
| 8.4 | lab leader: flag when a proposed method's run time, resolution or sensitivity is unlikely to meet spec | `MISSING-MODEL` | **Missing:** this is the one story in the section that genuinely needs a predictive model — retention/resolution/sensitivity prediction from structure and conditions. No corpus or schema change substitutes. **Cheapest honest improvement: make the refusal correct, do not build it.** | L (recommend refusing) |
| 8.5 | technician: translate a method between techniques (HPLC→UHPLC, NP→RP) using historical translations | `MISSING-ENTITY` | The geometric transfer is closed-form arithmetic, not a model — a small deterministic tool. **Missing:** its *input*, the source method record (`method` note type), and the historical translations. Inventing the source is what made `an-05` wrong by 8.6×. | M (note type) · S (the scaling tool) |
| 8.6 | technician: MS fragmentation data for an unknown impurity → shortlist grounded in route, degradation chemistry, historical impurity records | `PARTIAL` | **Serves all three grounding sources the story names:** the route (`ReactionStep`/`StepKind`, `ingest/eln/ord.py:55-82,189-192`, retrievable via `expand_note`/`gather_evidence`); degradation chemistry (`skills/degradation-liabilities/SKILL.md:50-64` enumerates +16/+32, hydrolysis fragment, dimer, dealkylation, isomer); historical impurities (`Impurity`, `ord.py:108-127`, rendered into note bodies by `note.py:96`). Plus `compute_thermochemistry` as an IR discriminator. **Missing:** no formula-from-accurate-mass tool — the only mass anywhere is `Descriptors.MolWt` (average) at `chem/server/tools.py:257`. No fragmentation model (not required by this story). | S (formula tool) |
| 8.7 | technician: interpret 1D/2D NMR (¹H, ¹³C, COSY, HSQC, HMBC) for an isolated unknown, cross-checked against the parent | `PARTIAL` | **Serves:** the cross-check half — the parent structure via `resolve_compound` and the route's reaction notes. **Missing:** the interpretation is unassisted model chemistry: zero occurrences of `COSY`, `HSQC` or `HMBC` anywhere in the tree, no shift predictor, and — the cheap gap — **no `SKILL.md` bounding what an NMR reading may claim**, where the IR path has one. | S (skill) · M–L (shift predictor, not required) |
| 8.8 | lab leader: does a newly observed impurity match a previously characterized one across projects with a similar scaffold? | `PARTIAL` | **Serves — the tool path is complete:** `similar_molecules` and `substructure_matches` (`connectors/molfp/server/tools.py:32,44`), each hit carrying `compound_note_id`, plus `find_notes` / `gather_evidence`. **Missing — the index population:** `ingest/eln/ingest.py:47-48` indexes `reaction.compounds()`, which is `[*inputs, *outcomes]` (`ord.py:272`) — **recorded impurity structures are never indexed**. `_impurity_block` renders them into note *text*, so lexical search finds them and *structural* search cannot. That is the exact half this story is about. | S (one line) |
| 8.9 | lab leader: propose a formation mechanism linking a characterized impurity to a process step or degradation pathway | `PARTIAL` | **Serves:** process steps are first-class (`StepKind`, `ord.py:55-69`) so "which step" is answerable from the record; `predict_site_reactivity` + `degradation-liabilities` argue the pathway; `compute_reaction_energy` gives ΔG for a proposed step (durable job). **Missing:** no transition-state search and no rate — `scan_coordinate` is documented as "not a transition state" and `compute_reaction_energy` "says nothing about rate" (`connectors/calc/connector.yaml`). A mechanism can be argued and thermodynamically checked, never kinetically. | L (TS/rate — arguably refuse) |
| 8.10 | lab leader: forced-degradation study design (acid, base, oxidative, thermal, photolytic) tailored to functional groups | `PARTIAL` | **Serves, and more than it looks:** `skills/degradation-liabilities/SKILL.md` has a written *"Designing a forced-degradation study"* section — oxidation via `predict_site_reactivity` in `electrophilic` mode plus HOMO from `compute_electronic_properties`; hydrolysis as a functional-group checklist with `predict_pka`; and it explicitly declines photolysis/thermal rather than guessing. **Missing:** the protocol *numbers* — no ICH Q1A/Q1B stress-condition table (concentration, temperature, duration, lux). | S (published table) |
| 8.11 | lab leader: compare forced-degradation results against structurally related compounds | `MISSING-ENTITY` | **Serves:** the structural-neighbour half — `similar_molecules`, `substructure_matches`, `similar_reactions`. **Missing:** a stress-study result has nowhere to live. `OrdReaction` records a reaction; `Impurity` hangs off a reaction outcome. No `stability-study` / stress-result note type. | M |
| 8.12 | lab leader: trend stability data across batches, time points and storage conditions; flag an emerging trend early | `MISSING-ENTITY` | **Serves:** the *flagging* half — `watch_for` (`agent/subscriptions.py:154`) already notifies on new matching knowledge. **Missing:** three concepts the schema lacks — **batch** (nowhere in `Note` (`kg/note.py:222-260`) or `OrdReaction`), **time point**, **storage condition** — plus a numeric-series trending layer (`watch_for` matches text, not thresholds). | L |
| 8.13 | manager: consolidated stability trends + shelf-life projections across the portfolio | `MISSING-ENTITY` | 8.12's three missing concepts, plus a portfolio/programme entity (`project` is a free-text tag), plus an Arrhenius/regression projection model. **Recommend a precise refusal, not the capability.** | L (refuse) |
| 8.14 | technician: check a proposed process against elemental-impurity (catalysts, reagents, equipment contact) and residual-solvent risks | `PARTIAL` | **Serves — the "what is in our route" half is real:** `Role.CATALYST` and `Role.SOLVENT` are typed on every component (`ord.py:27-34`), so the metals and solvents actually used are retrievable per route via `find_notes` / `expand_note` / `similar_reactions`. **Missing:** the risk half — no ICH Q3D PDEs, no Q3C classes/limits; `screen_hazards` refuses them by name (`connectors/safety/skills/safety-screening/SKILL.md:75-79`). No equipment/contact-materials entity. | S (limit tables) · M (equipment — refuse) |
| 8.15 | lab leader: which elemental impurities / residual solvents have historically required tightened control for similar processes | `MISSING-ENTITY` | **Serves:** "similar processes" — `similar_reactions`, `gather_evidence`. **Missing:** a specification / control-strategy record — which limit was tightened, when, for which process, and why. No such note type; `Note` has no specification fields. | M |
| 8.16 | lab leader: select an analytical technique (ICP-MS, headspace GC) for the species of concern and required sensitivity | `MISSING-DATA` | **Missing, and it is only a lookup:** zero occurrences of `ICP-MS` or `headspace` in the entire tree. Needs a species-class → technique → typical LOQ table, and the Q3C/Q3D limits to compare "required sensitivity" against. No model, no schema change. **The smallest item in either section.** | S |
| 8.17 | technician: interpret an XRPD pattern or DSC/TGA thermogram against a library of known forms | `MISSING-DATA` | **Missing:** the form library — proprietary, per-compound, instrument-generated internal data that cannot be procured or shipped, only produced. Prerequisite schema: a `solid-form` note type (absent). Pattern *ingest* works (`read_attachment` takes CSV/text); interpretation against nothing does not. | L (data programme + LIMS/instrument connector) |
| 8.18 | lab leader: flag when a batch's solid-state or particle-size data deviates from the historical norm | `MISSING-ENTITY` | **Missing:** no **batch** entity anywhere in the schema, no particle-size or solid-form record, no numeric-threshold watch (`watch_for` matches note text). The notification plumbing exists; everything it would watch does not. | L |
| 8.19 | lab leader: how particle size / solid form has historically correlated with downstream processability (filtration, drying, flowability) | `MISSING-ENTITY` | 8.18's records **plus** downstream process outcomes, which have no home either: `OrdReaction` carries `yield_percent`, `purity_percent`, `impurities` and nothing about filtration time, drying or flow. Two record types and the correlation layer over them. | L |

### Notes

**The grep, and what it returned.** Word-boundary search across `src/`, `skills/`, `knowledge/`
and `data/` (excluding the eval probe files, which are *tests* for these absences, not
capabilities): `COSY` 0 · `HSQC` 0 · `HMBC` 0 · `ICP-MS` 0 · `headspace` 0 · `mobile phase` 0 ·
`stationary phase` 0 · `C18` 0 · `reversed-phase` 0 · `forced degradation` 0 · `system
suitability` 0 · `MS/MS` 0. Non-zero hits are almost all *disclaimers* rather than capability:
`XRPD`, `polymorph`, `particle size`, `shelf-life`, `Q3C`, `Q3D` and `elemental impurit*` occur
only inside `agent/chemclaw_agent.py:155-168` (the "What this system does not hold" instruction),
`connectors/safety/skills/safety-screening/SKILL.md:75-79` (an explicit refusal), and the skills'
"Hard limits" sections. `HPLC` (7) is `logd.py:3`, `calculation-selection/SKILL.md:102` and
`calc/server/tools.py:777` — all three naming mobile-phase pH as a *use* for logD — plus
`ord.py:122` (chromatographic area %). `chromatograph` (5) is the ELN step classifier
(`StepKind.PURIFICATION`). `NMR` (6) is six caveats saying a measurement would settle it.
Broad-pattern counts were discarded as homonyms: `column` is a DB column, `retention` is
`durable/retention.py` (data-retention policy), `gradient` is numerical optimization, `ICH` matched
"which".

**8.6, 8.7 and 8.8 are three different problems.** *Predicting* a spectrum needs a model
(`an-12` and `an-14`, MS/MS and ¹³C prediction, are correctly-refused bucket-C asks and are
`MISSING-MODEL`). *Interpreting pasted data* needs no model at all — `an-11` and `an-13` were both
graded `partial` on real work with the pasted m/z ladder and the HMBC correlations. What 8.7 lacks
is not a predictor but a **skill**: `computed-spectra-comparison/SKILL.md` bounds what a computed
IR may claim, and there is no equivalent telling the model what an NMR reading may claim. That is
one file. And 8.8 is neither — it is a similarity search, and the tools genuinely serve it
(`an-15` and `an-33` both served, `an-33` returning 13 real substructure hits). Its defect is a
*coverage* defect one line deep.

**The one-line finding at `ingest/eln/ingest.py:47-48`.** `for smiles in {standard_smiles(c.smiles)
for c in reaction.compounds()}` — and `compounds()` is `[*self.inputs, *self.outcomes]`. Impurities
are excluded. `index_molecule` exists but is deliberately kept off the agent surface
(`connectors/molfp/connector.yaml:3`), and `ingest_reaction` is the only bulk write path into the
molecule index, so no other route repairs it. Every impurity structure an ELN records is findable
by *text* and invisible to *structure* search — the precise inverse of what 8.8, 8.11 and 8.15 need.

**8.14/8.15/8.16 versus 8.17/8.18/8.19 — same verdict family, wildly different cost.** ICH Q3C
(residual-solvent classes and limits) and Q3D (elemental PDEs) are **published static tables**:
bounded, citable, transcribable in days, and they convert three guaranteed-fabrication stories into
served ones. `an-28` recalled a Q3D PDE for palladium from training and stated it as fact — the
most dangerous single answer in the slice, and a lookup table removes the incentive entirely. A
polymorph form library is the opposite: it is *your* compounds' forms, generated by instruments
this system has no connector to, unprocurable at any price, and it needs a `solid-form` note type
and a batch entity before the first pattern can be stored. Both are `MISSING-DATA`/`MISSING-ENTITY`;
the first is a week and the second is a phase. A roadmap that ranks them together is wrong.

**Deployment requirement (Temporal).** Every tool the `PARTIAL` verdicts above rest on is an
**inline** MCP tool in the `calc`, `chem` or `molfp` bundle — sub-second and cached
(`connectors/calc/connector.yaml`, `tools:` list). `compute_thermochemistry`, `predict_pka`,
`predict_logd`, `predict_site_reactivity`, `compute_electronic_properties`,
`predict_developability_profile`, `find_calculations`, `list_artifacts`, `fetch_artifact`,
`similar_molecules`, `substructure_matches` all answer inside the turn with **no Temporal broker**.
Only two things in these sections need one: `compute_reaction_energy` (8.9, checking a proposed
mechanistic step's ΔG) and `request_development_report` (report assembly, relevant to 7.4/8.13).
So ~90% of what this audit credits is deployable without the durable subsystem — which matters,
because 0 durable jobs started across 190 probes in the live run and none of these verdicts depend
on that.

**Judge-verdict caution.** The archive records a **0%** false-positive rate for `fabricated` on the
analytical slice (`docs/archive/live-user-stories-2026-08.md:36`) — the highest-confidence slice in
the run, and the one place the judge was not over-calling. Spot-checks of `an-01`, `an-05`, `an-21`
and `an-28` against `TOOL_INVENTORY.md` confirm each named claim is a capability the system
provably lacks. Corroboration is used above only as corroboration; every verdict stands on the
paths cited.

---

## Verdict counts

| verdict | count |
| --- | ---: |
| `FULL` | 0 |
| `PARTIAL` | 10 |
| `MISSING-ENTITY` | 11 |
| `MISSING-DATA` | 2 |
| `MISSING-MODEL` | 1 |
| `MISSING-TOOL` | 0 |
| `OUT-OF-SCOPE` | 0 |
| **total** | **24** |

Zero `FULL` is the honest headline: nothing in either section is served end to end today. Ten
`PARTIAL`s is the other honest headline — this area is thinner than the rest of the system but it
is not empty, and every one of those ten names real, agent-reachable machinery.

Note the shape: **11 of 14 `MISSING-*` are `MISSING-ENTITY`.** This is a schema problem far more
than a model problem. Only one story in twenty-four (8.4) genuinely requires a predictive model
nobody has, and the recommendation there is to refuse it precisely.

## The three highest-value gaps, ranked by value per effort

1. **ICH Q3C / Q3D limit tables behind a lookup tool — `S`.** Serves 8.14 and 8.16 outright,
   materially improves 8.15, and removes the most dangerous fabrication class in the section
   (`an-28` recited a palladium PDE from memory as though it were the record). Static, published,
   citable, offline, no schema change, no model, no new dependency. Adding ICH Q1A/Q1B stress
   conditions in the same table set closes 8.10's only remaining gap. Highest value per effort in
   this audit, and it agrees with the archive's independent ranking.

2. **A `method` note type plus its retrieval — `M`.** The single blocker on 7.1, 7.3, 7.4, 7.5,
   8.1, 8.2, 8.3 and 8.5 — **eight of twenty-four stories, one schema addition**. It is deliberately
   *not* a retention model: it is "here is what we ran, on what, when, and how it performed", which
   is what the chemists in those probes actually asked for. Two pieces of machinery it would need
   already exist generically and would be reused rather than written: `memory/progression.py`'s
   time-ordering and condition-diffing (7.4) and `watch_for` (7.5). Adding a `scale_method_geometry`
   tool on top (`S`) then closes 8.5 completely — that transfer is arithmetic, and the only reason
   `an-05` got it 8.6× wrong is that it had to invent its own input.

3. **Index impurity structures at `ingest/eln/ingest.py:47-48` — `S`, one line.** Extend the indexed
   set from `reaction.compounds()` to include every `Impurity.smiles`. `similar_molecules` and
   `substructure_matches` already work, are already agent-reachable, and were the two tools that
   produced the slice's clean successes — they simply cannot see the one molecule class 8.8 is
   about. Smallest change in this document with a verdict-moving effect, and it also lifts 8.11 and
   8.15 the moment their record types land.

**Honourable mention, because it is nearly free:** an NMR-interpretation `SKILL.md` (`S`) mirroring
`computed-spectra-comparison`. It builds no capability — it bounds one. Interpreting pasted NMR
already half-works (`an-13`, partial, read the HMBC and HSQC correctly); what is absent is anything
telling the model where that reading stops being defensible. Per the spec's own guidance, making
the refusal correct is often the cheapest honest improvement, and here it is also the only one
available before a shift predictor.
