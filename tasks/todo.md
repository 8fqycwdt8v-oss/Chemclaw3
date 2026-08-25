# GFN workflows: one image layer, four composites, a protocol catalogue

The investigation (2026-08-25) asked how this system handles GFN calculations through `crest`,
`tblite` and `xtb`, and what it would take to make as many multi-step workflows as possible
available — ensemble generation, optimization, then a specific value; Boltzmann averaging;
tautomers; pKa; and the agent either *selecting* a protocol or *composing* one. This is the plan.

The full narrative version, with the measured findings behind each claim, is in
`/root/.claude/plans/investigate-deeply-how-chemclaw-sequential-biscuit.md`.

## The finding that reorders everything

**No deployment of this system can run a CREST search today.**
`Chemclaw3-mcp/servers/calc/Containerfile` installs neither `xtb` nor `crest` — both are compiled
Fortran from conda-forge, which `pyproject.toml` cannot express — so the physics runs on the
`tblite` Python bindings, `crest_cli.is_available()` is `False`, and `require_crest()` refuses every
conformer, tautomer, protomer, deprotomer and NCI search. Chemclaw3's own image removed the same two
binaries in that split (`deploy/Containerfile:58-69`) and handed the GPL-3.0 distribution question
to `Chemclaw3-mcp`, **where it has not been taken.** So `sample_conformers`,
`compute_interaction_energy`, `compute_reaction_energy(level="thorough")` and the one shipped QM
template (`data/templates/conformer-refinement.yaml`) are all dead at runtime.

Everything below sits behind that one image layer.

## The concept

Three tiers, each with a home that already exists:

- **Primitives** — `Chemclaw3-mcp` `servers/calc` and `servers/chem`. One calculation, identity
  derivable from its inputs, cached here by key.
- **Composites** — `connectors/calc/compose.py` + `science/calc/*.py`. The fan-out and the
  arithmetic; no key of its own, every part cached.
- **Protocols** — `XtbJobSpec` union members (durable gated tools), `data/templates/*.yaml`
  (`run_<name>`, the "many workflows"), and finally a typed pipeline the model authors.

**The rule that places every piece of work: the loop lives in the job spec, the sequence lives in
the template.** Templates deliberately have no loops and the agent loop is capped at 25 iterations,
so a fan-out (per tautomer, per bond, per microstate) belongs in `compose.py`.

**Why the composites collapse to four.** `compose._species_energy` is already
*embed → (thorough: CREST → lowest) → relax → Hessian → G* for one SMILES. Everything below is that
function plus one of three finishers: a population distribution, a difference across an arrow, or a
Boltzmann average of a property.

---

## Phase 0 — Make CREST run (`Chemclaw3-mcp`)

- [ ] Add a `crest` release-tarball layer to `servers/calc/Containerfile`; `crest_cli` already reads
      `CHEMCLAW_CREST_BINARY`. Everything else (`crest_cli.py`, `crest_search.py`, the four
      `_SEARCH_FLAGS` modes) is written, keyed and tested-for-refusal.
- [ ] Record the GPL-3.0 distribution position in the ADR — `crest` is invoked as a separate process
      over files and never linked, but shipping it *in an image* is the product owner's decision.
      Both repos already say so; this plan asks for the decision rather than assuming it.
- [ ] **Do not add `xtb` in the same step.** It buys the measured 7-9x ANCopt speedup (76 atoms:
      266 s → 38 s) and GFN-FF, but `auto` then moves **every** `calc_version`, so two pods compute
      different keys for one molecule and every existing row is orphaned. Adding `crest` orphans
      nothing, because `CrestSpec.calc_version()` answers `crest-absent` today and no CREST row can
      exist. If `xtb` is taken later, pin `CHEMCLAW_XTB_ENGINE` per deployment rather than leaving
      `auto`.
- [ ] **Measure:** wall-clock and `total_found` for n-butane and one 33-atom molecule at each
      `effort`. This is the cost table everything else quotes.

## Phase A — A molecule is editable before it is computable

