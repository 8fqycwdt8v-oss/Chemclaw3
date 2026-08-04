# D-2026-08-04-what-bofire-does-when-you-actually-run-it — What BoFire does when you actually run it, and the roadmap that survived it

**Status:** accepted · **Date:** 2026-08-04 · **Extends:** D-012 (BoFire is the BO engine),
D-092 (the capability survey that added `generate_screening_design`),
D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate (the measurement that refuted the
last roadmap)

## Context

`suggest_next_experiment` is the strongest single capability in the story audit
(`tasks/story-audit-optimization.md` §3.1) and it sits on a very narrow slice of BoFire: three
strategies, two objectives, four feature types, no constraints, no multi-objective, and no
configured acquisition function or surrogate. `bofire[optimization,cheminfo]>=0.4.1` is declared, so
both heavy extras are installed and almost entirely unused.

The demand side is not speculative. Every ELN run already carries `yield_percent`, `purity_percent`
**and** `impurities[].area_percent`, so the corpus records a trade-off that the optimizer can only
be told one side of. Story 3.3 is `PARTIAL` with multi-objective and constraints marked
*unrepresentable*; live probe `op-16` was graded *fabricated* for promising to call the tool "with
both objectives"; `op-13` was graded *fabricated* for calling 1–2% gains real against a ±2%
reproducibility the user had stated in the question.

**The reason this ADR leads with measurements.** The previous BO roadmap was written from a code
audit and was wrong. It said threading `n_generators` through `factorial_design` was a one-line
change — the parameter exists on the imported class and its docstring explains it — and the
parameter was **inert** on the only domain shape that reaches it. `tasks/lessons.md` records the
rule that came out of it: *a plan item that says "just thread X through" gets X measured before an
agent is told to thread it*. A roadmap is precisely the artefact that tells a future agent to thread
it, so the register below was run before the roadmap was written, not after.

## Decision

**The analysis and the roadmap live in `docs/reference/bo-capability-map.md`**, beside
`user-story-capability-map.md` and in its style. This ADR holds the measured numbers and the
refusals, because those are the two parts that must not be silently re-litigated.

### The measurement register — seven questions, run on `bofire==0.4.1` / `botorch==0.18.1`

**M-1 · `MoboStrategy` on the domain shape `_to_domain` builds.** Validates with `ref_point` unset,
defaulting to an `ExplicitReferencePoint` of `AbsoluteMovingReferenceValue(orient_at_best=False,
offset=0.0)` per objective, acquisition `qLogNEHVI`. `tell`+`ask` succeeds at **n=2** — the same
floor SOBO has, so `MIN_SEED_OBSERVATIONS` does not change. Two outputs give
`yield_pred, impurity_pred, yield_sd, impurity_sd, yield_des, impurity_des`: per objective, in the
naming `_frame_to_candidates` already reads. A row with `valid_<objective>=0` and NaN is accepted.
Same seed → identical candidates.

**M-2 · the campaign-id baseline, captured before any edit.** `campaign_id_for` hashes the decision
space with a *denylist* (`exclude={"descriptors"}`), so adding any field to a parameter — ever —
forks every campaign id in the database, and the failure is invisible: a new id, an empty history, a
chemist told their campaign is new.

| case | id |
|---|---|
| continuous-only | `campaign-6958b7edaa261c83` |
| continuous + categorical | `campaign-55e5f929fe83a9a5` |
| categorical with `structures` | `campaign-109f34eac28892ab` |

An allowlist of `{kind, name, lower, upper, categories, structures}` reproduces all three
byte-identically. That is the fix, and these ids are what pins it.

**M-3 · are constraints honoured, and by which strategy?** The risk was never `SoboStrategy`. It was
`RandomStrategy`, which seeds every cold-start campaign: had it ignored `Domain.constraints`, the
schema would have claimed a limit was honoured while every seed point violated it — the same
"parameter present, behaviour absent" shape as `n_generators`. Measured:

- `LinearInequalityConstraint(features=[a,b], coefficients=[1,1], rhs=3)` means **a+b ≤ 3**
- SOBO: **0 violations of 5** proposals
- `RandomStrategy`: **0 violations of 20** points → no rejection-sampling path is needed
- equality `x1+x2+x3 == 1`: **10 of 10** random points exactly on the simplex
- a constraint naming a `CategoricalInput` is **refused by BoFire itself**
  (`Feature solvent is not a continuous input feature`)

**M-4 · `CategoricalExcludeConstraint`.** Refused on a mixed domain — "can only be used for pure
categorical/discrete search spaces" — and **works on a pure categorical one**: 0 violations on SOBO
and 0 of 20 on `RandomStrategy`. So "never combine Pd(OAc)₂ with DMSO" is expressible exactly where
a screen lives, and must be refused with a clear message elsewhere.

**M-5 · the `FractionalFactorialStrategy` knobs — the direct re-run of the measurement that
refuted the last roadmap, on the domain shape it never tried.** On the **all-categorical** domain
`factorial_design` accepts today, three of the four knobs are inert:

| knob | all-categorical (3 two-level factors) | mixed / continuous |
|---|---|---|
| `n_generators` | **inert** — 8 runs at 0 and 1 | 4 continuous + 1 categorical: 32 runs → **16** at 1 |
| `n_repetitions` | **inert** — 8 runs at 1, 2, 3 | 2 cont. + 1 cat.: 10 → **18** (replicates the factorial part, not the centres) |
| `n_center` | **inert** — 8 runs at 0, 1, 3 | **defaults to 1**; adds `n_center` midpoint rows *per categorical combination* — measured 4/5/6, 8/10/12, 16/20/24 |
| `randomize_runorder` | **works** | works; seed-reproducible and seed-sensitive |

`n_center=0` returns exactly the corner points, at the two bounds. Two continuous factors are too
few to fractionate: `n_generators=1` there raises "Design not possible, as main factors are
confounded with each other".

**M-6 · `strategy.predict()`.** Exists after `tell`, accepts a **params-only** frame, works on a
`CategoricalDescriptorInput` (featurized) domain, returns `<objective>_pred`/`_sd`/`_des`.
Out-of-bounds input is **not clamped** — it extrapolates, and the sd rises about sixfold
(1.60 / 2.60 in range → **16.08** at T=400 against a 20–120 bound).

**M-7 · the diagnostics, and the refusal they reversed.** `shap` is **already installed** via
`bofire[optimization]`, so nothing here costs a dependency. More importantly, the argument against a
cross-validation tool was that reaching it means naming a surrogate class in `engine.py` —
permanently coupling to BoFire's model zoo, and risking a number that describes a different model
than the one that made the recommendation. **That is false.** `strategy.surrogate_specs.surrogates[0]`
exposes the surrogate BoFire itself chose (`MixedSingleTaskGPSurrogate` for a mixed domain,
`SingleTaskGPSurrogate` for a featurized one) and `cross_validate` runs straight off it: 10 rows,
5 folds → **R² 0.948, MAE 1.47**. The number therefore describes *the* model, and no class is named
in our code.

### The waves

Five, each independently shippable, each carrying its own ADR and the measurement that gates it.
`bo-capability-map.md` §5 holds the detail; the ordering rationale is:

1. **W1 `S` — compute what the system already asserts.** A plateau test whose `assay_noise` is a
   **required argument with no default**, and an observed-spread scale for `predicted_sd`. No
   BoFire, no compatibility risk. First, because you cannot judge whether a later wave helped
   without a convergence read.
2. **W2 `S` — admit a continuous factor to a screen.** M-5 turned this from a companion of the
   unused knobs into their **precondition**.
3. **W3 `M` — multi-objective, inline only.** Gated on M-1 and M-2.
4. **W4 `M` — linear constraints, plus the scoped categorical exclusion.** Gated on M-3 and M-4.
5. **W5 `S` — interrogate the fitted surrogate.** `predict_outcome` and CV fit quality. Gated on
   M-6 and M-7.

