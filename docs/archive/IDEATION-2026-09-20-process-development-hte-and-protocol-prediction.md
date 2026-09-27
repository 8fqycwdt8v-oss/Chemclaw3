# Ideation — synthesis planning, HTE campaigns and reaction-protocol prediction, from early process development to a kilo-lab campaign

**Point-in-time, 2026-09-20.** This is `docs/archive/`, so it is a record of what was true and what
was proposed on this date, and it is never updated. It closes no `BACKLOG.md` row, deletes no
`DEFERRED.md` row, and decides nothing — every item marked **[ADR]** below is a decision somebody
still has to take.

It answers one ask: *what skills and tools would this system need to plan HTE campaigns and predict
reaction protocols, across the whole arc from early process development to a late-development kilo
lab campaign?*

---

## 0. Three findings that change the shape of the answer

The honest answer is not a list of capabilities to invent. Most of the ideation below is reachable
because three structural facts came out of the survey first.

### 0.1 Most of the scale-up arithmetic is already built, and no deployment can reach it

`Chemclaw3-mcp` serves five process-development servers. **This tree declares a bundle for none of
them**, so `build_langgraph_agent` binds none of their tools, and only a deployment pointing
`CHEMCLAW_CONNECTORS_DIR` at the fleet's own `manifests/` directory reaches them — which
`infra/live/e2e-full-stack/up.sh` does and no chart deployment does.

| Fleet server | What it serves | Reachable from a chart deployment |
| --- | --- | --- |
| `thermalsafety` | `adiabatic_temperature_rise`, `mtsr`, `tmr_ad`, `stoessel_criticality_class`, `heat_removal_capacity`, `semenov_critical_ambient`, `oxygen_balance_screen` | **no** |
| `kinetics` | `rate_constant_at_temperature`, `activation_energy_from_two_rates`, `batch_conversion_after`, `batch_time_to_reach`, `continuous_reactor_conversion`, `semibatch_accumulation_profile` | **no** |
| `unitops` | `agitation_scale_up`, `just_suspended_speed`, `heat_transfer_time_constant`, `shortcut_distillation`, `crystallisation_yield`, `filtration_time`, `drying_time` | **no** |
| `props` | `list_solvents`, `solvent_properties`, `vapour_pressure`, `boiling_point_at_pressure`, `solvent_swap_candidates`, `compare_solvent_properties` | **no** |
| `suitability` | `replicate_precision`, `plate_count`, `peak_symmetry`, `peak_resolution`, `retention_factor`, `permitted_method_adjustment`, `system_suitability_report` | **no** |

This is already on the record:
`D-2026-09-15-a-capability-in-the-fleet-cannot-refute-a-denial-this-tree-declares-no-bundle-for`
refused to key a prompt denial on `adiabatic_temperature_rise` for exactly this reason — the block
would have dropped from no deployment, ever.

**So the largest single gap in "early process development to kilo lab" is not missing capability.
It is undeclared capability, and closing it is five `connector.yaml` files** — the shape `safety`
already has here: a manifest with an `endpoint:`, a `README.md`, and its own `skills/`, with no
`server/` because the capability is the fleet's.

The cost is real and is why this is **[ADR]** and not a chore. `core/config/connectors.py` says it
plainly: *discovery is enablement unless `connectors_enabled` narrows it*, and every bound tool's
description is serialised ahead of the system message on **every** model call. Declaring five
bundles moves the default surface for every deployment, against the ceiling
`tests/test_context_floor.CEILINGS["__default__"]` holds. `D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`
already declined wiring `rxnpredict` on that exact ground, calling it *"the highest-value single
addition to this pipeline"* and queueing it as its own ADR and its own PR. The same queue is the
right place for these five — and `data/profiles/` is the reason it need not be all-or-nothing.

### 0.2 The prescriptive tier is bench-scale by construction

`protocols/` is the right object and it cannot currently hold a kilo-lab batch.

- `ChargeLine` carries `amount_mmol`, `mass_mg`, `volume_ml` — three fixed-unit floats, in a
  repository whose `core/units.py` already has the whole g/kg and mL/L ladder.
- `checks.quantities_are_plausible` caps a charge at `_MAX_MASS_MG = 1_000_000` (1 kg) and
  `_MAX_VOLUME_ML = 20_000` (20 L), as a unit-mistake heuristic.
