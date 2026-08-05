# BO capability map

**What this is.** An audit of the Bayesian-optimization layer against the chemical- and
analytical-development use cases it is supposed to serve, and a roadmap out of the gap. It is the
BO-shaped sibling of `user-story-capability-map.md`, which verdicts all 106 stories across all
seventeen sections; this one takes the four sections where BO is the machinery (§2 planning, §3 BO,
§4 HTE, §7/§8 analytical method development) and asks a narrower question: **of what BoFire can do,
what do we use, what would each unused piece buy, and in what order should it be built.**

**Every claim about BoFire's runtime behaviour in this document was measured**, on
`bofire==0.4.1` / `botorch==0.18.1` / `torch==2.13.0`, and the numbers are in
`docs/decisions/D-2026-08-04-what-bofire-does-when-you-actually-run-it.md`. That is not diligence
theatre. The *last* BO roadmap was written from a code audit and was wrong: it said threading
`n_generators` through `factorial_design` was a one-line change because the parameter exists on the
imported class and its docstring explains it, and the parameter turned out to be **inert** on the
only domain shape that reaches it (D-2026-08-02, `tasks/lessons.md`). Three of the seven
measurements in that register changed a wave below, and one reversed a refusal.

| verdict | meaning |
| --- | --- |
| `SERVED` | A named, agent-reachable path answers the question end to end. |
| `PARTIAL` | Real machinery serves part of it; the row names which part it does not. |
| `UNREPRESENTABLE` | The question cannot be *asked* — the type model has no field for it. |
| `BLOCKED-UPSTREAM` | BoFire could serve it; something outside BO (a schema, a note type) cannot. |
| `REFUSED` | Deliberately not built. The ADR says why and what would reopen it. |

---

## 1. The wiring, in one table

Exactly one module imports BoFire — `science/bo/engine.py`, and `tests/test_connector_isolation.py`
plus `tests/test_workflow_registry.py` keep `bofire`/`botorch`/`torch` out of every core process.
`bofire[optimization,cheminfo]>=0.4.1` is declared (`pyproject.toml:15`), so both heavy extras are
installed: the BoTorch strategies are now substantially in use, the RDKit/Mordred featurisers are
deliberately not (`science/bo/featurize.py` uses cached GFN2-xTB descriptors instead, so a
suggestion can cite the calculations behind its search space — see the `DEFERRED.md` row).

**This section is dated.** It describes the wiring as it stood *before* W1–W5, which is what makes
it worth keeping: the roadmap below is read against it. The table's third column is therefore a
snapshot, and the rows W1–W5 moved say so.

| BoFire concept | in use | passed any configuration? |
| --- | --- | --- |
| input features | `ContinuousInput`, `CategoricalInput`, `CategoricalDescriptorInput` | bounds / categories / a descriptor matrix |
| output features | `ContinuousOutput` | one per objective; **more than one since W3** (`MoboStrategy`) |
| objectives | `MinimizeObjective`, `MaximizeObjective` | `w=1.0`, fixed |
| strategies | `RandomStrategy` (seeding), `SoboStrategy` / `MoboStrategy` (proposing, **W3**), `FractionalFactorialStrategy` (screens) | `domain` and `seed`; the fractional design's knobs since **W2** |
| acquisition function | qLogNEHVI on the multi-objective path (**W3**) | otherwise **never set**; BoFire's `SoboStrategy` default stands |
| surrogate | read back via `strategy.predict` and `cross_validate` (**W5**) | **never set**; BoFire picks per domain |
| `Domain` | `Domain(inputs=…, outputs=…, constraints=…)` | linear limits and categorical exclusions are passed since **W4** |

The neutral spec that crosses the connector boundary carries a list of objectives, singular no
longer (**W3**); `objective` remains as the lead one, which is what every persisted row holds:

```python
class OptimizationProblem(BaseModel):
    parameters: list[Parameter]              # ContinuousParameter | CategoricalParameter
    objectives: list[Objective]              # {name, direction}, lead first
    constraints: list[Constraint]            # linear limits and categorical exclusions (W4)
```