Structure manipulation is essentially absent today: RDKit here does *identity*
(`core/chem.py::standardize` — cleanup, fragment parent, uncharge, tautomer **canonicalize**) and
nothing else. No enumeration, no microstates, no stereoisomers, no bond homolysis, no protonation
edit.

`Chemclaw3-mcp`:

- [ ] New `servers/chem/src/chemclaw_mcp_chem/engine/enumerate.py` — `TautomerEnumerator`,
      `EnumerateStereoisomers`, microstate construction, `Chem.FragmentOnBonds` for homolysis,
      `Uncharger` + `LargestFragmentChooser`, `RWMol` H add/remove at an index,
      `rdChemReactions.ReactionFromSmarts` for named degradation transforms.
- [ ] **Re-implement, do not import**, the ~40 lines of `servers/calc/.../engine/pka.py`
      (`_acidic_protons`, `_basic_nitrogens`, `_protonated_forms`) — cross-server imports are not a
      thing that repo does.
- [ ] 7 new `@server.tool()`s in `servers/chem/.../tools.py` (it has 4 today).
- [ ] `servers/chem/connector.yaml` — all 7 in `tools:` **and `read_only:`**. They genuinely are: no
      SCF, no store. So they sit *outside* the plan gate — the agent may enumerate freely and only
      the compute is gated.
- [ ] `servers/calc/.../tools.py` — `compute_fukui_at(structure, solvent)` over
      `engine/xtb_props.py::compute_fukui`, which already takes a `Structure`.
- [ ] `servers/calc/.../engine/identity.py` — key it `xtb.fukui` with **empty params**. Load-bearing:
      `connectors/calc/server/tools.py::predict_site_reactivity` deliberately sends neither `mode`
      nor `top_n`, because the server keys all three modes to one row and `ranked_for` re-ranks
      locally. Keying on the mode would serve the wrong ranking on a hit, silently.

`Chemclaw3`:

- [ ] `src/chemclaw/connectors/chem/connector.yaml` — mirror the 7 names (declaration only; four
      validators read it). Note the manifest exists in **both** repos and first-directory-on-
      `CHEMCLAW_CONNECTORS_DIR` wins, so both must gain the names or the live lane serves the older
      surface.
- [ ] `connectors/calc/server/tools.py::predict_site_reactivity` — add `structure_id: str = ""`
      routed through `_starting_geometry`. **Keep the SMILES route byte-identical** — same two-route
      shape and same argued reason as `compute_electronic_properties` (lines 830-852); re-routing it
      would orphan every `xtb.fukui` row.
- [ ] `tests/calc_server_fake.py` — `"compute_fukui_at": ("xtb.fukui", ())` plus its answer.
- [ ] **Delete** the "Fukui indices at a chosen geometry" row from `docs/planning/DEFERRED.md:23`
      in the commit that closes it (D-154 rule).
- [ ] **Measure:** how often the top-ranked Fukui site changes between a force-field embedding and a
      CREST-chosen conformer, over N molecules. That swap rate is the whole justification for the
      deferral. If it is near zero, say so and ship it as an honesty fix rather than a capability.

## Phase B — The arithmetic and the composites (`Chemclaw3` only)

`science/calc/thermo.py` — four pure additions:

- [ ] `boltzmann_weights(...)` — **extracted from inside `ensemble_from_members`**, where it is
      inline today. Third caller, so the extraction is earned rather than speculative.
- [ ] `populations_from_free_energies(...)` — the same weighting over ΔG. Closes the D-101 gap
      ("does *not* Boltzmann-average free energies over every conformer").
- [ ] `weighted_average(values, populations)` — scalar and per-atom, so one function serves a dipole
      and a Fukui vector. Returns the mean **and the spread**: a property whose ensemble spread
      exceeds the inter-molecular difference it is arguing is not a number to report.
- [ ] `ensemble_spread(ensemble)` — Shannon flatness and top-decile ΔE spread, for polymorph risk.

- [ ] `science/calc/models.py` — `RefinedEnsemble`, `EnsembleProperty`, `SpeciesDistribution`; widen
      `ConformerEnsemble.treatment` with `"free-energy-weighted-top-n"`, keeping today's value as
      the default.