- So `data/evals/probes/process-chemistry.yaml`'s own opening probe — *20 kg in a 250 L jacketed
  reactor* — trips the envelope's plausibility check as a suspected unit error. **Corrected while
  implementing this: the tier does not *refuse* that scale, as this line first claimed.**
  `quantities_are_plausible` is a warning rather than a blocker, so the protocol stores. What it
  does is report every correct charge on a kilo-lab batch as a suspected unit error — which is the
  one kind of warning that teaches a chemist to stop reading the two checks beside it.
- There is no vessel, no working volume, no addition rate, no jacket setpoint, and no object for
  *a batch* — `DesignStatus` reaches `executed`, and N executions of one arm have nowhere to live.

What is already right and should not be rebuilt: `ProtocolStepKind` already spells `charge`,
`addition`, `hold`, `sampling`, `workup`, `purification`; `ProtocolBody` already carries
`in_process_controls`, `hazards` and `waste`; revisions are already CAS-guarded with a human/agent
`author_kind`. The envelope is close. It is the *quantities* that are bench-only.

### 0.3 The registers already hold the triggers this ask fires

Three `DEFERRED.md` rows and one stale premise are waiting on precisely this request:

- **Process flowsheet synthesis/simulation** — trigger, verbatim: *"Process design, not just
  reaction design, is in scope."* This ask is that sentence.