**What is good around it, so this does not read as a report of failure.** `featurize.py` turns a
categorical whose options are molecules into a `CategoricalDescriptorInput` over five cached
GFN2-xTB electronic descriptors, and carries the `calc_refs` out so a suggestion can cite the
calculations that shaped its search space — a provenance link most BO tooling does not have.
`campaign_record.py` identifies a campaign by a **hash of its decision space**, so three refinements
of one optimization accumulate on one row without anyone having to open a campaign first, and
`resume_campaign` closes the loop across sessions. `BoCampaignWorkflow` runs the multi-round loop
durably on the bundle's own Temporal queue and files its recommendation as a PR-gated `bo-candidate`
note. And `Candidate.predicted_sd` recovers BoFire's own posterior spread, so the optimizer's
statement about *why* it proposed a point reaches the note a human signs.

---

## 2. What it serves today

| # | The question | verdict | what serves it |
| --- | --- | --- | --- |
| 3.1 | which few experiments should I run next, given the results so far | `SERVED` | `suggest_next_experiment` → `propose_candidates` → SOBO over mixed continuous/categorical space; `count` gives a batch; inline, no Temporal |
| 3.2 | feed a result back and get the next conditions, across bench sessions | `SERVED` | `resume_campaign(campaign_id)` over the content-addressed campaign record |
| 4.3 | which reagents/catalysts should go in the campaign | `SERVED` for the electronic axis | `featurize.py`'s xTB descriptors let the surrogate speak about a ligand nobody has run. **No steric axis** — two ligands differing mainly in bulk look alike |
| 2.3 / 4.4 | a screening plan; the full grid or a smarter reduced one | `SERVED` | `generate_screening_design` — full grid or a fractional design whose `resolution` and `summary` state what was confounded, over categorical **and** continuous factors (the latter held at their two bounds and named as such), plus centre points, replication and seeded run-order randomisation (W2) |
| — | pick the best molecule from a library without evaluating all of it | `SERVED` | `molecule_library_problem` + candidate-set BO by exhaustive discrete acquisition |
| 3.4 | explain why it suggests these conditions (exploring vs exploiting) | `SERVED` | `ExperimentSuggestion.scale` gives what the objective spans in the runs supplied, and its `summary` reads each candidate's `predicted_sd` against that spread in three bands, naming a missing sd as a seed point rather than as confidence (W1). `predict_outcome` extends the same reading to a point the *chemist* named, with the cross-validated fit quality of the surrogate behind it (W5). That score is reported to two decimals because that is all it repeats to — the GP's hyperparameter fit is not deterministic, and measured over twelve identical calls R² spanned 0.906–0.969 and MAE 1.16–1.80 (D-2026-08-05-a-score-reported-more-precisely-than-it-repeats) |
| 3.3 | set up a BO from natural language: ranges, **constraints**, one **or more** objectives | `SERVED` | `objectives` is a list; `MoboStrategy` searches the trade-off and `ExperimentSuggestion.front` returns the non-dominated subset of the runs supplied, `best_of` raising rather than picking an axis (W3). An optional `assay_noise` draws that front at the chemist's own reproducibility, so two runs the assay cannot separate both stay on it. `constraints` carries a linear limit over continuous parameters (`<=`/`>=`/`==`, so a mixture summing to 1 comes free) or an exclusion forbidding a pairing of categorical options; both the seeding and the proposing strategy honour them, and a screen refuses them (W4) |
| 3.5 | has this optimization plateaued | `SERVED` | `campaign_progress(problem, observations, assay_noise, window)` — evaluations since a gain beyond the noise, the recent window's spread, a plateau verdict, and a summary stating the limit. `assay_noise` is required with no default, which is what stops `op-13`'s fabrication recurring with a tool behind it (W1). The gain is measured from the **last real gain**, not from the running best, so a campaign creeping upward in sub-noise steps is not called a plateau once the climb accumulates past the noise (D-2026-08-05, which found the first version reporting +20.9 against a ±2 assay as plateaued). `op-13`'s other half — is there an unexplored corner — is a posterior question, answered by predicting at corners and comparing sds (W5) |
| 4.5 | rank a completed campaign against my objectives | `PARTIAL` | plural objectives now expressible (W3), so what remains is the aggregate-over-a-set half — a retrieval gap, not a BO one |
| 3.6 | how much effort did optimization save versus screening | `PARTIAL` | no cost or labour field exists on any record, so the effort half stays blocked. The design-space half is served: `campaign_progress` reports distinct conditions against the full grid, which is a defensible efficiency claim with zero labour data (W1) |
| 7.x / 8.x | HPLC method development: starting conditions, robustness, transfer | `BLOCKED-UPSTREAM` | not a BO gap. See §4 |

