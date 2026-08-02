# Story audit — §2 Experiment & Study Planning, §3 Bayesian Optimization, §4 HTE Campaigns

Method and verdict vocabulary: `STORY_AUDIT_SPEC.md`. Every verdict below is defensible from the
source; live-run probe ids (`op-NN`, from `tasks/live-test/regrade-merged.json`) appear only as
corroboration. Paths are relative to `/home/user/Chemclaw3`.

---

# §2 — Experiment & Study Planning

**Section summary:** The *chemistry* half of planning is genuinely served — the agent can gather
cited precedent from the graph and the fingerprint index, expand a past run to its verbatim
procedure, resolve reagent names to structures, compute a charge table, and screen the proposed
mixture for structural hazards, with `deep-research` and `experiment-progression` holding the
judgment. The *operational* half does not exist at any level: there is no instrument, booking,
inventory or lot concept anywhere in `src/`, in the note schema, or in the data-source registry, so
"can I run this on Thursday, do we have the ligand" is unanswerable by construction rather than by
omission. The single biggest thing standing between this section and the rest is that a "plan" is
not a first-class object: there is no protocol/method note type carrying an ordered step sequence
with per-step conditions, so a drafted plan is model prose citing notes rather than a structured
artifact the system can validate, cost, or check against a constraint.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 2.1 | technician: draft an experimental plan (reagents, conditions, sequence) from similar past work | `PARTIAL` | Serves: `gather_evidence` (`agent/research_tools.py:98`) over `GraphRetriever` + the `reaction_smiles` fingerprint leg (`:153`), `similar_reactions`, `expand_note`, `resolve_compound` + `stoichiometry_table` (`connectors/chem/server/tools.py:89`), `screen_hazards`, `green_metrics`; judgment in `skills/deep-research/SKILL.md`; recorded via `propose_knowledge_note` type `experiment-proposal`. Missing: no `protocol`/`method` note type — sequence exists only as free prose inside a `reaction` note; and the charge table cannot express a solvent charge (see 4.2) | `M` |
| 2.2 | technician: flag conflicting equipment bookings, reagent availability, or safety requirements | `PARTIAL` | Safety only: `screen_hazards` → `science/safety/screen.py:211`, the `run_hazard_briefing` template (`data/templates/hazard-briefing.yaml`), `connectors/safety/skills/safety-screening/SKILL.md`. Missing entirely: no `instrument`/`booking` entity and no calendar data source; no `inventory`/`lot`/`stock` record — `core/reagents.py` is a name→SMILES identity table with no quantity, location or expiry. Neither appears in `core/config.py` or `KNOWN_NOTE_TYPES` (`kg/note.py:118`) | `L` |
| 2.3 | lab leader: propose a screening or DoE plan grounded in historical data | `PARTIAL` | Serves: `generate_screening_design` (`connectors/bo/server/tools.py:141`) + `suggest_next_experiment`, grounded by `gather_evidence` / `optimization-campaign` notes; `connectors/bo/skills/experiment-design/SKILL.md` §"The other DoE question". Missing: the only design producible is the complete Cartesian product — `factorial_design` (`science/bo/engine.py:193`) uses `FractionalFactorialStrategy` at default `n_generators=0`, and continuous factors are rejected outright (`:205`). No fractional/reduced design, no blocking, replication or randomized run order | `S` |
| 2.4 | lab leader: what has not been tried yet for a route or separation problem | `PARTIAL` | Serves (synthesis): `find_knowledge_gaps` (`agent/graph_tools.py:169`) + `kg/analytics.py`; the "changed vs previous" column of `optimization_campaign_note` (`memory/optimization.py:142`) and `skills/experiment-progression/SKILL.md` §2 "Untouched", which instructs exactly this scan. Missing: `find_knowledge_gaps` reports *graph* health (type counts, isolated notes, undistilled tags), not chemistry coverage — nothing enumerates a factor space and subtracts the observed runs. The separation half has no model, no `method` note type and no column/retention store at all | `M` (synthesis) / `L` (separation) |

### Notes

**2.1 — what "plan" means here.** The reagent and condition halves are real and traceable: `op-01`
reconstructed an in-house Suzuki record with every number citable, and `op-07` produced a
five-condition diagnostic set from `failure-aqueous-protodeboronation` and `opt-suzuki-conditions`.
What is absent is structure, not content. `KNOWN_NOTE_TYPES` (`kg/note.py:118`) has eleven types and
none of them is a procedure: `experiment-proposal` is a free Markdown body. That means nothing can
validate a plan (are the equivalents consistent? is every named reagent resolvable? was the mixture
screened?) the way `kg-validate` validates a note's links. A `protocol` note type with an ordered
step list is the enabling change for 2.1, and it is also what 2.2's inventory check and 4.2's volume
check would hang off.