- **Retrosynthesis + reaction prediction** — trigger: *"route planning is a real user need"*, and
  the row already specifies the landing shape (a separate image declared here as a `url:` bundle,
  never an in-tree dependency, because `aizynthfinder` needs `numpy<2`/`networkx<3`/`rdkit<2024`
  against this tree's resolved versions). The fleet's own catalogue lists `retro` as **adopted, not
  built**: an existing `chemclaw2_retrosynthesis` server is the thing to integrate.
- **BoFire `DoEStrategy` (D/A/E/G/K/I-optimality) and `SpaceFillingCriterion`** — the dependency
  objection was measured false and deleted; only the use-case objection survives, with the trigger
  *"a story asking to design within a constrained continuous space with a stated run budget."*
  That is what `hte-campaign-design` already refuses today: a factorial *"enumerates corners and
  honours no limit"*, so a chemist's real constraint has no design generator.
- **`NChooseKConstraint`, `InterpointEqualityConstraint` and DoE blocking** — this row's stated
  premise is *stale*: it says a tree-wide search finds no plate, well, day or operator concept,
  and `D-2026-08-28` has since shipped `protocols.layout.place`, `plate_format` and a plate map.
  Blocking by plate/day/operator is reopenable on a premise that no longer holds.

---

## 1. The stage map

What a programme actually walks through, against what serves it today.

| # | Stage | The question | Served today by | Gap |
| --- | --- | --- | --- | --- |
| A | Route scouting | Which disconnections, and which survives scale? | nothing | no retro; no building-block/cost source; no route object |
| B | Reaction screening (HTE) | What goes on the plate? | `hte-campaign-design`, `protocols`, `generate_screening_design` | strong — but no results ever come back to the design; only factorial designs; no stock-solution feasibility |
| C | Optimization | Where is the optimum? | `experiment-design`, BO | no response-surface or mixture designs; no robustness design |
| D | Understanding | Why does it do that? | `experiment-progression`, `competing-hypotheses` | `kinetics` unreachable; no rate-law fit from a run table; no impurity-fate map |
| E | Thermal & process safety | Can we run it at 20 kg? | `safety-screening` (structural alerts only) | `thermalsafety` unreachable; **no judgment layer at all**; no gas-evolution tool |
| F | Isolation | Crystallisation, filtration, drying | nothing | `unitops` unreachable; no solid-form capability; no wash model |
| G | Equipment fit | Does it fit the plant we have? | nothing | `unitops` partially; no vessel-fit or campaign-throughput tool |
| H | Analytical readiness | Can we even measure it? | `analytical/` (spec verdicts, stability trend) | `suitability` unreachable; no mass-balance check; no IPC judgment |
| I | The package | Are we ready for 200 L? | `development-report`, `assemble_evidence_pack` | no composition skill; no campaign object above a protocol |

Stages E, F and G are where "late development kilo lab" lives, and they are the three with no
skill, no bundle and no judgment anywhere in this tree.

---

## 2. Skills to write (layer 3)

Ordered by value. Each is judgment only — the arithmetic is a tool's, per the layering rule.

### 2.1 `thermal-safety-assessment` — the highest-value missing skill in the system

Trigger: any scale-up question that names a temperature, an exotherm, a cooling failure, an
addition, a quench, or a vessel.

The judgment it has to hold, and that nothing holds today:

- **Every input to `thermalsafety` is a number a person measured** — a DSC onset, an ARC
  self-heat rate, an RC1 heat flow. That server *fits nothing and predicts nothing*. The skill's
  first move is therefore to ask for the measurement, not to estimate it.
- **A computed reaction enthalpy is not a process heat load.** The system prompt denies this
  unconditionally and correctly. A GFN2-xTB ΔH over a balanced equation is a ranking of
  alternatives; it is not ΔH_rxn for a charge, it omits mixing, dilution, crystallisation and
  quench heats, and it must never be fed to `adiabatic_temperature_rise` as though it were.
  **This is the sentence the skill exists to make unmissable.**
- The Stoessel frame: ΔT_ad → MTSR against T_boil and TD24 → criticality class 1–5. A class 3, 4
  or 5 is a *stop and talk to process safety*, never an answer.
- What no tool sees: gas evolution and pressure rise, secondary decomposition with a different
  onset than the DSC scan rate suggests, accumulation from a stalled reaction, and the fact that
  a dose time chosen for heat removal can be the dose time that maximises accumulation.
- The composition that is the actual question and that no single tool answers:
  `kinetics.semibatch_accumulation_profile` × ΔH_rxn → the MTSR that the *accumulated* reagent
  supports, not the MTSR of the fully-charged batch.

### 2.2 `scale-up-arithmetic` (mixing, heat transfer, equipment fit)

Trigger: "we are going from X to Y", a vessel volume, an agitator, a jacket.

- You cannot hold P/V, tip speed, blend time and Reynolds number constant at once. Which one you
  match is a decision about *what the chemistry is limited by* — gas-liquid mass transfer, solids
  suspension, a fast competitive reaction at the feed point — and the skill's job is to make the
  chemist name that limitation before `agitation_scale_up` is called at all.
- Surface-to-volume falls as volume rises; `heat_transfer_time_constant` is where that becomes a
  number, and it is why a 5 g reaction that "never got warm" is not evidence about 20 kg.
- The feed-point problem: at scale, a dosed reagent sees a locally huge excess before it mixes.
  This is where an impurity that never appeared on the bench appears — probe `pc-27` in one line.
- What does **not** scale: filtration, drying, cooling, distillation and hold times all get
  longer, and a protocol scaled by multiplying every charge has silently changed the chemistry's
  time-temperature history.

### 2.3 `crystallisation-design`

Trigger: an isolation, a yield-from-liquors question, an oil, a form.

- `unitops.crystallisation_yield` is **a mass balance over two measured solubilities**. It says
  nothing about whether you will get a solid, what habit, what form, or how long it takes.
- Metastable zone width, seeding temperature and seed load, why a linear cooling ramp is the
  wrong default, oiling-out, and the fact that a polymorph question is a *screening* question and
  this system has no solid-form capability at all (`solidform` is proposed-not-built in the fleet;
  `D-2026-08-27-a-solvate-is-not-its-solvent` is the nearest thing on the record).
- Mother-liquor loss is the yield you are choosing to give up, and it belongs in the PMI.

### 2.4 `solvent-swap-and-distillation`

Trigger: "swap DMF for 2-MeTHF", "distil before the crystallisation", a residual-solvent limit.

This one is pure composition and is the cheapest high-value skill in the list once `props` and
`unitops` are declared: `solvent_swap_candidates` (Hansen neighbourhood) → `compare_solvent_properties`
→ `boiling_point_at_pressure` (can you even distil it without cooking the product) →
`shortcut_distillation` (stages and carry-over) → `ich_impurity_limit` (is the carry-over under
Q3C). The existing `solvent-selection` skill is about a *free-energy* comparison over one
equilibrium and explicitly says the process constraints usually bind first; this is that other
half, and today it names none of those tools because none is reachable.

### 2.5 `kinetics-and-reactor-choice`

Trigger: a concentration-time table, "how long should I hold it", batch vs flow.

- What two (T, k) pairs buy and what they do not: an Arrhenius line through two points has no
  residual, and extrapolating it beyond the measured window is the common failure.
- `batch_time_to_reach` assumes isothermal and ideal; a real batch is neither.
- PFR vs CSTR at equal residence time is a statement about the *rate law*, and the skill's job is
  to check that the order was measured rather than assumed.
- The accumulation profile is a safety input before it is a kinetics output (hand off to 2.1).

### 2.6 `impurity-fate-and-purge`

Trigger: an impurity, a spec that will not clear, a genotoxic alert on an intermediate.

- Where it forms, where it goes, and where it is rejected — per unit operation, against the
  record.
- **Purge factors are measured, never predicted.** `safety-screening` already records that an
  invented purge factor and invented acceptable-intake limits were the failure that preceded the
  ICH table; this skill must inherit that refusal verbatim, not soften it.
- `enumerate_degradants` and `degradation-liabilities` already generate candidate structures; what
  is missing is the *fate* half — the route-level map from where a candidate can form to where it
  is controlled.

### 2.7 `robustness-and-edge-of-failure`

Trigger: late development, "prove it tolerates", "what is the range".

Structurally different from optimization and currently absent: you are not seeking an optimum, you
are bounding a region you can hold. Deliberate perturbation around a setpoint, edge-of-failure
runs, and the honest statement of what a small design can and cannot establish.

**Care required:** the system prompt denies critical process parameters, proven acceptable ranges
and design space. This skill must stop at *"here is what the runs show the process tolerated"* and
must not produce a PAR or a design space. Lifting that denial is **[ADR]**, and it should be taken
deliberately or not at all.

### 2.8 `route-scouting`

Trigger: a target, "how would we make this", a route comparison.

Blocked on capability (§3.2) but the judgment is writable now and is not the same as a
retrosynthesis tool's ranking: longest linear sequence and convergence, late-stage
diversification, cryogenics, chromatography, hazardous or restricted reagents, atom economy and
PMI, and whether the starting materials are actually buyable at scale.

### 2.9 `protocol-scale-translation`

Trigger: "we ran this at 1 g, give me the 2 kg version".

This is the single most concrete kilo-lab task and nothing serves it. Charges scale; the skill's
content is entirely **what does not** — addition time, cooling rate, filtration and drying, the
work-up volumes against vessel capacity, the hold times that appear because a step now takes a
shift. It pairs with the deterministic `rescale_protocol` tool in §3.3.

### 2.10 `analytical-readiness`

Trigger: a plate about to be run, a batch about to be released, an IPC.

An IPC answers *should I proceed*; a release test answers *is it acceptable* — different methods,
different criteria, different readers. `hte-campaign-design` already says decide the analytic
before the plate; this generalises it and adds the reading half: what `suitability` establishes
about whether a number is worth having at all, and what `analytical.evaluate`'s `not_measured` and
`indeterminate` verdicts oblige you to say.

Respect the denial: no column, gradient, flow rate, wavelength or retention time. Method
*readiness* is in scope; method *development* is denied.

### 2.11 `scale-up-readiness-review`

Trigger: probes `pc-23` and `pc-28` — *"put together what we know before the scale-up meeting"*.

The composition skill over everything above, and the one that must be most disciplined about
what it cannot supply: no tech-transfer package, no master batch record, no design space. What it
*can* produce is the honest shape — route, precedent, thermal envelope, equipment fit, unit
operations sized, analytics ready, **and an explicit list of what is still unknown and what
measurement would close each item**. The unknowns list is the deliverable, not the caveat.

### 2.12 `solid-form-screen-design`

Trigger: a salt screen, a cocrystal screen, a polymorph screen, a solubility plate.

Worth calling out because it breaks an assumption: `hte-campaign-design` assumes the plate is a
*reaction* screen — factors are reaction conditions, arms carry a `reaction_smiles`. A 96-well
polymorph or salt screen is an HTE campaign in every operational sense and fits none of that.
Either the envelope generalises or this is a sibling skill; that choice is **[ADR]**.

---

## 3. Tools, bundles and sources (layer 2, and the seams)

### 3.1 Bundles to declare here — no new capability, five manifests **[ADR]**

`thermalsafety`, `kinetics`, `unitops`, `props`, `suitability`. Each is a directory with a
`connector.yaml` (`endpoint:`, no `server/`), a `README.md`, and its `skills/` tree — exactly
`connectors/safety/`'s shape, which keeps its judgment here because *judgment is layer 3 and does
not follow the engine*.

This is the decision D-2026-09-15 left implicit and D-2026-08-28 queued for `rxnpredict`: it is a
**default-surface** decision, not a wiring change. Take it once, for all six bundles, with the
profile story decided in the same ADR.

### 3.2 Capability genuinely missing from the fleet

| Proposal | Where | Note |
| --- | --- | --- |
| `retro` — retrosynthetic disconnections | fleet, separate image, `url:` bundle | already *adopted, not built*; `chemclaw2_retrosynthesis` exists; DEFERRED trigger is user need, which this ask supplies |
| `solidform` — polymorph/salt/solvate | fleet, proposed | probe `pc-18` is unanswerable without it |
| `rate_law_from_runs` — order and Ea from a concentration-time table | fleet `kinetics` | probe `pc-07`; today only `activation_energy_from_two_rates` exists, which is two points and no residual |
| `variable_time_normalisation` (VTNA) | fleet `kinetics` | deterministic, cheap, and exactly what a process chemist runs on RPKA data |
| `gas_evolution` / pressure rise | fleet `thermalsafety` | the one thermal hazard with no tool; currently invisible |
| `wash_efficiency` — displacement vs dilution on a cake | fleet `unitops` | probe `pc-13` asks for wash volume; `filtration_time` does not answer it |
| `vessel_fit` — working volume, freeboard, charge fit, batches per campaign | fleet, new `equipment` or `unitops` | **the kilo-lab campaign-planning tool**; nothing anywhere computes it |
| `mass_balance_check` — assay + impurities against 100% | fleet `suitability` or here | cheap, deterministic, catches a bad analytical set before it becomes a conclusion |

### 3.3 Tools that belong *in this repository*

Per `D-2026-08-16-the-physics-leaves-the-cache-stays`: a primitive whose identity is derivable
from its inputs is a fleet server; **orchestration and composition stay here**.

- **`rescale_protocol`** — take a stored `ExperimentDesign` and a new basis, return a new revision
  with charges scaled and an explicit, structured list of every quantity that *did not* scale and
  must be re-decided. Deterministic; pairs with skill 2.9. Reuses `stoichiometry_table` arithmetic
  and the existing revision/diff machinery.
- **`attach_plate_results`** — **the loop this system does not close.** A design reaches
  `executed` and nothing attaches what the plate produced. Results enter only through `ingest/eln`
  as records with no link back to the design that prescribed them, so the plate → observations →
  `suggest_next_experiment` round trip that `hte-campaign-design` promises in its own closing
  section is handwork. Closing it also unlocks the DEFERRED row on mining human protocol
  edits — *"the highest-quality supervision this system can collect about its own suggestions,
  and it is currently written and never read."*
- **`plate_stock_plan`** — stock solutions, dispense volumes, dead volume, minimum dispensable
  volume, and whether every level is actually soluble at plate concentration. A plate that cannot
  be pipetted is a design check, not a chemistry question.
- **`screening_power`** — replicates needed to resolve an effect against a stated assay noise.
  `campaign_progress` already takes `assay_noise` for the plateau question; this is the same
  number used before the plate instead of after it.
- **`campaign_throughput`** — batches × cycle time → campaign duration and vessel occupancy.
  Orchestration over the record, so it is this repository's.

### 3.4 Two of these are **data sources**, not connectors

A connector *produces*, a source *supplies*. Building-block availability, price and lead time
(probe `pc-15`) is supplied, not computed: it is `ingest/sources/<name>/datasource.yaml` plus a
name in `CHEMCLAW_DATA_SOURCES`, with zero core edits — a supplier catalogue export or a mounted
share, exactly like the warehouse-ELN and SMB cases. A solvent-property corpus is the same shape
if a site has its own; `props` covers the general case.

Getting this wrong is the easy mistake here, and the rule is already written: a source *"cannot
acquire a write path by declaring one"*, and `connector-validate` bans a mutating tool on an
endpoint.

### 3.5 Design generators the optimization layer is missing

- **D-optimal / space-filling designs** for constrained continuous spaces — the objection is now
  use-case only, and §0.3 supplies the use case.
- **Response-surface designs** (central composite, Box-Behnken) — what process chemists actually
  run to characterise a region. `factorial_design` gives corners and centre points only.
- **Plackett-Burman / definitive screening** — many factors, few runs; the early-PD workload.
- **Mixture designs** — solvent blends that sum to one. `LinearConstraint` can express the
  constraint; no generator produces the design.
- **Blocking by plate, day or operator** — reopenable on a premise that is now stale (§0.3).

---

## 4. What a kilo-lab protocol needs from the envelope **[ADR]**

The elegant fix and the expedient one differ, and the elegant one is available because
`core/units.py` already exists.

- **Replace `ChargeLine`'s three fixed-unit floats with `Measurement`.** With a declared unit,
  `_MAX_MASS_MG`/`_MAX_VOLUME_ML` stop being a unit-mistake proxy and the plausibility band
  becomes *scale-relative* — checked against `request.scale` rather than against an absolute
  ceiling that encodes "this is a bench experiment". The band's purpose (catching a gram entered
  as a milligram) is better served by the declared scale than by an absolute cap that a real
  kilo-lab charge trips.