**Two rows here read better than `user-story-capability-map.md` says, and both moved after that
audit was written.** 3.2 is scored `MISSING-TOOL` there — the campaign store had zero non-test
readers — and `resume_campaign` closed it
(`D-2026-08-02-shipped-is-not-reachable`). 2.3/4.4 are scored `PARTIAL` on the grounds that "the
only design producible is the complete Cartesian product", and reduced designs shipped
(`D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate`). Every other row above agrees
with the audit's verdict.

**The mismatch that matters is on the data side.** Every ELN run already carries `yield_percent`,
`purity_percent` *and* `impurities[].area_percent` (`ingest/eln/ord.py`), plus `temperature_c`,
`time_h` and per-component `amount_mmol`. That is a multi-response record feeding a
single-objective optimizer. The chemistry the corpus records is a trade-off; the tool can only be
told about one side of it.

---

## 3. What BoFire 0.4.1 ships that we do not use

Grouped by what it would buy. The verdict column resolves in §4/§5.

**Constraints** — `Domain(constraints=…)`, passed since W4; the rows below are what is *still* unused.

| class | the chemical question | verdict |
| --- | --- | --- |
| `LinearInequalityConstraint`, `LinearEqualityConstraint` | "base plus acid must not exceed 3 equivalents"; "water is at most 5% of the solvent"; "these three fractions sum to 1" | `SERVED` — one `LinearConstraint` over `<=`/`>=`/`==` (W4) |
| `CategoricalExcludeConstraint` (+ `Selection`/`Threshold` conditions) | "never combine Pd(OAc)₂ with DMSO" | `SERVED` for an all-categorical problem (W4). **Not** for a screen: measured, `FractionalFactorialStrategy` rejects every constraint class outright, so the roadmap's "expressible for a screen" did not survive |
| `NChooseKConstraint` | "at most 3 of these 8 additives" | `REFUSED` — no story |
| `InterpointEqualityConstraint` | "all four wells share one temperature" | `REFUSED` — needs a plate entity |
| `Nonlinear*`, `Product*` | — | `REFUSED` — `BotorchOptimizer` does not support nonlinear; would need pymoo |

**Multi-objective** — `MoboStrategy` (`qLogNEHVI`) shipped in **W3**, inline only. Still unused:
`QparegoStrategy`, the additive/multiplicative SOBO scalarisers, and
`bofire.utils.multiobjective.{get_pareto_front, compute_hypervolume, infer_ref_point}` — the front
is hand-written so `problem.py` stays importable in the agent process, and hypervolume has no caller.
`MoboStrategy`'s reference point is derived per objective from the data rather than set by us.

**Objectives beyond min/max** — `TargetObjective`, `CloseToTargetObjective`, the sigmoid family,
`MovingMaximizeSigmoidObjective`, the Derringer desirability family
(`Increasing`/`Decreasing`/`Peak`/`InRange`), `ConstrainedCategoricalObjective`, non-unit weights.
`REFUSED` for now: a scalarisation the chemist cannot audit, where a Pareto front is the honest
answer. `TargetObjective` ("hit exactly this") is the one with a real future case — an analytical
method target — and it is blocked on the missing `method` note type, not on BoFire.

**Design of experiments** — `DoEStrategy` with D/A/E/G/K/I-optimality and `SpaceFillingCriterion`
(model-based optimal design, and the only route to a real LHS): `REFUSED`, it needs cyipopt + SCIP.
Three of the four unused `FractionalFactorialStrategy` knobs — `n_center`, `n_repetitions`,
`randomize_runorder` — shipped in **W2**. `block_feature_key` did not: blocking needs a block
factor, and no plate, day or operator entity exists in `src/`.

**Features** — `DiscreteInput` (a numeric factor on a fixed grid: 5/10/20 mol%, today expressible
only as strings or as a continuous range), `CategoricalMolecularInput` + `Fingerprints` /
`Fragments` / `MordredDescriptors` (a second molecular representation), `CategoricalTaskInput`
(multi-fidelity), `CategoricalOutput`, `EngineeredFeatures`. All `REFUSED` or unscheduled — the
molecular ones because this repo already has a **better-grounded** featurisation, with a provenance
link into the calculation cache that a fingerprint has no equivalent of.