**2.2 — the safety half is narrower than the story.** "Safety requirements" in a planning context
means COSHH/SDS, exposure limits, thermal stability at scale, and containment. `screen_hazards` is
16 SMARTS structural alerts plus **5** pairwise incompatibilities (`science/safety/rules.yaml`:
oxidizer-with-reductant, azide-with-DCM, hydride-with-dipolar-aprotic, peroxide-with-ketone,
complex-hydride-with-chlorinated-solvent). `ScreenResult.verdict` (`science/safety/screen.py:72`) is
carefully worded to refuse the overclaim, and it is a `computed_field` specifically so the
disclaimer reaches the model — that is the system being honest about a narrow capability, and it
should be read that way rather than as coverage. `op-04`, `op-05` and `op-06` all graded `served`
because the agent refused the booking, stock and EHS-coordination asks outright.

**2.3 — the cheapest capability gain in this whole audit.** `FractionalFactorialStrategy` is already
imported and instantiated at `science/bo/engine.py:211`; BoFire's `n_generators`, `block_feature`
and `n_repetitions` parameters are simply never passed, and `ScreeningDesign` has no field to carry
the resolution back. Exposing them turns "here are your 96 wells" into "here is a resolution-IV
design in 16" — the actual DoE question a lab leader asks. See 4.4, which is the same gap seen from
the HTE side.

---

# §3 — Bayesian Optimization & Data-Driven Experiment Design

**Section summary:** The single-objective ask step is the strongest capability in the audit:
`suggest_next_experiment` is inline, sub-second, needs no Temporal, featurizes molecular
categoricals through cached xTB descriptors so the surrogate can speak about an untried ligand, and
returns the surrogate's own posterior mean and sd alongside each candidate. Everything around that
one call is thinner than the stories assume. `OptimizationProblem` (`science/bo/problem.py:121`) has
exactly two fields — `parameters` and a **singular** `objective` — so multi-objective and constraints
are not partially supported, they are unrepresentable; the campaign record is **write-only** in
production, so the loop the stories are built on cannot cross a session; and nothing computes
convergence, so "has this plateaued" has no answer to give. The single biggest thing standing
between this section and the rest is the missing *read* path on the campaign store: it is a few
hours of work, everything downstream of it already exists, and without it the `campaign_id` the tool
tells chemists to quote back resolves to nothing.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 3.1 | technician: which few experiments to run next, given results so far | `FULL` | `suggest_next_experiment` (`connectors/bo/server/tools.py:56`, in `connector.yaml` `tools:`) → `propose_candidates` → BoFire SOBO (`science/bo/engine.py:172`); `count` gives a batch; `featurize_problem` (`science/bo/featurize.py`) turns molecular categoricals into GFN2 descriptors; judgment in `experiment-design/SKILL.md`. Inline — no Temporal dependency | — |
| 3.2 | technician: feed a result back and get the next conditions, across bench sessions | `MISSING-TOOL` | The store exists and is written (`record_suggestion`, `science/bo/campaign_record.py:186`; Postgres in `campaign_record_store.py`; schema `infra/sql/031_bo_campaigns.sql`). `read_campaign` and `suggestions_for` have **zero non-test callers** — no MCP tool, no agent tool, no skill reaches them. Absent: a `resume_campaign(campaign_id)` tool, or a `campaign_id` argument on `suggest_next_experiment` that loads prior observations | `S` |
| 3.3 | lab leader: set up a BO from natural language — ranges, constraints, one *or more* objectives | `PARTIAL` | Ranges: served (`ContinuousParameter`/`CategoricalParameter`, `problem.py:25/41`; framing in `experiment-design/SKILL.md`). Multi-objective: **unrepresentable** — `OptimizationProblem.objective` is one field (`problem.py:125`), `_to_domain` builds one `ContinuousOutput` (`engine.py:93`), no MOBO/Pareto/scalarization anywhere. Constraints: **no concept exists** — `OptimizationProblem` has two fields, and BoFire's `Domain(constraints=…)` is never passed one. Cost/greenness as an objective: `green_metrics` is a tool, not a registered objective (`science/bo/objectives.py:90` holds exactly `reizman-suzuki` and `solubility-max`) | `M` each |
| 3.4 | lab leader: explain why it suggests these conditions (exploring vs exploiting) | `PARTIAL` | The quantities exist and are surfaced: `Candidate.predicted_value`/`predicted_sd` (`problem.py:158`), read from BoFire's `<obj>_pred`/`<obj>_sd` in `_frame_to_candidates` (`engine.py:116`), returned in `ExperimentSuggestion`; `_surrogate_belief` (`connectors/bo/knowledge.py:167`) renders the explore/exploit reading on a `bo-candidate` note. Missing: no reference scale for the sd (nothing compares it to the observed spread or the other candidates), the acquisition column `_des` is deliberately dropped (`engine.py:127`), and the skill *body* has no explore/exploit section — only its front-matter description claims the job | `S` |
| 3.5 | lab leader: tell me when an optimization has plateaued | `MISSING-TOOL` | Nothing computes convergence. `campaign.py::optimize` runs exactly `n_rounds`; the only early stop is `space_exhausted` (`problem.py:312`), which is discrete-space exhaustion, not a plateau. `bo_regret` (`evals/metrics.py:135`) needs a known reference optimum from a committed case file and is not agent-reachable. Absent: a `campaign_progress` tool computing best-so-far per round, improvement over the last *k* rounds **against a caller-supplied assay noise**, and max posterior sd / expected improvement over the space | `S`–`M` |
| 3.6 | manager: cross-project visibility into effort optimization saves vs traditional screening | `MISSING-DATA` | Three absences. No effort/cost figure on any record: `OrdReaction`, `Note`, `bo_suggestions` and the job record all carry no duration, labour, material cost or instrument time. No counterfactual: nothing records the factorial baseline a BO campaign displaced. No project entity (see 4.6). `find_past_jobs` (`agent/durable_tools.py:224`) lists durable runs with their reasons — a proposal count, not effort saved | `L` |