- [ ] New `science/calc/speciation.py` — Henderson-Hasselbalch over microstate free energies,
      `Ka_macro = Σ Ka_micro`, the pH profile. Pure, testable with no engine.
- [ ] New `science/calc/contacts.py` — H-bond geometry and shape descriptors over
      `Structure.arrays()`.
- [ ] New `science/calc/budget.py` — `require_within_budget(units, what)` raising a `ValueError`
      naming the count. `BAD_DATA_RETRY` treats that as non-retryable, so an over-budget request
      fails fast rather than burning the activity budget. Mirrors `xtb_scan_max_points`. **This is
      the single most important new mechanism in the plan.**

`connectors/calc/compose.py` — four new functions, each in the established shape
(`store`, `progress`, `run: RemoteRunner = plain`):

- [ ] `refined_ensemble()` — `conformer_ensemble` → top-N by E → `relax_to_minimum` (already carries
      the saddle-point escape) → `hessian` → `populations_from_free_energies`.
- [ ] `ensemble_property()` — ensemble → `compute_properties_at` / `compute_fukui_at` per member →
      `weighted_average`.
- [ ] `species_ranking()` — `refined_ensemble` per species → distribution. Three simultaneous callers
      at ship time (tautomer ratio, microspecies, stereoisomer ranking) differing in nothing but the
      SMILES set and the label — Rule of Three satisfied, not aspirational.
- [ ] `bond_dissociation_survey()` — `reaction_energy(level="quick")` per cleavage;
      `radical_multiplicity` already makes the open shell work with no extra argument.
- [ ] `_species_energy` gains an `"ensemble"` branch beside its `"thorough"` one (line 674).
- [ ] **Honesty requirement:** refining the top N is a *different approximation*, not a better one.
      Carry `refined_population_covered` (the E-weighted fraction the refined members account for)
      and warn below a threshold. "G-weighted over 5 of 47" must not read as "the ensemble" —
      `ensemble_from_members` already refuses that error for `max_members`.
- [ ] `core/config/calculators.py` — `ensemble_refine_top_n: int = 5`, `species_ranking_max: int = 8`,
      `calc_max_primitive_calls: int = 120`.
- [ ] **Measure:** G-weighted vs E-weighted population difference on 5 real flexible molecules, and
      cold/warm timing for `refined_ensemble` in the established form (`CCO` 0.816 s / 0.007 s). If
      G-weighting moves populations by <5%, that is a finding that reshapes the tier — report it.

## Phase C — The job kinds

`XtbJobSpec` is a closed union discriminated on `kind` and `CalcJobWorkflow` dispatches on it, so
each is **a spec member + a dispatch branch + an optional `| None = None` field on `XtbJobResult` +
a `jobs:` entry**. No new Temporal workflow type, no new activity.

- [ ] `RefinedEnsembleJobSpec`, `EnsemblePropertyJobSpec`, `SpeciesRankingJobSpec`,
      `BdeSurveyJobSpec`; widen `ReactionJobSpec.level` / `SolventScreenJobSpec.level` with
      `"ensemble"`.
- [ ] All `expensive: true`; all `precondition:
      chemclaw.science.calc.solvents:require_supported_solvents` (duck-typed, already covers a
      `solvent` or `solvents` attribute).
- [ ] **`specs.py` must stay a leaf and must inline its Literals.** The chat service imports it on
      every `build_langgraph_agent`; `tests/test_connector_isolation.py` asserts it in a fresh
      interpreter. The existing four members re-declare their Literals rather than importing
      `EnsembleSearch` — four new members are four fresh chances to break that.
- [ ] Add the four tool names to `data/profiles/computation.yaml`.
- [ ] **Deploy the worker before adding the `jobs:` entry** — the manifest is what makes the tool
      reachable, and an old worker cannot decode a new union member.
- [ ] **Measure:** primitive-call count per job kind for a 33-atom reference, tabulated. This becomes
      the cost table the skill quotes.