- **Add the batch fields**: vessel, working volume, and on `ProtocolStep` an addition rate and a
  jacket setpoint alongside the existing `temperature_c`. `ProtocolStepKind` already spells
  `addition` and `hold`; what is missing is the rate, not the kind.
- **An IPC-gated hold.** `in_process_controls` exists on the body; a `hold` step cannot yet say
  *hold until the IPC passes*, which is what a batch record's hold actually is.
- **A batch object.** N executions of one arm, each with its own analytics and its own deviations.
  `D-2026-07-31-a-campaign-is-an-entity-not-a-turn` is the precedent for the entity question, and
  `D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing` for how execution attaches.
- **Not a `protocol` note type.** `D-2026-08-28` declined it — a design is not a knowledge claim —
  and nothing here changes that.

---

## 5. Step templates — the cheapest wins in the whole document

All nine shipped templates are computational. A template is a fixed chain with no loops
(`D-2026-08-25-the-loop-is-a-composite-not-a-template`), which fits several process workflows
exactly, and adding one is a YAML file plus a `template-validate` run.

- `scale-up-thermal-envelope` — screen → ask for the measured DSC/ARC values → ΔT_ad → MTSR →
  Stoessel class → report, with the "this is not a calorimetry model" sentence built into the
  agent step rather than left to the model.