### Notes

**3.2 — the finding, stated precisely.** `D-2026-07-31-a-campaign-is-an-entity-not-a-turn` built the
campaign entity for exactly this story: a stable `campaign_id_for(problem)` hash so three refinements
of one optimization accumulate on one row, both store backends, a Postgres table, and a tool
docstring (`connectors/bo/server/tools.py:88-94`) plus a skill (`experiment-design/SKILL.md:85-89`)
that both instruct the agent to "quote the `campaign_id` back … that id is how a chemist, or you in
a later session, picks the thread back up." **Nothing can pick it up.** A grep across the tree finds
`read_campaign` and `suggestions_for` defined twice (Protocol + two backends) and called only from
`tests/test_bo_campaign_record.py`. The write half is complete and correct; the read half was never
wired to a caller. Consequence: a chemist returning tomorrow must re-type every prior observation as
a tool argument, and the id they were told to keep does nothing. Note the nuance — *within* a
resumed session id the durable conversation history (`agent/session_store.py`) carries the
observations, so a same-session loop works until history compaction drops them; the break is the new
session, which is the story's actual setting ("across bench sessions"). `op-20` was graded a harness
limitation (fresh single-turn session), and the code analysis shows the harness was not the binding
constraint.

**3.3 — three separate ceilings, and one of them is a docstring away from honest.** The module
docstring at `problem.py:1-8` is explicit — "one scalar objective; multi-objective comes when a real
problem needs it" — and `experiment-design/SKILL.md:32-38` gives the correct fallback in bold ("pick
the one they lead with, say so explicitly, and call the tool for that one only … do not dress up a
single-objective call as if it were a real multi-objective/Pareto optimization"). But the *tool
schema* the model sees says none of this: `suggest_next_experiment`'s docstring mentions "the single
objective" once in an `Args:` line and never says multi-objective or constrained optimization is
unsupported. `op-16` was graded `fabricated` for precisely that — it promised to call the tool "with
both objectives". **Cheapest honest improvement: two sentences in that docstring** naming the
boundary, which is hours of work and converts a fabrication into a correct refusal. The real
capability is `M` twice over: `constraints: list[Constraint]` on `OptimizationProblem` mapped onto
BoFire's `LinearInequalityConstraint`/`NChooseKConstraint` (which the library already ships, so this
is wiring rather than research), and `objectives: list[Objective]` + `MoboStrategy` + a Pareto
front replacing `best_of` (`problem.py:266`).