## Phase D — The protocol catalogue (`data/templates/`)

Each template becomes `run_<name>`; the model picks among them from `summary` + `description`
exactly as it picks among tools. **Every fan-out sits inside one `job` step** — which is why B and C
come first. Shape follows `conformer-refinement.yaml`: `job` → `tool` at a dotted result path →
`agent` write-up.

Ships already (behind phase 0): conformer ensemble · conformer refinement · reaction ΔG + exotherm
flag · solvent screen · relaxed scan / rotational barrier · non-covalent complex · IR vs measured.

- [ ] `fukui-in-conformer` — search → `predict_site_reactivity(structure_id=…)` *(A)*
- [ ] `degradant-hypotheses` — `apply_reaction_smarts` → rank *(A)*
- [ ] `ensemble-free-energy` — `refine_ensemble` *(B)*
- [ ] `descriptor-panel` — Boltzmann-averaged dipole/HOMO/LUMO/gap/charges *(B)*; the highest-value
      consumer is BO featurization (`xtb-use-cases.md` §6.2)
- [ ] `regioselectivity-ensemble` — same job, Fukui inner tool *(A+B)*
- [ ] `tautomer-ratio` — enumerate (chem **or** CREST `--tautomerize`) → `rank_species` *(A+B)*
- [ ] `stereoisomer-ranking` — `enumerate_stereoisomers` → `rank_species` *(A+B)*
- [ ] `microspecies-profile` — `enumerate_microstates` → `rank_species` → `speciation` *(A+B)*
- [ ] `macro-pka` — per-site pKa → `Ka_macro = Σ Ka_micro` *(A+B, +E for site pKa)*
- [ ] `bde-survey` — `enumerate_homolysis` → `bde_survey` → ranked table *(A+B)*
- [ ] `reaction-thermodynamics-ensemble` — `compute_reaction_energy(level="ensemble")` *(B)*
- [ ] `polymorph-risk` — ensemble → `ensemble_spread` *(B)*
- [ ] `intramolecular-hbond` — ensemble → `contacts.py` → population-weighted occupancy *(B)*
- [ ] `shape-and-exposure` — per-member PMI + exposure → weighted *(B)*
- [ ] `ensemble-ir` — refined ensemble (Hessians already paid) → weighted bands *(B)*
- [ ] `impurity-identification` — enumerate candidates → rank → IR/property match *(A+B)*
- [ ] `oxidative-and-hydrolytic-triage` — enumerate → BDE / hydrolysis ΔG → ranked *(A+B)*
- [ ] `templates_enabled` empty means *every discovered template*, so files ship enabled — but a
      profile only advertises what its `tool_names` lists. Name each `run_*` in
      `computation.yaml`. Ten on one profile is a surface a model reads; twenty is one it guesses
      at, so split by profile rather than piling on.
- [ ] **Known gap to state, not fix:** `cli/validate_templates.py` can only resolve signatures for
      tools implemented in this tree, and `chem` is declared here but run elsewhere — so every new
      enumeration tool is name-checked and argument-*un*checked, reported only in
      `unchecked_arguments`. Either keep chem tools behind a `job`/`agent` step, or accept the gap
      and lean on `make connector-validate` against a running server. Publish the new count.

## Phase E — Selection and self-composition

- [ ] **Split rather than grow the skills.** `calculation-selection/SKILL.md` is ~180 lines and
      `make skill-validate` checks both directions. Keep it as the question→calculator table with
      one short "multi-step" section pointing at the templates; add a new global skill
      `ensemble-workflows` holding the ensemble judgment — when an ensemble changes an answer versus
      merely widening it, what a G-weighted population costs against an E-weighted one, how to read
      `refined_population_covered`, and the standing rule that a search is a *sample* so an
      unsampled conformer is not evidence of absence. (`tags` is inert; do not design around it.)
- [ ] Write the catalogued skills each protocol unblocks — `docs/guides/xtb-skill-catalogue.md`
      already specifies ~28 with what gates each, and 19 of 28 were gated on capability that now
      exists.
