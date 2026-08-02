# §6 — Reaction Chemistry (catalysis · solid state · impurities · workup)

**Section summary:** The half of §6 that is *retrieval over recorded chemistry* is genuinely served
and in places served well — `gather_evidence` + `similar_reactions` + `expand_note` over a graph
whose reaction schema already carries impurities, purity, outcome class and failure reason, plus a
`failure-mode` note type and distilled `playbook` notes, answer the coupling-troubleshooting and
catalyst-shortlist stories end to end. The half that needs a *predictive model of matter in a
vessel* — crystallization, solid form, purification, phase behaviour — is absent down to the
concept: `predict_solubility` is aqueous-only, and `KNOWN_NOTE_TYPES` (`src/chemclaw/kg/note.py:118`)
has no form, no method, no impurity and no risk-assessment citizen. The single biggest thing standing
between the system and the rest of §6 is not a model but **rows and columns**: the genotoxicity
stories need SMARTS rows in a rule table whose engine already exists, and every cross-project
comparison story (6.3, 6.18) fails on a comparison artifact that carries only Temp/Time/Yield.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 6.1 | technician: stalling/low-conversion coupling → likely causes from past cases | `FULL` | `gather_evidence` (`src/chemclaw/agent/research_tools.py:98`), `similar_reactions` (`src/chemclaw/connectors/rxnfp/server/tools.py:20`), `find_notes`/`expand_note` (`src/chemclaw/agent/graph_tools.py:73,122`) over `failure-mode` notes (`src/chemclaw/kg/note.py:132`; `knowledge/failure-mode/failure-aqueous-protodeboronation.md`) and `playbook-pd-cross-coupling-scope`. `OrdReaction.failure_reason` (`src/chemclaw/ingest/eln/ord.py:164`) makes a negative result citable. | — |
| 6.2 | technician: shortlist catalyst/ligand combos for a substrate class | `FULL` | `similar_reactions` (DRFP) over the ~10k indexed HTE reactions + `similar_molecules`/`substructure_matches` (`src/chemclaw/connectors/molfp/server/tools.py:32,44`) + `expand_note` for the conditions. No ligand-property or ligand-ranking model — but the story asks what *worked*, which is retrieval. | — |
| 6.3 | lab leader: compare conversion/selectivity/impurities across candidate routes to one intermediate | `PARTIAL` | Per-reaction data is retrievable (`purity_percent`/`impurities`, `ord.py:159,160`, rendered by `_impurity_block`, `src/chemclaw/ingest/eln/note.py:96`). Missing: **no `conversion` and no selectivity field anywhere** in `OrdReaction`; no `route` entity grouping alternative routes to one target; and the only comparison artifact — the `optimization-campaign` table (`src/chemclaw/memory/optimization.py:102`) — has columns Run/Performed/Temp/Time/Yield/Changed and groups the *same* transformation, not competing routes. | M |
| 6.4 | lab leader: flag chemo-/regioselectivity risk from the functional groups present | `PARTIAL` | `predict_site_reactivity` (`src/chemclaw/connectors/calc/server/tools.py:628`) + `product-prediction` / `reactivity-descriptors` skills rank sites *within* one molecule; `substructure_matches` finds the groups. Missing: no functional-group / reagent-compatibility table and no reagent-aware selectivity model — the Fukui ranking sees no reagent, no sterics, no solvent, one MMFF conformer. | M |
| 6.5 | lab leader: starting T / stoichiometry / addition-rate ranges for a *new* reaction type, from literature + analogues | `PARTIAL` (literature half `OUT-OF-SCOPE`) | Literature is refused by decision — D-089, enforced by `tests/test_no_egress.py`. Internal half: `similar_molecules`/`similar_reactions` reach analogues and `temperature_c`/`time_h` are recorded fields. Missing: **equivalents never reach a note** (`_conditions_block`, `note.py:73`, renders T/time/yield/purity/date only) and are deliberately excluded from run-to-run diffs (`changes_between`, `src/chemclaw/memory/progression.py:120`); **no addition-rate field exists** — `ReactionStep` carries `temperature_c`/`duration_h` only. | S |
| 6.6 | technician: why did impurity formation change from small to larger lab scale | `PARTIAL` | Impurity records + `failure-mode` notes support naming chemical hypotheses; the system prompt's gap list (`src/chemclaw/agent/chemclaw_agent.py:159`) makes the honest refusal available. Missing: **scale is not a queryable fact** — `Component.amount_mmol`/`mass_mg` exist but never render into the note body — and there is no mixing, heat/mass-transfer, residence-time or addition-rate model to attribute a difference to. | S (scale field) / L (model) |
| 6.7 | technician: unexpected crystallization outcome (oiling out, wrong form, poor filtration) → causes and fixes | `MISSING-MODEL` | Only `StepKind.PURIFICATION` (`ord.py:68`) labelling past crystallizations as prose. Absent: an organic/mixed-solvent solubility model (`predict_solubility`, `calc/server/tools.py:544`, is aqueous-only), metastable-zone-width / supersaturation, a nucleation or oiling-out criterion, a cooling-profile model, and any particle-size or cake-resistance quantity. | L |
| 6.8 | lab leader: compile the polymorph landscape from similar past compounds | `MISSING-ENTITY` | `similar_molecules` would gather the neighbours; there is nothing to compile. No `solid-form`/`polymorph` note type in `KNOWN_NOTE_TYPES` (`kg/note.py:118`), no XRPD/DSC/TGA store, no form/solvate/stability fields. A **data + schema** problem, not a research one. | M |
| 6.9 | lab leader: design a solvent/anti-solvent or cooling crystallization screen | `PARTIAL` | `generate_screening_design` (`src/chemclaw/connectors/bo/server/tools.py:141`) produces a real full-factorial matrix over categorical factors — solvent × anti-solvent is exactly that shape — and `screen_hazards` vets the candidates. Missing: the chemistry that *chooses* the levels (organic-solvent solubility, miscibility, a solvent-property table in `data/`), and the tool refuses continuous parameters, so a cooling profile must be discretized by hand. | M |
| 6.10 | lab leader: flag stability/manufacturability risk in a proposed final form | `MISSING-ENTITY` | Nearest machinery: `compute_interaction_energy` (`src/chemclaw/connectors/calc/connector.yaml:159`) names salt/co-crystal association and is ranking-only in continuum solvent; `predict_pka` supports a ΔpKa argument but refuses aliphatic amines and the `ionization-and-partitioning` skill forbids deciding a salt on it. Missing: no salt/solvate/form record, no stability or shelf-life data, no manufacturability concept. | M |
| 6.11 | technician: propose mechanisms for a new process-related impurity | `PARTIAL` | `degradation-liabilities` skill orchestrates `predict_site_reactivity`, `compute_electronic_properties`, `predict_pka`, and `compute_reaction_energy` for whether a proposed pathway is downhill; `compute_thermochemistry` (`calc/server/tools.py:696`) gives a computed IR that `computed-spectra-comparison` uses to discriminate isomers. Missing: no mechanism/retro engine, **no transition states or barriers anywhere** (`scan_coordinate` is explicitly not a reaction path), no MS or NMR prediction to test the hypothesis. | L |
| 6.12 | lab leader: flag ICH M7-type structural alerts for mutagenic/genotoxic impurities | `MISSING-DATA` | The matcher is generic and fully reachable: `screen_hazards` (`src/chemclaw/connectors/safety/server/tools.py:23`) over a YAML SMARTS table with severity/explanation/citation/`min_matches` (`src/chemclaw/science/safety/screen.py`), whose header states a chemist edits it "without touching Python". What is absent is **rows**: `rules.yaml` holds 16 process-safety motifs and zero mutagenicity content. See Notes for where this turns into `MISSING-MODEL`. | S–M |
| 6.13 | lab leader: flag nitrosamine / other high-concern impurity risk in reagents, conditions, sequences | `MISSING-DATA` | Same engine, and the *pair* form fits exactly: `incompatible_pairs` fires when one component matches `left` and a different one matches `right` — a nitrosatable amine plus a nitrosating agent is that rule. Absent: the rows; plus two real code gaps — the pair check is **within one reaction, never across route steps** ("two steps later" is unreachable), and there is no purge-factor model or acceptable-intake table. | S (rows) / M (route-level) / L (purge) |
| 6.14 | lab leader: trace which route step is responsible for a downstream impurity | `PARTIAL` | `src/chemclaw/memory/chains.py` links product→reactant into a route and `campaign` notes cite each step; per-step `impurities` are recorded and rendered. Missing: `Impurity` (`ord.py:108`) has optional name/SMILES/area% and **no link to a compound note**, so the same impurity at two steps is two unrelated strings; no carry-over/fate model, no purge factor, and impurities are deliberately excluded from every fingerprint. | M |
| 6.15 | manager: which projects have open, unresolved impurity/genotoxicity risk assessments | `MISSING-ENTITY` | Nothing. `project` is a free-text string on `OrdReaction` (`ord.py:181`) used as a grouping tag; `find_knowledge_gaps` reports `projects_without_distillation` (`src/chemclaw/kg/analytics.py`), which is a synthesis backlog, not a risk register. No `risk-assessment` note type, no status/owner/due-date field on any note, and no genotoxicity content to be open about. | L |
| 6.16 | technician: workup problem (emulsion, phase split, loss to aqueous) → causes and fixes | `PARTIAL` | Past workups are retrievable (`StepKind.WORKUP`, `ord.py:67`; `knowledge/failure-mode/failure-dcm-amide-coupling.md`), and `predict_logd` (`calc/server/tools.py:774`) is the genuinely right tool for "loss to the aqueous layer" — pH-dependent partition with its site named. Missing: `predict_logd`'s domain is one ionisable site (aliphatic amines and amphoterics refused), the skill forbids *setting* a wash pH from it, and there is no emulsion, interfacial or solvent-pair phase-behaviour model or table. | M |
| 6.17 | technician: solvent systems / purification approach for a compound class | `PARTIAL` | Retrieval only: `similar_molecules` + past `PURIFICATION` steps as prose. Missing: no organic-solvent solubility model, no crystallinity or volatility predictor, no chromatography model/method store/gradient recommender, and **no solvent-property table anywhere in `data/`** (miscibility, bp, ICH Q3C class, greenness) — `core/reagents.py` is a name→SMILES identity table only. | S (solvent table) / L (models) |
| 6.18 | lab leader: compare purification approaches across similar intermediates and projects | `PARTIAL` | All the ingredients exist — `similar_molecules`, `StepKind.PURIFICATION`, `yield_percent`+`purity_percent`, `project`, and a distillation layer that requires ≥2 projects (`src/chemclaw/memory/playbook.py`); `playbook-recrystallisation-purity` is exactly this class of lesson. Missing: purification is a **label on free text, not a field**, so nothing can group by it, and there is no aggregation primitive — `gather_evidence` returns capped chunks, never a count or a group-by. | S–M |