**3.4 — this is a rubric gap, not a computation gap.** The hard part was already done: the sd was
being computed on every model-guided ask and dropped one function short of anything that could
record it, and `_frame_to_candidates` now recovers it. What is still missing is the interpretive
scale. `predicted_sd` comes back in the objective's own units with nothing to compare it to — a
±3.1% posterior sd is an exploit if the observed yields span 40 points and an excursion if they span
4, and neither the tool result nor the skill says to make that comparison. A section in
`experiment-design/SKILL.md` telling the model to quote the sd against the observation spread, and
to say plainly when `predicted_sd is None` that no surrogate had an opinion (a seed point), is the
whole fix.

**3.5 — the shape the tool should take is dictated by the failure it prevents.** `op-13` was graded
`fabricated` for asserting "the last 1–2% gains are real" against a ±2% reproducibility the user had
*stated in the question*. A plateau test that does not take the assay noise as an argument would
reproduce that error with more authority. The inputs all exist: `CampaignResult.history` for the
durable path, the caller's `observations` list for the inline path, `best_of` for the running best,
and `Candidate.predicted_sd` for the model's remaining uncertainty.

**3.6 — a partial deliverable is available cheaply and is worth naming.** The full metric is `L` and
gated on data nobody has. But `discrete_candidate_count` (`problem.py:281`) already computes the
full-grid size of an all-categorical problem, and `bo_campaigns.problem` already stores the decision
space as JSONB. Recording the suggestion count per campaign — one `COUNT(*)` on `bo_suggestions` —
would let the system say "this campaign reached its best point in 11 proposals against a 96-cell
full grid", which is a defensible design-space efficiency claim even with zero labour data.
`op-28` graded `served` for refusing the number and naming exactly this ("decision-space size,
actual BO run count, matched factorial baseline"); `op-27` graded `fabricated` for supplying an
illustrative savings figure. Both behaviours are reachable today, which is the argument for making
the boundary a tool output rather than a hoped-for judgment.

---

# §4 — Planning HTE Campaigns

**Section summary:** §4 splits cleanly along one line, and the line is physical. Everything that is
a *set of conditions* is served — the design enumeration, the reagent identity resolution, the
per-species mass, the pairwise hazard screen, and a genuinely rich corpus of real published HTE
records to ground reagent choice in. Everything that is a *physical plate* is absent at every level:
a tree-wide search finds no plate, well, position, volume, stock-concentration or density concept
anywhere in `src/`, and the only occurrences are eval probes written to catch the agent inventing
one. The most consequential single defect is in the middle of that line: `stoichiometry_table` has
no notion of volume or concentration, so a solvent charge — the term that dominates E-factor, PMI
and every practical "do I have enough" check — cannot be expressed at all, and the documented
pairing with `green_metrics` breaks on exactly that term. The single biggest thing standing between
this section and the rest is that HTE is a *campaign-level* activity and the system reasons one note
at a time: nothing aggregates or ranks a set of runs, and roughly four times more reactions sit in
the fingerprint index than are citable as notes, with no way for an answer to say which corpus it
read.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 4.1 | technician: lay out an HTE plate (reagents, catalysts, conditions per well) from a screening goal | `PARTIAL` | Condition list: `generate_screening_design` returns every categorical combination as ordered runs (`ScreeningDesign.runs`, `problem.py:181`); `stoichiometry_table` gives per-species mass. Plate: **nothing**. No plate/well/position concept in `src/`; no `stock_solution` record; no density on `ResolvedCompound` (`core/reagents.py`), so mL↔g is not computable even for THF; no liquid-handler or worklist connector. Absent specifically: a `plate_layout(design, format, volume)` tool and the two record types it would need | `M` |
| 4.2 | technician: check a campaign for practical issues (volumes, stock concentrations, incompatible combinations) | `PARTIAL` | Incompatibilities: `screen_hazards` → `screen_reaction` (`science/safety/screen.py:211`) cross-checks every component pair — but against 16 structural + **5** pairwise process-safety rules, not chemical compatibility. Volumes: **absent, and this is a real defect** — `stoichiometry_table` (`connectors/chem/server/tools.py:89`) takes only `basis_mass_g` + molar `equivalents` and has no volume, concentration, density or reaction-scale input, so a solvent charge cannot be expressed. Stock concentrations: absent as 4.1 | `S` (volumes) |
| 4.3 | lab leader: propose which reagents/catalysts/conditions to include, from the chemistry and past campaigns | `PARTIAL` | Serves: `similar_reactions` over the DRFP index (Doyle BH, Perera Suzuki, Santanilla amidation, Nielsen deoxyfluorination), `gather_evidence` with a `reaction_smiles` anchor, `expand_note` for merged runs' real conditions, `playbook` + `optimization-campaign` notes, `compute_electronic_properties`/`predict_site_reactivity` for a computed shortlist with the discipline in `experiment-design/SKILL.md` §"Narrowing a categorical space". Missing: descriptors are **electronic only** (`DESCRIPTOR_NAMES`, `featurize.py:38`) — no cone angle or buried volume, i.e. no steric axis, which is what phosphine choice turns on; and the index is reachable only via an explicit `reaction_smiles` anchor, never from a prose question | `M` |
| 4.4 | lab leader: choose the right screening strategy (full grid vs a smarter reduced design) for a campaign size | `PARTIAL` | The *choice* is real: `discrete_candidate_count` (`problem.py:281`) sizes the grid, and both `experiment-design/SKILL.md` §"The other DoE question" and `skills/deep-research/SKILL.md` §6 frame full-factorial vs adaptive BO explicitly. The *reduced design does not exist*: `factorial_design` (`engine.py:193`) builds `FractionalFactorialStrategy` at default `n_generators=0`, which returns the plain Cartesian product. No fractional factorial, Plackett–Burman, D-optimal, LHS, blocking or replication — only "the whole grid" or "adaptive BO" | `S` |
| 4.5 | lab leader: summarize and rank a completed HTE campaign's results against my objectives | `PARTIAL` | Works for the merged subset: `gather_evidence(note_type="optimization-campaign")` → `optimization_campaign_note` (`memory/optimization.py:67`) lays runs out chronologically with temp/time/yield and per-run deltas; `optimization-campaign-synthesis/SKILL.md` holds the ranking discipline. Does not work for the rest: `ingest_reaction` (`ingest/eln/ingest.py:44-50`) indexes unconditionally but PR-gates the note, so the index holds ~4,251 reactions against ~987 citable notes; `FingerprintReactionRetriever` (`retrieval/retrievers.py:262`) returns a one-line stub with no yield, citing a note id that may not exist. There is no fingerprint entry in `ingest/sources/` and no aggregate tool over a *set* of runs. "Objectives" plural is also blocked by 3.3 | `S`–`M` |
| 4.6 | manager: visibility into HTE throughput and hit rates across the department | `MISSING-ENTITY` | No `project`/`department` field on `Note` (`kg/note.py:196-240`) — a "project" is a free `tag`, and `kg/analytics.py:72` treats it as one; `OrdReaction.project` (`ingest/eln/ord.py:181`) is an ingest-time grouping key that lands in tags. No throughput record: nothing counts plates, wells or campaign duration. No hit-rate basis: `optimization-campaign` notes are deliberately output-neutral (`memory/optimization.py:84`), so no success criterion is stored to count hits against | `L` |

### Notes

**4.1/4.2 — the `stoichiometry_table` defect, measured rather than asserted.** The signature is
`stoichiometry_table(basis, basis_mass_g, reagents, equivalents)` and every row it emits is
`mass_g = mmol × MW / 1000`. There is no parameter, no return field and no downstream helper
anywhere in the tree that carries a volume, a molarity, a density or a reaction concentration.
Chemists charge solvent as volumes-relative-to-substrate ("10 vol") or as a target molarity ("0.2 M
in the aryl halide"); neither is expressible, and the only available encoding — solvent as a molar
equivalent of the limiting reagent — is not a form anyone writes or checks. This is not a cosmetic
gap because the two tools are documented as a pair: `green_metrics`'s own docstring
(`connectors/chem/server/tools.py:174-181`) says "Pair it with `stoichiometry_table`, whose `mass_g`
column is exactly this input" and then, two lines later, "Omitting solvent is the usual way these
numbers get flattered; include it." The pairing therefore breaks on precisely the term that
dominates both E-factor and PMI, and the only route to a solvent mass is the model doing density
arithmetic from a density it invented. `op-29` graded `served` because the model did exactly that
sort of arithmetic (0.5 µmol in 25 µL = 20 mM) correctly by hand — which is evidence that no tool
does it, not that the tool works.

**Cheapest honest improvement for 4.2, and it is small.** Add a `density_g_per_ml` field to
`ResolvedCompound` and populate it for the ~15 solvents already in `core/reagents.py`'s solvent
block, then add an optional `solvent`/`volumes_ml` (or `concentration_m`) input to
`stoichiometry_table` that emits a real `mass_g` row. That single change closes the practical
"volumes needed" half of 4.2, makes the documented `green_metrics` pairing true, and gives 4.1 the
unit conversion any future plate layout would have needed anyway. `S`.

**4.3 — the recall problem is separate from the reasoning problem.** The corpus genuinely contains
what this story needs, and the reasoning path is sound. What fails is reaching it: the fingerprint
index is bolted onto `gather_evidence` as a conditional extra leg
(`agent/research_tools.py:153-155`), fired only when the model supplies a `reaction_smiles` anchor,
and it is deliberately *not* a registered data source — `ingest/sources/` holds `graph`, `eln-json`,
`eln-ord`, `lexical`, `vector`, `vendored`, with `data_sources` defaulting to `"graph,eln-json"`
(`core/config.py:1575`). So a prose question ("which ligands have worked for this amination") sweeps
the note tree alone. `op-24` graded `unserved` — the three-plate Doyle screen was in the corpus as
660 notes and retrieval missed all of them; `op-30` graded `unserved` for the same reason on the
Perera flow records. `op-11` graded `fabricated` (invented "Catherine Phos", mischaracterised
JackiePhos as electron-donating) is the downstream cost of a shortlist that was not grounded in
retrieved records.

**4.4 — same root as 2.3, and it is the best-value fix in §4.** `FractionalFactorialStrategy` is
already imported and constructed at `engine.py:211`; only its defaults are used. BoFire exposes
`n_generators`, `block_feature` and `n_repetitions` on that same class. Threading them through
`generate_screening_design` — plus a `resolution` field on `ScreeningDesign` so the answer can say
what was aliased with what — converts the system's answer to "96 wells, 7 factors" from "here is the
full grid, which does not fit" into an actual design. `op-14` graded `partial`: the model typed the
problem correctly and got 4 × 3 × 2 = 24 right, then never called the tool.

**4.5 — whether a campaign-level ranking is answerable at all, worked through.** It is, for one
subset, and the system cannot tell you which. `ingest_reaction` indexes the DRFP fingerprint and
*proposes* the note; the index is a deterministic serving index and the note is PR-gated, so the two
corpora diverge by design — 4,251 indexed against 987 citable in the run corpus
(`docs/archive/live-user-stories-2026-08.md:290`). For a merged reaction, `expand_note` returns
conditions, yield and procedure prose, and `optimization_campaign_note` will already have laid a
DRFP-similar group out chronologically with per-run deltas; that is a real ranking substrate and it
is what `op-25` (Nielsen deoxyfluorination, all yields verified) and `op-32` (Santanilla, correct 0%
results reported) rode. For an indexed-but-unmerged reaction, the only thing retrievable is
`"Similar reaction {label} (Tanimoto 0.87)"` citing a note id that resolves to nothing — **no yield,
no conditions, no objective value**. So a "rank these 96 wells" question is answerable exactly to
the extent the plate's notes were merged, and no signal distinguishes "not in the record" from "in
the index, not yet a note".

What would make it answerable, cheapest first: (i) carry the outcome on the index row or have
`FingerprintReactionRetriever` fall back to the ELN record when the note is unmerged, so a hit
carries a yield — `S`, and it also fixes 4.3's recall cost; (ii) an aggregate
`rank_campaign_results(reaction_ids, objective)` tool that reads outcomes across a *set*, since
every existing path is one note at a time — `M`; (iii) plural objectives, which is 3.3. Even (i)
alone changes the honest answer from "I could not find it" to "I found 41 of 96 as citable runs;
here they are ranked, and 55 are indexed without approved notes."

**4.6 — why this is `MISSING-ENTITY` rather than `MISSING-DATA`.** Unlike 3.6, the raw material
partly exists — reactions carry dates, campaigns group them, `find_past_jobs` lists durable runs.
What has no home is the roll-up axis. A `project` is a string in `Note.tags`, which means it cannot
be renamed, merged, validated, or given a department, an owner or a date range; `kg/analytics.py`'s
`projects_without_distillation` is the closest thing to a portfolio view and it is a list of tags
with no playbook above them. Adding `project` as a first-class field is a migration across every
merged note, hence `L`. Cheapest honest improvement is again a correct refusal: `op-08` graded
`unserved` not for refusing but for handing the question back *without* stating that no
project/portfolio entity exists — the system knows what its note types are (`KNOWN_NOTE_TYPES`) and
could say so.