**Surrogates and diagnostics** — the whole model zoo is unconfigured, which is fine: BoFire's
per-domain default is a reasonable choice and naming a class would couple us to its model zoo
permanently. Reading the fit back was the gap, and **W5 closed it without naming a class**:
`predict_outcome` answers a chemist's what-if from `strategy.predict()` and reports
`cross_validate`'s score off the surrogate `strategy.surrogate_specs` says BoFire chose, so the
number describes the model that made the recommendation. `permutation_importance` is reachable the
same way and stays unbuilt (`DEFERRED.md`). `IterativeTrimming` (outlier-robust GP),
`PairwiseGPSurrogate` (preference learning), `RobustSingleTaskGPSurrogate`: no caller.

**Strategies with no caller at all** — `ActiveLearningStrategy`, `MultiFidelity*`,
`ShortestPathStrategy`, `StepwiseStrategy`, `LLMStrategy`, `EntingStrategy` (which defaults to a
commercial Gurobi solver). Listing them is the whole treatment they need.

---

## 4. The gap, by use-case family

Ranked by what actually binds, because the four families are blocked by four different kinds of
thing and a roadmap that calls them all "missing" is useless.

| family | what binds it | size | where |
| --- | --- | --- | --- |
| **Campaign health & transparency** | nothing computes numbers the system already asserts in prose. **No BoFire involvement at all** | `S` | W1 |
| **DoE / HTE screening** | a refusal in *our* code (`factorial_design` rejects continuous parameters) — and, measured, that refusal is what makes three of the four unused knobs inert | `S` | W2 |
| **Reaction / process optimisation** | the type model: one objective, no constraints. The responses already exist on `OrdReaction`; nothing has to be ingested or migrated | `M`+`M` | W3, W4 |
| **Analytical method development** | **not BO.** There is no `method` note type in `KNOWN_NOTE_TYPES` (`kg/note.py`) and no retention, resolution, tailing or RSD field anywhere in the schema — the story audit's grep counts `mobile phase` 0, `C18` 0, `system suitability` 0, `reversed-phase` 0 | `M` (schema) | not scheduled here |