### Notes

**6.12 / 6.13 — why this is `MISSING-DATA` and where it stops being so.** The engine side is complete
and needs zero code: `science/safety/screen.py` compiles SMARTS from `rules.yaml`, supports counted
matches (`min_matches`) and cross-component pair rules, carries severity + explanation + citation per
rule, is exposed as `screen_hazards`, is sequenced by the `run_hazard_briefing` template
(`data/templates/hazard-briefing.yaml`), and a flag at or above the gate severity forces a `## Hazards`
section through `kg-validate`. A published alert set (Ashby–Tennant / Kazius / Benigni–Bossa classes:
N-nitroso, aromatic nitro, aromatic amine, alkyl halide, epoxide, aziridine, Michael acceptor, sulfonate
ester, hydrazine, azoxy, α,β-unsaturated aldehyde) is a list of SMARTS with citations — procurement and
curation, days to a few weeks, `S–M`. Nitrosamine risk is even better matched to the existing shape,
because the primary rule is a *pair* (nitrosatable secondary/tertiary amine × nitrite / nitrous acid /
NOx / a nitrosating reagent), and `incompatible_pairs` is that check.

Three things inside these stories are **not** data and must not be sold as such:

- **An ICH M7 class (1–5) is `MISSING-MODEL`.** M7 asks for two complementary QSAR systems — an
  expert rule-based one and a statistical one — plus bacterial-mutagenicity data and expert review.
  An alert list flags; it does not classify. The moment the question is "give me the class", the cost
  jumps from a data file to a licensed or built model plus an Ames corpus (`L`).