- [ ] **Species chaining** — a SMILES *list* out of a `chem` enumeration into `rank_species` /
      `bde_survey`. New; one paragraph in `computation.yaml` and one section in
      `ensemble-workflows`. (Geometry chaining via `structure_id` already works and phase A extends
      it to Fukui, the one calculator that visibly did not take one.)
- [ ] **Turn `harness_enabled` on for `computation` only, at `harness_autonomy: plan_only`.**
      `AgentProfile.harness_enabled` is a per-profile override, so this is one YAML line. The
      argument is cost, not capability: `plan_only` puts a chemist in front of the plan *before* the
      first CREST search — the control that matters once one turn can commission six. Not globally,
      and do not raise autonomy.
- [ ] Eval probes per protocol (`expects_tools`) plus `plan_quality` cases for the ad-hoc chains;
      `runaway_rate` must stay at its 0.0 gate.

## Phase F — New engine modes

- [ ] CREST `--entropy` → `compute_conformational_entropy` (keyed `crest.entropy`) — the *sampled*
      absolute S_conf, distinct from `ensemble_from_members`' Gibbs-Shannon term.
- [ ] xtb `--vipea` → `compute_vertical_ionization` (IP, EA, ω, hardness). `xtb_cli.CliTask` is
      `sp|opt|hess|ohess` today, so this is a new task type. **Measure** Spearman ρ, MAE and worst
      error against an experimental IP set, following the pKa suite's precedent, and set the
      reported uncertainty from the *worst* error.
- [ ] **Redox potential** — `--vipea` (vertical) plus `reaction_energy` on charged species
      (adiabatic). Needs no server change for the adiabatic half: embed the neutral, relax it, then
      build the ion as a new `Structure` from the relaxed coordinates with `charge ± 1` and
      `multiplicity 2` — a Born-Haber construction, and `Structure`'s validator checks the electron
      count so a wrong pairing is a loud error.
- [ ] xtb `--bhess` + Eyring → **rotational barrier as a free energy**, which turns
      `atropisomer-assessment` from a sketch into its regulatory number.
- [ ] `predict_pka_at_site(smiles, atom_index)` — closes `macro-pka` and `logd.py`'s
      polyprotic/amphoteric refusal.

## Phase G — QCG, MSREACT, and the typed pipeline

- [ ] **QCG microsolvation** — the highest-value later item: D-104 records that aliphatic amines
      fail at Spearman −0.17 precisely because a continuum solvent cannot represent the ammonium
      ion's hydrogen bonding to water, and most pharma APIs are basic amines.
- [ ] **MSREACT** in-silico MS/MS — `impurity-structure-hypotheses`.
- [ ] **`PipelineJobSpec`** — a sixth and final `XtbJobSpec` member, pydantic-only: steps over a
      **closed vocabulary** of Literals; references as **typed objects**
      (`{from_step: "search", field: "lowest_structure_id"}`) rather than a string mini-language;
      a `@model_validator(mode="after")` enforcing backwards-only references and no cycles at run
      time, because nothing can validate a model-authored payload in CI; exactly **one** `for_each`
      step kind, one level deep, bounded; `max_steps`, `max_fanout`, and the same
      `require_within_budget` preflight. This is the escape hatch `specs.py` already describes — *a
      model-authored payload can select among calculations we defined, and can never describe one we
      did not*. It comes last so its vocabulary is the one the templates proved.
- [ ] **Measure:** fraction of model-authored pipelines that validate on first attempt.

---

## Risks that will actually bite

- **Cost explosion — first and worst.** `species_ranking` over 6 tautomers × `refined_ensemble(top
  5)` is 6 CREST searches + 30 relaxations + 30 Hessians ≈ 72 primitive calls. Against the measured
  anchors (~50 s CREST for 14-atom n-butane; 5.7 s opt+Hessian for 33-atom ibuprofen) a drug-sized
  molecule blows past `xtb_job_timeout_seconds = 14400`. Fences in order: the
  `require_within_budget` **preflight**, `ensemble_refine_top_n`, `species_ranking_max`. Leave
  `calc_screen_max_parallel` at 1 — its own comment explains that the server sizes itself to its
  machine. **Do not raise `xtb_job_timeout_seconds`**: one number shared by every calc job, and
  raising it to fit the worst degrades failure detection for the 2-second one. Bound the work, not
  the clock — and do not add a per-job timeout setting either, a new manifest axis serving one job.