**On analytical method development, stated plainly because it is the largest family by story count
(24 across §7/§8) and the one most likely to be mis-scheduled.** A method-development BO campaign
today has neither factors nor responses to sit on: nothing can record "we ran this gradient on this
column and it resolved these peaks", so there is nothing to seed a campaign from and nothing to
optimise toward. Shipping BO features for it first would be building on air. The prerequisite is a
`method` note type carrying performance fields — a schema addition of the same shape as the
`reaction` note that already exists, which `user-story-capability-map.md` scores as unblocking
**eight** stories on its own. Once it lands, three things become immediately useful and are already
named above: `TargetObjective` / `CloseToTargetObjective` for a stated spec ("resolution ≥ 2.0, run
time ≤ 12 min"), W2's centre points and replication for a robustness screen, and W4's linear
constraints for the couplings a gradient table has. None of them is worth building before it.

---

## 5. The roadmap

Five waves. Each is independently shippable and green under `make lint type test` on its own; none
depends on a later one. Sizes are engineering judgement, not estimates from a plan.

### W1 · `S` · Compute what the system already asserts

No BoFire change and no compatibility risk, and it goes first because it is what makes the later
waves legible — you cannot tell whether multi-objective helped without a convergence read.

- **`campaign_progress`** — best-so-far, improvement over the last *k* rounds, evaluations since a
  real improvement, and a plateau verdict; plus `design_space` (reuse `discrete_candidate_count`)
  beside the distinct-candidate count, which turns 3.6's cheap half into a computed claim.
  **`assay_noise` is a required argument with no default.** Probe `op-13` was graded *fabricated*
  for calling 1–2% gains real against a ±2% reproducibility the user had stated; a plateau test
  with a default noise would reproduce that error with a tool's authority behind it. It belongs in
  its own module under `science/bo/`, with no BoFire import — the arithmetic needs none, and keeping
  it BoFire-free means the types stay importable in the agent process.
- **An observed-spread scale on the suggestion return**, so `predicted_sd` can be read against what
  the objective's numbers actually span, and so a `None` sd is stated as "no surrogate had an
  opinion — this is a space-filling seed point" rather than read as endorsement.
- The explore/exploit section the `experiment-design` skill's front matter advertises and its body
  does not contain.

### W2 · `S` · Let a screen hold a continuous factor

Gated on M-5, which **partly refuted the plan and made this wave sharper**. On the all-categorical
domain `factorial_design` accepts today, `n_generators`, `n_repetitions` and `n_center` are *all*
inert — 8 runs at every value — and only `randomize_runorder` bites. So admitting continuous
factors is not a companion to those knobs, it is their **precondition**. On a mixed domain all four
work: `n_generators=1` halves 32 runs to 16, `n_center=0` returns exactly the corners at the two
bounds, and `n_repetitions=2` replicates the factorial part.

The refusal being removed (`engine.py:369-374`) exists because the class would *silently* fractionate
a continuous input to its two bounds, and a design that looks complete while quietly reshaping a
factor is worse than a clear refusal. That reasoning was right when a fractional design did not
exist. `_fractional_design` now performs exactly that re-encoding deliberately, and `ScreeningDesign`
carries a `resolution` and an unavoidable `summary` naming what was given up. The condition the
refusal was waiting for has been met.

Two traps the measurement found, both now in the diff: **`n_center` defaults to 1**, so a naive
change would start silently returning midpoint rows; and it adds `n_center` rows *per categorical
combination* (measured: 4·2^k + n_center·2^k), so the run count a chemist is handed is not
`corners + n_center`. `randomize_runorder` is available today and independently of everything else.

**Shipped** as `D-2026-08-04-a-screen-may-hold-a-continuous-factor-at-its-bounds`. One further
measurement gated the reduced half: M-8 asked whether real continuous factors and re-encoded
categoricals fractionate as one factor set, because if they did not, the stated resolution would
describe only part of the design. They do — 32/16/8 runs at `n_generators` 0/1/2 over five factors,
every one at exactly two levels.

### W3 · `M` · Multi-objective, inline only

Gated on M-1 and M-2, both of which came back clean. `MoboStrategy` validates with no reference
point (it derives a moving one per objective), fits at **n=2** — the same floor SOBO has, so
`MIN_SEED_OBSERVATIONS` is unchanged — and returns `<objective>_pred` / `_sd` per objective, the
same naming `_frame_to_candidates` already reads.

`objective: Objective` becomes `objectives: list[Objective]`, with a `mode="before"` validator that
accepts the singular spelling **permanently**: it is on disk in every `bo_campaigns.problem` row and
in every in-flight `CampaignSpec` in Temporal history, and rejecting it would fail a running
campaign at replay. `best_of` stays scalar and raises on multi-objective; a separate `pareto_front`
returns the non-dominated set in pure Python — deliberately not `compute_hypervolume`, because
`problem.py` is imported into the agent process as the campaign job's `params_model` and there is a
test whose job is to keep `torch` out of it.

Two things this wave must not get wrong. **The campaign-id hash** dumps parameters with a *denylist*
(`exclude={"descriptors"}`), so adding any field to a parameter forks every id in the database
invisibly — a new id, an empty history, a chemist told their campaign is new. M-2 captured the
baseline ids and confirmed that an allowlist reproduces them byte-identically; the new keys go into
the identity dict only when non-empty. And **the stale refusals must die in the same commit**: the
tool's "they are not partially supported, they are unrepresentable" and the skill's "pick the one
they lead with" become *wrong* the moment this lands, and a refusal instruction that outlives its
refusal teaches the model to refuse a capability that exists.

MOBO stays inline-only. The durable campaign's objective registry is
`Callable[..., Awaitable[float]]` with two demo entries, so a multi-output registry would be an
abstraction with zero real callers; the launch precondition refuses a multi-objective spec and
points at the inline tool.

### W4 · `M` · Constraints

Gated on M-3, which answered the question that decided this wave's size. The risk was never
`SoboStrategy` — it was `RandomStrategy`, which seeds every cold-start campaign: if it ignored
constraints, the schema would claim a limit was honoured while every seed point violated it.
Measured, **both honour them**: 0 violations of 20 random points, 0 of 5 SOBO proposals, and an
equality constraint puts 10 of 10 random points exactly on the simplex. So no rejection-sampling
path is needed, and the wave stays `M`.

One neutral `LinearConstraint{parameters, coefficients, relation, rhs}` covering `<=`, `>=`, `==` —
one kind, because a five-member discriminated union is the single biggest comprehensibility
regression available to an LLM-facing schema. BoFire itself refuses a constraint naming a
categorical (measured), so our validator exists to turn a pydantic error into a caller-fixable
sentence, not to be the safety.

The mixture/formulation case *is* `relation: "=="` and therefore comes free — ship the mechanism,
and say nothing about formulations in the skill until a dataset exists to validate it against.
`CategoricalExcludeConstraint` joins this wave in scoped form: measured, it is refused on a mixed
domain and works on a pure categorical one, so "never combine Pd(OAc)₂ with DMSO" is expressible for
an all-categorical campaign and refused with a clear message otherwise. **Corrected during the
build:** the sentence above originally said "for a screen and for an all-categorical campaign". M-4
had measured the exclusion against `SoboStrategy` and `RandomStrategy` only; measured against
`FractionalFactorialStrategy` (M-4c), *every* constraint class is rejected at strategy construction,
so a screen can carry none of them. The verdict table in §3 records what shipped.

`note_from_campaign_result`'s "Searched over:" block becomes untrue the moment constraints reach the
durable path — it would describe a box when the campaign searched a polytope — so it gains a
"Subject to:" block in the same diff.

### W5 · `S` · Interrogate the fitted surrogate

Gated on M-6 and M-7, **and this is the wave a measurement reversed**. The case against a
cross-validation tool was that reaching it means naming a surrogate class in `engine.py`,
permanently coupling us to BoFire's model zoo and risking a number that describes a different model
than the one that made the recommendation. Measured, that is not so: `strategy.surrogate_specs`
exposes the surrogate BoFire actually chose, `cross_validate` runs straight off it, and the number
therefore describes *the* model. Ten rows and five folds gave R² 0.948 / MAE 1.47 on a synthetic
series — a figure **since retracted**: the script that produced it passes `get_metric` a string
where an enum is required and raises, so the exact pair is not reproducible. The finding it
supported is, at 0.935 / 1.695 corrected and 0.950 / 1.36 through the shipped code
(D-2026-08-04-the-model-can-be-asked-not-only-obeyed). `shap` is already installed via `bofire[optimization]`, so nothing here costs a dependency.

Two capabilities, one fit:

- **`predict_outcome`** — "what would the model predict for 90 °C in toluene with L3?", the question
  a chemist asks *instead of* trusting a recommendation. Measured: `predict()` accepts a
  params-only frame and works on a featurized domain. Out-of-bounds input is **not clamped** — it
  extrapolates, and the sd rises about sixfold, which is an honest signal to surface rather than a
  reason to refuse.
- **Surrogate fit quality** — CV R² and MAE for the model behind the current recommendation. It
  needs the caveat that a CV score over ten observations will be over-read; the repo already has the
  pattern for that (a `computed_field` summary that reaches the context window at the moment the
  answer is composed, where a docstring does not).

Feature importance is *available* rather than blocked — `permutation_importance` needs no new
dependency — and is left out because an attribution over four parameters and ten runs is a number
this system cannot caveat well enough, and it has no second caller.

---

## 6. What this map does not tell you

- **Nothing here was run against a live campaign.** The BoFire behaviour is measured; the *use
  cases* are verdicted from the code and from `tasks/story-audit-optimization.md`. In the 190-probe
  live run, `suggest_next_experiment` was selected **zero** times — a skill-routing defect that has
  since been fixed and **not re-tested live**. A capability nothing routes to is not a served story.
- **The durable campaign has never run a real optimization.** `objectives.py` holds exactly two
  entries: a RandomForest emulator over vendored Reizman data, and a solubility maximiser. Neither
  is an optimization a chemist runs, so nothing in §5 is validated against a real automated loop.
- **The BO loop is still open at one end.** Nothing decides that an ingested `reaction` note *is*
  the execution of a proposed candidate. That needs a matching rule over conditions with tolerances
  on parameters an ELN records inconsistently, and getting it wrong attributes a result to an
  experiment nobody ran — worse than the open loop. It is its own decision, and none of the five
  waves closes it.
- **Sizes are engineering judgement**, not estimates from a plan.
- The measurements were run on one machine, on `bofire==0.4.1`. A version bump invalidates them,
  and the tests each wave carries are what stop a finding from rotting.