- **Purge factors and acceptable-intake limits are `MISSING-MODEL` + regulatory data** (`L`).
- **Route-level screening is a small but real code gap** (`M`): the pair rule walks one reaction's
  components. rx-18's "we quench with sodium nitrite two steps later" is structurally unreachable
  today, and would need a route object to screen over.

One code change belongs with the rows: a **second table, not more rows in the same one**. A genotox
alert and an energetic-hazard flag must not share `HazardFlag.severity`'s three-value vocabulary or
the same "no rule matched" verdict string, or the screen's own warning — that an over-trusted screen
is worse than none — is undone by the fix. Cheapest honest improvement *right now*, before any rows:
nothing to build. `connectors/safety/skills/safety-screening/SKILL.md:74` already names all four
absent classes explicitly and the system prompt repeats them; the refusal is correct in the source
and the live run's failures (rx-17, rx-18 both graded `fabricated`) are the model routing around a
correct refusal, not a missing capability.

**6.7–6.10 — corpus versus model, kept apart.** The polymorph *landscape* (6.8) and the final-form
risk (6.10) are data and schema: a `solid-form` note type with form designation, solvent/conditions,
characterisation refs and a relation to its compound, plus the studies to fill it. That is `M`, and
`similar_molecules` already supplies the "structurally similar compounds" half of 6.8 for free. The
crystallization *outcome* (6.7) and the *screen design that picks its solvents* (6.9) are not: they
need an organic and mixed-solvent solubility model where the system has an aqueous-only one, plus
supersaturation/MSZW and nucleation behaviour. Building a form corpus does not move 6.7 one step, and
this is exactly where the live run failed — rx-10 and rx-12 both offered organic-solvent solubility
that `predict_solubility` cannot produce.