**Multi-objective stays inline-only.** The durable campaign's objective registry is
`Callable[..., Awaitable[float]]` with two demo entries, so a multi-output registry would be an
abstraction with zero real callers; the launch precondition refuses a multi-objective spec and
points at the inline tool.

**Analytical method development gets no wave, and that is the finding.** It is the largest family by
story count (24 across §7/§8) and it is not blocked on BO: there is no `method` note type in
`KNOWN_NOTE_TYPES` and no retention, resolution, tailing or RSD field anywhere in the schema. A
method-development campaign today has neither factors nor responses to sit on. The prerequisite is a
schema addition; the BO pieces that would then serve it (`TargetObjective`/`CloseToTargetObjective`,
W2's centre points and replication, W4's constraints) are named in the map with that dependency
rather than scheduled.

### What is deliberately not built

Each is mirrored as a `docs/planning/DEFERRED.md` row with its trigger.

| refused | why |
|---|---|
| `DoEStrategy` (D/A/E/G/K/I-optimality) and `SpaceFillingCriterion` | needs cyipopt + SCIP, compiled dependencies in the one rootless image that serves all four process roles; no story asks for an optimality criterion, and fractional + centre points + replication answers 2.3 and 4.4 |
| a real LHS / maximin space-filling design | the same dependency — it is a DoE *criterion*, not a separate strategy. Hand-rolling one would put a second design engine beside the module whose premise is that exactly one module imports BoFire, for a marginal gain over uniform random at n≈5 |
| nonlinear constraints, `Product*` | `BotorchOptimizer` does not support them; it would take pymoo's `GeneticAlgorithmOptimizer` — a new dependency *and* a worse optimizer — to serve no stated use case |
| `NChooseKConstraint` | no story asks for "at most 3 of these 8" |
| `InterpointEqualityConstraint`, `block_feature_key` | both need a plate, day or operator entity; a tree-wide search finds none |
| `EntingStrategy` | defaults to a commercial Gurobi solver |
| the Derringer desirability family, sigmoid objectives, non-unit weights | a scalarisation the chemist cannot audit, where a Pareto front is the honest answer |
| `CategoricalMolecularInput`, `TanimotoGP`, `Fingerprints`/`Fragments`/`MordredDescriptors` | this repo already has a **better-grounded** molecular featurisation — cached xTB descriptors that carry `calc_refs` into the note. A second representation with no provenance link is a competing answer to one question |
| feature importance (`permutation_importance`, `shap_importance`) | *available*, not blocked — measured, no new dependency. Left out because an attribution over four parameters and ten runs is a number this system cannot caveat well enough, and it has no second caller |
| `Qparego`, additive/multiplicative SOBO, `ActiveLearning`, `MultiFidelity`, `ShortestPath`, `StepwiseStrategy`, `LLMStrategy`, `DiscreteInput`, `EngineeredFeatures`, `RobustSingleTaskGP`, `PairwiseGPSurrogate`, `IterativeTrimming` | no caller; listing them is the whole treatment they need |

One asymmetry worth naming: **`DoEStrategy` and `SpaceFillingCriterion` are the only refusals that
rest on a dependency rather than on a use case.** If a deployment ever takes the cyipopt/SCIP cost
for another reason, both become cheap, and the trigger row says so.

## Consequences

- Three roadmap items changed because of a measurement, which is the argument for the register:
  W2 was re-scoped (the knobs are inert without continuous factors), W4 kept its size (no rejection
  sampling needed) and gained the scoped categorical exclusion, and W5's cross-validation moved from
  *refused* to *buildable*.
- The campaign-id denylist is now a known, dated trap with three pinned ids against it. Any wave
  that touches `OptimizationProblem` must reproduce them.
- Two live-run fabrications (`op-13`, `op-16`) have a named wave each, and in both cases the fix is
  a computed number rather than a stronger instruction.
- **No code changed in this ADR's commit.** It records measurements and decisions; each wave carries
  its own ADR and its own tests, which are what stop these findings from rotting. The numbers were
  taken on one machine at one version — a `bofire` bump invalidates them.