- `solvent-swap-design` — the §2.4 composition as a fixed chain.
- `crystallisation-first-pass` — solubility at two temperatures → yield → liquor loss → PMI
  contribution.
- `impurity-fate-map` — `enumerate_degradants` → hazard and genotox screen → ICH limit per
  candidate.
- `pre-scale-up-package` — probe `pc-28`, as a template rather than as a prompt.

---

## 6. What not to propose

From the declines, so nobody re-derives them:

- **An LLM judge over the answer.** `D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer`
  measured it: non-degenerate benefit zero, eight of ten clears were deletions — including a
  five-step protocol the user had explicitly asked for — and one cleared by fabricating citations.
- **A safety gate in CI.** `D-2026-08-15-safety-is-a-tool-not-a-gate` retired it deliberately and
  stated the cost out loud. A thermal-safety *skill* is not a gate and does not reopen this.
- **A `ProtocolDraftWorkflow`.** Declined: it puts chemistry judgment into orchestration code.
  Protocol judgment belongs in a `SKILL.md`.
- **A specialist router or challenge panel**, `reject_widening`, and a heavy compute tier. All
  declined, each with terms for return that this ask does not meet.
- **MACE weights.** Non-commercial academic licence; refused permanently, not deferred.

And the standing constraints any proposal here has to hold: **no runtime egress while answering**
(a corpus may be vendored at build time, a live API may not), and **semiempirical is the whole
tier** — *a decision inside GFN2-xTB's error bar is answered with an experiment, not an
escalation*, which is precisely why the thermal skill's first move must be to ask for a measured
number.

---

## 7. If only three things are taken from this

1. **Declare the five bundles** (§3.1). It is the largest capability gain available for the least
   new code in the system, and it is one ADR about the default surface.
2. **Write `thermal-safety-assessment`** (§2.1). Tools that compute an MTSR with no skill saying
   where their inputs come from are more dangerous than no tools, and the repository has already
   written that lesson once, about the hazard screen.
3. **Unfreeze the envelope's quantities** (§4). Until a charge can be a kilogram, every other
   kilo-lab capability lands next to a prescriptive tier that refuses to hold the result.