**6.3 / 6.18 — the comparison gap is a column, not a retriever.** Retrieval can reach several routes:
`compound_smiles` on a reaction note is its principal product (`ingest/eln/note.py:41`), so
`find_notes` on a SMILES does return every recorded reaction making that intermediate, and `expand_note`
gives each one's conditions, purity and impurity block. What fails is *comparison*: the one artifact
built for side-by-side reading, the `optimization-campaign` table, carries Temp/Time/Yield and nothing
about purity, impurities or selectivity, and its grouping key is DRFP similarity — the same
transformation, which is the opposite of "candidate routes". The cheapest honest improvement in this
whole section is **three more columns in `memory/optimization.py:102`** (purity, major impurity, its
area%) — the data is already on every `OrdReaction` and already rendered per note. `S`.

**6.11 / 6.14 — the impurity thread.** Give `Impurity` a resolved compound link and the trace in 6.14
becomes a graph query instead of a prose-reading exercise: the same structure appearing at step 3 and
step 6 becomes one graph citizen with two edges. That is the highest-leverage schema change in §6
after the alert rows.

**6.5 — what the internal half can actually do.** With literature refused, the answerable question is
narrower and still useful: "the nearest transformations we have on record and what they were run at".
`similar_reactions` + `similar_molecules` reach them; `temperature_c`/`time_h` are fields. The story's
other two variables are not reachable at all — equivalents exist on `Component` and never render into
a note, and addition rate has no representation anywhere. Surfacing equivalents into `_conditions_block`
is `S` and would convert one third of this story from prose-mining to retrieval.

---

# §11 — Reaction & Process Scale-Up

**Section summary:** §11 is the section the system serves least, and it is honest about it: the
system prompt's own gap list (`src/chemclaw/agent/chemclaw_agent.py:154`) names the absence of
calorimetry, heat/mass-transfer, mixing, addition-rate, CPP, PAR, design-space, tech-transfer and
master-batch-record concepts before any story is asked. Nothing here is a `MISSING-TOOL` — no
scale-up computation is sitting unexposed in `science/`. What exists is the *chemistry* half:
`similar_reactions` finds the analogous transformation, `campaign` and `failure-mode` notes record
what went wrong last time, and `watch_for` is a real standing-query mechanism. The single biggest
thing standing between the system and the rest of §11 is that **scale itself is not a fact the
system holds**: `Component.amount_mmol`/`mass_mg` exist on every record and never reach a note body,
and `changes_between` deliberately excludes amounts — so the system cannot tell a 5 g run from a 2 kg
run except by reading verbatim procedure prose.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 11.1 | technician: what typically changes from flask to pilot scale | `MISSING-MODEL` | Nothing in the tree serves this; the only real machinery is the refusal-plus-labelled-background rule in the system prompt (`chemclaw_agent.py:159`: a computed enthalpy "is never a process heat load, an adiabatic rise, a jacket duty or a safe addition rate"). Absent: no vessel/equipment entity, no heat- or mass-transfer correlation, no mixing/blend-time model, no dosing model, no scale field. | L |
| 11.2 | lab leader: compare a proposed scale-up against similar past campaigns | `PARTIAL` | "Similar" is genuinely served — `similar_reactions`, `campaign`/`optimization-campaign` notes, `find_past_jobs` (`src/chemclaw/agent/durable_tools.py:224`) — and the seeded corpus proves it (rx-26 graded `served`, every specific corpus-verified). Missing: no batch/campaign **scale** attribute; amounts never render into the note body; `changes_between` (`memory/progression.py:120`) excludes amounts by design; no vessel or equipment concept. | S |
| 11.3 | lab leader: identify critical process parameters and their scale-dependent risks | `MISSING-ENTITY` | No CPP, proven-acceptable-range or design-space concept exists in any schema, and the system prompt says so explicitly. BoFire fits a surrogate (`suggest_next_experiment`, `connectors/bo/server/tools.py:56`) but **exposes no parameter-importance or sensitivity output** — grepping `science/bo/` for importance/sensitivity returns nothing. Scale dependence additionally needs the models 11.1 lacks. | L |
| 11.4 | lab leader: pull together process understanding (kinetics, safety, impurity formation) to size and select equipment | `MISSING-MODEL` | Three of four parts absent: **no kinetics of any kind** (no rate, no transition state, no barrier; `scan_coordinate` is documented as "not a reaction path"), no calorimetry (DSC/ARC) — `screen_hazards` is structural alerts only — and no equipment entity to size. Only impurity *records* exist. See Notes for exactly where the thermochemistry stops. | L |
| 11.5 | manager: early warning when a plan resembles past campaigns that ran into delays/failures | `PARTIAL` | Real halves: `watch_for`/`list_watches`/`stop_watching` (`src/chemclaw/agent/subscriptions.py:154`) is a durable standing query with a watermark, run by the digest job (`src/chemclaw/durable/digest.py`); `failure-mode` notes and `outcome_class=FAILURE` record the failures; `similar_reactions` supplies "resembles". Missing: the trigger fires when a *note lands*, not when a *user proposes a plan* — there is no plan entity to match against and no proactive risk matcher — and there is **no delay, schedule, timeline or project entity at all**. | M (matcher) / L (delays) |