- **Wire compatibility.** New `XtbJobResult` fields must be additive and defaulted; histories are in
  flight (`calc_refs` is the precedent).
- **Do not bump `CALCULATION_EPOCH`.** Every new capability is a new `calc_type` or a new `params`
  value, writing new rows that cannot collide. A defensive bump would discard every cached CREST
  search — the most expensive thing in the system.
- **Never pin a sampled count in a test.** D-101 records the failure exactly: `total_found == 2` for
  n-butane passed twice and returned 4 on the third run. Assert call counts and arguments against
  `FakeCalcServer`, invariants (Σp = 1, populations monotone in energy, degeneracy-weighted ≠
  unweighted), and cache behaviour. Assert real physics only in `tests/test_calc_thermo.py`, over
  Hessians **recorded** from the live server.

## What not to build

A unified `state_change()` over pKa/redox/BDE/tautomerisation — traced, the four share only argument
marshalling: BDE and tautomerisation are already `reaction_energy`, redox adds one constant, and pKa
needs a *fitted LFER* that lives on the server and whose ±1.6/±1.0 accuracy is the only measured
number in this area. Building it would mean a second, competing pKa predictor here. One new
`ReactionLevel` value plus four thin protocol layers instead.

Also not: any Chemclaw3-side pKa or redox calibration · logD/logP "over an ensemble" (Crippen LogP is
a 2D atom-contribution sum, conformer-independent by construction; the real gap `logd.py` names is
*microstates*, covered by `microspecies-profile` / `macro-pka`) · a DFT/CENSO rung · a
transition-state search (the real remaining gap — keep saying a scan maximum is not a barrier) · a
new Temporal workflow type · conditionals or loops in templates · **a cached composite**, whose key
would name an output.

## ADRs

| Slug | Decides |
|---|---|
| `the-binary-is-the-capability` | Shipping `crest` in the `calc` image, the GPL-3.0 position, and why `xtb` is a separate decision that re-keys every row |
| `a-molecule-is-editable-before-it-is-computable` | Enumeration goes on `chem`, not `calc`: not a calculation, no `calc_version`, no cache row — and `read_only`, so outside the plan gate. Closes the Fukui-at deferral |
| `the-fan-out-is-a-composite-not-a-template` | The loop lives in `compose.py`; the budget preflight is part of the decision, not a follow-up |
| `a-free-energy-weighted-ensemble-is-a-different-treatment-not-a-better-one` | Closes the D-101 gap; `treatment` says which ran; `refined_population_covered` stops "5 of 47" reading as "the ensemble" |
| `pka-stays-on-the-server` | The refusal, and why `state_change()` was not built |
| `the-plan-gate-is-the-cost-control` | `harness_enabled` on for `computation` at `plan_only`, and why not globally |
| `a-model-may-compose-a-pipeline-it-cannot-describe` | The typed DAG: closed vocabulary, typed references, one bounded `for_each`, run-time validation |

## Verification

`sudo -n dockerd &` ; `make up` ; `make db-migrate` — Postgres and Temporal do run here, and without
them ~157 Postgres tests skip **silently**, which for phase B (where `StructureStore` writes are
load-bearing in `kept()`) means a green suite that proved nothing. Never report a local run as green
without saying what it skipped.

Per phase: `make lint type test`, then `connector-validate` / `template-validate` / `skill-validate` /
`prose-validate` as the phase touches them, then the four-repo lane
(`infra/live/e2e-full-stack/up.sh`) for at least `macro-pka` and `tautomer-ratio`, the two protocols
that cross every seam. Each phase ships the measured number named in its section — the house style is
a number, not a claim.

## Review

*(to be filled in as phases land)*