### Notes

**11.1–11.4 — where the physics actually stops.** `compute_reaction_energy` (a durable job,
`connectors/calc/connector.yaml:85`) and `compute_thermochemistry` (`connectors/calc/server/tools.py:696`)
do return a real reaction enthalpy, and it is worth being exact about what that number is and is not,
because it is the near-miss the whole section turns on:

- It is a **standard-state, per-mole, single-conformer, GFN2-xTB** ΔH (or ΔG) in gas phase or an
  implicit continuum, carrying a few kcal/mol of uncertainty that the tool reports and the
  `reaction-thermodynamics` skill insists is quoted.
- It is **not a process heat load** — that needs batch moles and the time over which they react, and
  neither is a fact the system holds (no scale field, no rate).
- It is **not an adiabatic temperature rise** — that needs batch mass and a heat capacity. There is no
  Cp anywhere in `science/calc/`, and no batch mass.
- It is **not a jacket duty or a cooling check** — that needs U, A and ΔT, i.e. a vessel. There is no
  equipment entity.
- It is **not a safe addition rate** — that is a kinetics-plus-heat-removal question and the system has
  neither term.

The live run confirms this is the failure mode that actually occurs: rx-29 was graded `fabricated`
for offering to convert a computed reaction energy into a heat load, an adiabatic rise and a jacket
assessment with a "typical U × A", and rx-30 for treating three absent capabilities (mixing, heat
transfer, cycle time) as a missing-data problem it could go and solve. Nothing was miscomputed —
the machinery was described as reaching further than it does.

**11.2 — the cheapest change in the section, by a distance.** Render the charged amounts into the
reaction note's conditions block and derive a `scale_g` from the limiting reagent. Everything else
needed for "have we run this at scale before" already exists: the transformation search, the campaign
notes, the failure modes, the impurity records. Today the answer depends on whether a chemist happened
to write the mass into `steps[].text`. Note the one thing this does *not* fix: `changes_between`
excludes amounts on purpose (a run recording a mass next to one that does not would report a spurious
change every time), so a scale column on the campaign table needs its own treatment rather than
falling out of the diff.

**11.5 — a composition, not a model.** "Screen this proposal against what has gone wrong before" is
buildable from parts that all exist and are all reachable: `similar_reactions` on the proposed
transformation, `gather_evidence(note_type="failure-mode")`, and the recorded `contradicts` edges that
`kg/conflicts.py` already reads (`failure-aqueous-protodeboronation` contradicts `bo-suzuki-next` —
the corpus already holds a machine-readable "the model wants to go hotter and that is the known
failure"). That is `M` and it is the one §11 story where the gap is wiring rather than physics.
Everything about *delays* — schedules, campaign duration, staffing — is `L` and needs a project
entity that does not exist.

**Deployment caveat, not a capability gap.** `compute_reaction_energy`, `compare_solvents`,
`scan_coordinate`, `sample_conformers`, `compute_interaction_energy`, `compute_dft_energy`,
`start_optimization_campaign` and `request_development_report` are declared, wired and Temporal-hosted;
they were unreachable during the live run because the broker was not up (rx-20, rx-32). Every verdict
above judges the code, not that outage.
