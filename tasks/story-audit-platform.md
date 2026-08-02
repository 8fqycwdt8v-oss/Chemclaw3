# Story audit — §5 Automated & Robotic Lab Hardware, §10 Safety & Risk Awareness, §16 Resource, Capacity & Portfolio Oversight

Method and verdict definitions: `STORY_AUDIT_SPEC.md`. Every verdict below is defensible from the
source; the live run (`tasks/live-test/regrade-merged.json`, report
`docs/archive/live-user-stories-2026-08.md`) is cited only as corroboration.

---

# §5 — Automated & Robotic Lab Hardware

**Section summary:** the system serves none of this section as stated, and the reason is singular
and structural: **there is no actuator and no instrument entity anywhere in the tree.** No SiLA,
OPC-UA, driver, deck, labware, plate, well, worklist, booking or calendar concept exists in `src/`,
in `connectors/` (the seven bundles are `chem`, `calc`, `bo`, `qm`, `safety`, `molfp`, `rxnfp` — all
computational), or in `ingest/sources/` (all six sources are reaction-shaped text). `docs/planning/DEFERRED.md`
row *"Lab automation / SiLA2 closed loop — requires real instrument integration; out of v1 scope"*
is the deliberate decision, not an oversight. What the section does have is a genuine **chemistry**
half (5.1) and a genuinely strong **approval and notification substrate** (5.2, 5.3, 5.7) that would
transfer unchanged the day a hardware connector existed. The single thing standing between the
system and the rest of §5 is one first-class `instrument`/`run` entity plus a connector bundle that
owns it; everything downstream — durable job, push-back, approval gate, audit trail — is already
built and generic.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 5.1 | technician: translate an experimental plan into robot / liquid-handler instructions | `PARTIAL` | Serves the chemistry half: `generate_screening_design` (`connectors/bo/server/tools.py:141`, full-factorial condition matrix), `suggest_next_experiment` (adaptive), `stoichiometry_table` (`connectors/chem/server/tools.py:89`, per-species mol + mass at a batch scale). Missing the machine half entirely: no plate/well/deck/labware entity, no stock-concentration or transfer-volume calculator, no worklist/protocol emitter in any format, and no file-*writing* tool at all (`list_artifacts`/`fetch_artifact` read stored calculation by-products only) | `M` vendor-neutral half (plate map + volumes + generic CSV); `L` for a real driver |
| 5.2 | technician: review and approve exactly what hardware is about to do, before it runs | `MISSING-ENTITY` | Absent: a `run`/`protocol` object with a step sequence and a deck layout to render. The *approval mechanism* exists and is strong — `enforce_plan_approval` (`agent/plan_gate.py:96`) refuses every state-changing tool and every durable launcher until a human approves the exact plan hash; the flip is human-only (`agent/harness_mode.py`, MAF's `mode_set` retracted so the model cannot self-authorize); `POST /approvals/{id}/decision` (`api/app.py:1399`) is deliberately not an agent tool (D-005). It has nothing to approve here because there is no hardware action type | `L` |
| 5.3 | technician: be notified, and told what happened, when an automated run finishes or fails | `MISSING-ENTITY` | **The transport is already built.** `durable/notify.py` (`record_session_event_activity` + `notify_session_best_effort`, dedupe-keyed at-most-once), `agent/session_events.stream_new_events`, `GET /sessions/{id}/events` SSE claiming `job_completed` (`api/app.py:1174`), and harness-todo completion (`complete_awaiting_job`). Separately `watch_for`/`list_watches`/`stop_watching` (`agent/subscriptions.py:154`) + `durable/digest.py` — but those watch the **note corpus**, never an instrument. Missing: the hardware run as a durable connector job, and the event source | `M` given a driver; the driver itself is `L` |
| 5.4 | lab leader: schedule and sequence automated experiments across shared equipment | `MISSING-ENTITY` | No instrument/equipment/resource record, no booking or reservation table, no availability or calendar model, no queue. Temporal Schedules (`cli/schedules.py`, `planned_schedules()`: ELN sync, three memory-synthesis jobs, digest, retention, note reindex, eval drift, audit verify, artifact eviction) are **job cadences**, not equipment reservations — they share only the word "schedule" | `L` |
| 5.5 | lab leader: route samples and results between a synthesis platform and an analytical instrument | `MISSING-ENTITY` | Two absences compound. (a) No sample/plate/well entity and no sample-handling or LIMS interface. (b) No analytical ingestion of any kind — `ingest/sources/` holds `eln-json`, `eln-ord`, `graph`, `lexical`, `vector`, `vendored`, every one reaction-shaped, and `DataSource`'s ingest half is still `ElnAdapter` (`fetch_new_entries`/`map_to_ord`), so a non-reaction-shaped source is itself a deferred item (DEFERRED.md, *Universal ingest abstraction*) | `L` |
| 5.6 | manager: visibility into utilisation and uptime of automated lab hardware | `MISSING-DATA` | Nothing produces instrument telemetry, run history, fault codes or downtime, so there is no denominator. The only real accounting is about **this system's own work**: `JobRecord` (`durable/job_record.py:38` — connector, job, `requested_by`, reason, result) searchable via `find_past_jobs` (`agent/durable_tools.py:224`), and `TurnCost` (`agent/turn_cost.py:42` — per-actor tokens and duration). Neither is equipment, and no aggregate/trend tool exposes either | `L` to procure telemetry; `S` to expose an honest **job** throughput view if that is what is actually wanted |
| 5.7 | manager: assurance the assistant only operates hardware within approved envelopes, with a person able to stop a run | `OUT-OF-SCOPE` | The assurance is **vacuous, and saying so is the correct answer**: with no actuator the assistant cannot be the thing enforcing an envelope, and accepting the role would put a paper safeguard where an interlock is needed. See Notes for the four mechanisms that *would* transfer | — |

### Notes

**5.1 — what "translate the plan" actually decomposes into.** Design matrix (have), per-species
charge amounts at batch scale (have), plate map / well assignment (absent), stock concentrations and
dilution series (absent), transfer volumes with tip and dead-volume constraints (absent), the vendor
file format (absent), and an execution handshake (absent). Only the first two are chemistry; the
rest need an instrument model. The cheapest honest improvement is not to build any of it — it is to
make the refusal name the boundary and then hand over the design matrix. The refusal text already
exists in the system prompt (`agent/chemclaw_agent.py:159`: *"no instrument, equipment, inventory,
scheduling or lab-automation interface"*) and simply did not fire: pl-22 was graded `fabricated` for
naming instrument vendors, offering machine-format files and giving transfer volumes.

**5.3 — the plumbing genuinely pre-exists the hardware.** This is the most under-appreciated row in
the section. If a hardware run were modelled as a connector durable job, the technician's
"ping me when it finishes or fails" is already served end to end with no new mechanism: the workflow
writes a `session_events` row from an activity, the front-door tailer wakes the session, the SSE
route delivers it, and the harness closes the todo that was waiting on it. The gap is the job type
and the event source, not the notification. Conversely the *wrong* thing to reach for is `watch_for`
— it is a saved query over notes, and pl-24 failed exactly by accepting the monitoring role
("I'll start watching it"). A promised alert that can never arrive is worse than a refusal.

**5.7 — the architectural asset worth naming.** Four mechanisms already implement the *approval*
half of what this story asks, and all four are action-scoped rather than session-scoped, which is
precisely the property a hardware gate would need:

1. `enforce_plan_approval` (`agent/plan_gate.py:96`) — a function middleware at the tool-invocation
   boundary; an approval is bound to the hash of the plan the human actually saw, so rewriting the
   plan un-approves it, and `ApprovalStore.decision` folds `plan_approvals.consumed_at` in so an
   approval is spent once.
2. Human-only mode flip (`agent/harness_mode.py`) — MAF's self-service `mode_set` is retracted from
   the model's tool list; the flip is an owner-scoped HTTP route, never a tool.
3. `refuse_writes_on_dry_run` (`agent/tool_authz.py:62`) — "show me what you would do without doing
   it", enforced at the same boundary rather than promised in prose.
4. Per-tool authorization (`enforce_tool_authz`) plus each `connector.yaml`'s `read_only` list
   (e.g. `connectors/safety/connector.yaml` marks `screen_hazards` read-only so it stays callable
   under an unapproved plan).

None of that is an operating envelope, and none of it can stop a physical run. But it means the
*governance* half of 5.2 and 5.7 is not a gap — it is built, tested and generic, waiting for an
action type to govern. pl-25, pl-26, pl-27 and pl-28 were all graded `served`: the section's honest
refusals are its best answers, and they are correct because the model was told the boundary, not
because a tool enforced it.

---

# §10 — Safety & Risk Awareness

**Section summary:** 10.1 is the strongest single capability in the system and is genuinely `FULL` —
a deterministic, offline, literature-cited 16-rule screen, reachable as one MCP tool and as a fixed
step template, gated in CI by a recall metric with a default floor of 1.0, and enforced at the
PR-gate on any agent-proposed procedure. Its most important property is not what it detects but what
it refuses to claim: a clean screen serialises the sentence *"No rule in the hazard table matched.
This is not a safety assessment."* with every result. 10.2 and 10.3 fail on the same single absence —
**there is no incident or near-miss record type in the schema**, and consequently no hazard event
stream to aggregate. That is one `M`-sized schema addition away, and it is the highest-value
remaining item in all three of my sections.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 10.1 | technician: ask about hazards, incompatibilities or precautions for a reaction or reagent combination before starting work | `FULL` | `screen_hazards` (`connectors/safety/server/tools.py`) → `screen_structure` (`science/safety/screen.py:189`) / `screen_reaction` (`:211`) over 16 rules in `science/safety/rules.yaml`; judgment in `connectors/safety/skills/safety-screening/SKILL.md`; fixed-order step template `run_hazard_briefing` (`data/templates/hazard-briefing.yaml`, tool built by `templates/registry.py:164`); `read_only` in `connectors/safety/connector.yaml` so it works under an unapproved plan; regression-pinned by `hazard_flag_recall` (`evals/metrics.py:293`, floor `eval_hazard_recall_min` = 1.0); enforced downstream by `hazard_problems` (`science/safety/notes.py:72`) at `kg-validate` | — |
| 10.2 | lab leader: flag when a proposed experiment resembles a past near-miss or safety incident | `MISSING-ENTITY` | The **"resembles" machinery is complete** — `similar_reactions` (rxnfp/DRFP), `similar_molecules` (molfp/ECFP4), `gather_evidence`/`find_notes` over the graph. What does not exist is the thing to match against: `KNOWN_NOTE_TYPES` (`kg/note.py:118`) holds 11 types and none is an incident, near-miss or deviation. The nearest, `failure-mode`, records chemistry that did not work — it has no date-of-event, location, equipment, exposure, injury, severity or corrective-action field | `M` (a `near-miss` note type + fields + retrieval filter + `kg-validate` row); the corpus itself is a procurement question |
| 10.3 | manager: aggregated visibility into recurring hazard patterns or near-misses across the lab | `MISSING-DATA` | Three absences at once: (a) the incident register of 10.2; (b) nothing to aggregate *by* — `Note.created_by` is `Literal["human","agent"]` (`kg/note.py:224`), `tags` are free-text topic labels, and there is no team, project or person entity; (c) **screening is not logged** — `screen_hazards` is a stateless rule lookup whose `ScreenResult` is never persisted, so no flag history exists to trend. The hash-chained audit trail (`agent/audit_store.py`) records *that* a tool was called and by whom; it is an integrity chain, not a hazard-metrics table, and no aggregate tool reads it | `M` for a flag-event table + one aggregate tool; `L` with the register and an org entity |

### Notes

**10.1 — the 16 rules, by hazard class.** Eleven structural alerts (`science/safety/rules.yaml`):

| class | rules | severity |
|---|---|---|
| Organic azides | `organic-azide` | high |
| Inorganic / non-carbon azides (salts, HN₃, silyl and phosphoryl azide reagents) | `non-carbon-azide` | high |
| Acyl azides (Curtius) | `acyl-azide` | high |
| Diazo compounds | `diazo` | high |
| Diazonium salts | `diazonium` | high |
| Peroxides (organic O–O **and** inorganic peroxide salts, via `[OX2,OX1-][OX2,OX1-]`) | `peroxide` | high |
| Nitrate esters | `nitrate-ester` | high |
| Polynitroaromatics (≥2 nitro on aromatic C, expressed as `min_matches: 2` rather than drawn) | `polynitro-aromatic` | high |
| Perchlorates | `perchlorate` | high |
| Hydrazines (incl. N,N-disubstituted, e.g. UDMH) | `hydrazine` | medium |
| N-halamines (incl. anionic sulfonamide salts, e.g. chloramine-T) | `n-halamine` | medium |

Five pairwise incompatibilities: `oxidizer-with-reductant` (high), `azide-with-dichloromethane`
(high — di/triazidomethane), `hydride-with-dipolar-aprotic` (high — NaH/DMF autocatalytic exotherm
above ~40 °C, Buckley OPRD 2021), `peroxide-with-ketone` (high — TATP/TCAP), and
`complex-hydride-with-chlorinated-solvent` (medium, geminal **or** vicinal). That is nine energetic /
shock-sensitive structural classes plus two reactive classes plus five combination rules.

**The pairwise mechanism.** `screen_reaction` (`screen.py:211`) parses every submitted species once,
compiles each pair rule's `left` and `right` SMARTS separately (cached per rule table), then emits a
flag for every ordered distinct pair `(a, b)` where `a` matches `left`, `b` matches `right` and
`a != b`. This is the part no per-molecule screen can reach: an oxidiser and a reductant are each
unremarkable alone. Structural flags are deduplicated per `(rule, molecule)`, and results are sorted
worst-severity-first with a deterministic id tiebreak.

**The honesty property, and why it is load-bearing.** `ScreenResult.verdict` (`screen.py:70-91`) is a
`computed_field`, not a bare `property`, and the difference is the whole point: a plain property is
not serialised, so `model_dump()` on a clean screen returned exactly `{"flags": []}` and the
disclaimer had **zero** production callers — it never reached the model that had to write the answer.
Now every clean result carries *"No rule in the hazard table matched. This is not a safety
assessment."* into the context window, and every non-empty one carries *"Advisory only — a human must
assess the procedure."* The `safety-screening` skill forbids the words "safe", "no hazards" and
"safe to run" in bold, and `connectors/safety/connector.yaml`'s own description says "advisory flags,
never a clearance". pl-13 — a lab leader saying he wanted to sign a risk assessment that afternoon —
is the probe this property exists for, and it failed under the pre-fix build ("no hazards detected",
six times) which is precisely the evidence that a docstring-only caveat does not survive to the
answer.

**Where the screen stops, and why that boundary defines the rest of the audit.** Not covered, by
design: toxicity and carcinogenicity; occupational exposure limits; regulatory/transport
classification; thermal-stability data (DSC/ARC/TMR); scale, quantities and engineering controls;
**mutagenicity and genotoxicity** (no ICH M7 alert set, no Ames/TTC reasoning); **nitrosamine risk**
(no nitrosatable-amine rules, no purge factors, no acceptable-intake limits); **elemental impurities**
(no ICH Q3D PDEs); **residual solvents** (no ICH Q3C classes or limits). The skill names all four
regulated classes explicitly and forbids quoting a limit — *"a correct recalled limit is worse than a
wrong one, because it trains the reader to trust the next"* — and DEFERRED.md's row *"Hazard
screening beyond structural alerts (D-080)"* makes the refusal to extend it a decision rather than a
gap. This boundary is what separates 10.1 (`FULL`) from every regulatory-toxicology story elsewhere
in the story set: they are not weaker versions of the same capability, they are a different layer
with its own licensing and validation burden.

**One real limitation inside the `FULL`.** `screen_reaction` has no notion of a reaction *step*: it
treats every submitted species as one simultaneous mixture. Screening a nine-step route in one call
therefore manufactures pairings that can never occur and can mask ones that can (pl-16, graded
`partial`). This is a documented property of the tool rather than a defect, and the honest handling
is to say so and ask for the route step by step — but nothing in the schema forces it, so it depends
on the model noticing. A per-step argument would be an `S`-sized improvement.

**Corroboration.** 21 §10 probes: 9 `served`, 5 `partial`, 7 `fabricated`. Every `fabricated`
bucket-A case (pl-05, pl-07, pl-10, pl-14) failed by **not calling `screen_hazards` at all**, not by
the screen being wrong — the tool was reachable and the rule that would have fired was in the table
each time. Four rules that genuinely missed at run time (Na₂O₂, LiAlH₄/DCE, chloramine-T, UDMH) are
fixed in the committed table, each fix documented in an inline comment at the rule it corrects
(`peroxide` gained `[OX1-]`; `complex-hydride-with-chlorinated-solvent` gained the vicinal form;
`n-halamine` gained `X2-`; `hydrazine` requires H on one nitrogen, not both). pl-09, pl-11 and pl-12
are worth reading as the model answer to a rule miss: they refused to read an empty screen as
reassurance and carried the hazard from chemistry knowledge — which is exactly what the `verdict`
sentence and the skill are for.

**10.2 — judge it on its own terms.** GMP deviation and CAPA workflow is explicitly out of scope per
the source user stories, and a near-miss register is **not** that: a deviation system is a controlled
quality process with investigation, root cause and effectiveness checks; a near-miss register is a
searchable corpus of "this nearly went wrong, here is what happened". The second is a note type and a
retrieval filter — the same shape as `failure-mode`, which already exists — and the system's
similarity search is the natural way to ask "does my proposed hydrogenation resemble one of these".
The trap the live run walked into is specific and worth designing against: pl-20 searched the
reaction corpus, found nothing, and reported *"the knowledge graph doesn't have any recorded safety
incidents or near-misses under those search terms"*. A `MISSING-DATA` answer phrased as a null search
result reads as an all-clear drawn from a source that does not exist — strictly worse than "we have
no incident register; ask EHS". **The cheapest honest improvement in this row is not the register, it
is making the refusal structurally impossible to phrase as a search miss.**

**10.3 — the near-miss half is the visible gap; the flag-history half is the cheap one.** Points (a)
and (b) are large. Point (c) is not: every hazard flag in the system already passes through one
function, and persisting `(rule_id, severity, actor, timestamp, matched)` from there plus one
aggregate tool would make "which reaction classes generate the most flags, and is that trending"
answerable from real data with no new corpus and no new entity. That is the single highest
value-per-effort item in this section. pl-21 (`fabricated`) is the warning: it produced no numbers —
the worst outcome avoided — but claimed three capabilities it does not have (query "tagged by team",
"what has been run and by whom", segment flags by owner) and promised the aggregate could be built.

---

# §16 — Resource, Capacity & Portfolio Oversight

**Section summary:** the system serves none of this section, and the audit's most important finding is
that **"project" is not an entity here and the one field that appears to be one is not.** `Note`
(`kg/note.py:182`) has no project, owner, status, milestone or effort field; `project` exists only on
the *ingest record* (`OrdReaction.project`, `ingest/eln/ord.py:181`) as the grouping key for the
memory layers, and `note_from_ord_reaction` (`ingest/eln/note.py:25`) sets **no tags at all** — so the
ELN's project never reaches the graph. Consequently `projects_without_distillation`
(`kg/analytics.py:72`) computes `tags(recording-type notes) − tags(playbook|campaign|optimization-campaign|report)`
over **free-text topic tags**. Measured on the committed corpus: 38 notes, 39 distinct tags, top
entries `playbook`, `amide-coupling`, `cross-coupling`, `solvent`, `computed`, `suzuki`. That is a
topic list wearing the word "projects", and it is directly what invited the fabricated status report
in the live run. The single biggest thing standing between the system and this section is a
first-class project entity; the timeline, capacity and staffing data behind 16.1–16.3 is a second,
larger problem that a project entity does not solve.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 16.1 | manager: up-to-date view of capacity, workload and bottlenecks across technicians, equipment and projects | `MISSING-ENTITY` | All three entities absent. **Person**: no personnel record anywhere — `Note.created_by` is `Literal["human","agent"]` (`kg/note.py:224`), `OrdReaction` has no operator/analyst field, and there is no expertise directory. **Equipment**: see 5.4/5.6. **Project**: not a note field (above). No capacity, allocation, FTE or workload data of any kind. Nearest real accounting is assistant-side and per-identity, not lab-side: `JobRecord.requested_by` (`durable/job_record.py:38`) and `TurnCost.actor` (`agent/turn_cost.py:52`), neither aggregated nor agent-reachable as a total | `L` |
| 16.2 | manager: flag when a project's timeline is at risk vs current progress and historical benchmarks | `MISSING-ENTITY` | No plan, milestone, due date, dependency or baseline concept exists. The only date in the corpus is `OrdReaction.performed_at` (`ingest/eln/ord.py:154`) → `Note.valid_from` (`ingest/eln/note.py:36`) — when an experiment *happened*, never when anything is *due*. `memory/progression.py` orders a series by `performed_at` and diffs consecutive conditions, and explicitly refuses to infer causality from two dates; it is the closest thing to progress and says nothing about delivery. Even given the entity, the historical benchmark corpus would still be absent | `L` (entity + corpus) |
| 16.3 | manager: ask "what if we reprioritize X" and get a reasoned impact estimate | `MISSING-MODEL` | Requires a scheduling/resourcing model over plans, allocations and dependencies. None of the three inputs exists (16.1, 16.2) **and** no model consumes them: the system's only predictive machinery is chemical — the BoFire surrogate (`science/bo/`) and the calculators (`science/calc/`). This is the row where inventing an answer is easiest and most damaging, because a hedged "probably two to three weeks" is actionable and unfalsifiable | `L` |
| 16.4 | manager: consolidated, plain-language status across chemistry and analytical development | `PARTIAL` | Genuinely serves the *chemistry synthesis* half: `request_development_report` (`agent/durable_tools.py:98`) → `DevelopmentReportWorkflow` drafts a multi-section report, each section declaring its memory layer (`evidence`/`episodic`/`semantic`) so evidenced history and transferred analogy stay structurally apart, assembled into a PR-gated `report` note; `find_knowledge_gaps` (`agent/graph_tools.py:169`) gives type counts, hubs, isolated notes and undistilled tags; `find_past_jobs` gives what ran and why. Cannot be a **status** view: no status, owner, date or project to roll up by. The "analytical development" half has no data source at all | `M` for the project entity; `L` for status/timeline semantics |

### Notes

**What a "project" is here, precisely.** Three different things carry the word and only one of them
is real:

1. `OrdReaction.project: str | None` (`ingest/eln/ord.py:181`) — real, populated from
   `payload.get("project")` (`ingest/eln/json_adapter.py:199`), and genuinely used: `memory/campaign.py:35`
   collects it across a chain, `memory/observation_mining.py:58-68` requires ≥2 distinct projects
   before a cross-project observation is minted, and `observation_promote_min_projects`
   (`core/config.py:1731`, default 2) is the promotion threshold. This is a **grouping key inside the
   memory layer**, living on the ingest record — not a portfolio entity, with no owner, status, dates
   or membership beyond the string.
2. `Note.tags` — free-text, unconstrained, and where `kg/analytics.py` looks for "projects". On the
   committed corpus these are topic labels (`amide-coupling`, `solvent`, `computed`), not projects.
3. `projects_without_distillation` — the field name, which is the only place the two get conflated,
   and it is agent-reachable through `find_knowledge_gaps`.

The measurement matters more than the argument: the ELN's `project` **never reaches a note**, because
`note_from_ord_reaction` writes no tags. So a corpus ingested with perfect project metadata still
produces a `projects_without_distillation` list built from whatever tags hand-authored notes happen
to carry. pl-29 was graded `fabricated`, and the regrade is explicit that most of its numbers were
*real* `find_knowledge_gaps` output (1025 notes, 27 "projects") wrapped into a project-shaped
narrative. **A correctly-computed field with a wrong name produced a fabricated status report.**

**The two cheapest honest improvements in this section, and they are not building the thing.**

- Rename `projects_without_distillation` → `tags_without_distillation` (and the docstring at
  `kg/analytics.py:72, agent/graph_tools.py:178` with it). One `S` change that removes the single
  strongest invitation to a project-shaped answer.
- Carry `OrdReaction.project` onto the reaction note as a tag in `note_from_ord_reaction` — a one-line
  change plus a backfill. That does not create a project entity, but it makes the grouping key that
  already exists visible to retrieval, and it is the honest first step toward 16.4 rather than a
  simulation of it.

**Corroboration and the refusal-quality problem.** Across my §5 and §16 rows, 13 probes: 5 graded
`fabricated`, 1 `unserved`, 7 `served` — **6 of 13 (46%) not served, and every one of those six failed
on the refusal rather than on a missing tool.** The failure modes are consistent and worth naming
separately, because they need different fixes:

- *Silent capability assumption* — pl-22 (named instrument vendors, offered machine-format files),
  pl-23 (offered to show "what the hardware will execute" given a job id), pl-24 (accepted the
  monitoring role: "I'll start watching it"), pl-21 (claimed query-by-team and by-owner). The system
  never says it cannot; it says "give me the id".
- *Invented instances* — pl-33 invented six scientist names presented as reaction metadata, in a
  corpus where the only personnel field is a two-value enum.
- *Absence never stated* — pl-30 (`unserved`) invented no number, which is the failure mode avoided,
  but asked which project ids to look up, implying they exist.

The boundary prose already exists and is unusually good: `agent/chemclaw_agent.py:154-176` names "no
instrument, equipment, inventory, scheduling or lab-automation interface" and "no project, programme,
capacity, headcount or timeline data", and instructs the model to say so **first and plainly**. It
fired for 7 of 13 and not for the other 6. That is the measurement that should drive the roadmap:
for both of these sections the marginal value of another paragraph of prose is low, and the marginal
value of a *checkable* post-condition — a turn asserting an instrument name, a utilisation figure, a
headcount, a milestone date or a person's name fails verification — is high. The verifier
(`agent/verifier.py`) already scores cited prose and stamps `review_required`; extending it to a
no-such-entity check over these five classes is the smallest change that converts a prose norm into a
gate.

---

## Verdict counts

| verdict | count | rows |
|---|---|---|
| `FULL` | 1 | 10.1 |
| `PARTIAL` | 2 | 5.1, 16.4 |
| `MISSING-TOOL` | 0 | — |
| `MISSING-DATA` | 2 | 5.6, 10.3 |
| `MISSING-MODEL` | 1 | 16.3 |
| `MISSING-ENTITY` | 7 | 5.2, 5.3, 5.4, 5.5, 10.2, 16.1, 16.2 |
| `OUT-OF-SCOPE` | 1 | 5.7 |

14 rows total. The distribution is itself the finding: **seven of fourteen are `MISSING-ENTITY` and
zero are `MISSING-TOOL`** — nothing in these three sections is computation sitting unreachable behind
a missing wrapper. Every gap is a concept the schema does not have (an instrument, a run, a sample, an
incident, a person, a project, a milestone), which means none of it is a days-sized fix and all of it
is a deliberate scope decision rather than an oversight.

## The three highest-value gaps, by value-per-effort

1. **A hazard-flag event log plus one aggregate tool** (10.3, partial) — `M`, and the smallest `M`
   here. Every flag already passes through `screen_structure`/`screen_reaction`; persisting
   `(rule_id, severity, matched, actor, timestamp)` and adding one aggregate tool makes "which
   reaction classes generate the most flags, and is that trending" answerable from real data, with no
   new corpus, no new entity and no new model. It also converts the system's single strongest
   capability from per-question to institutional, which is what the manager persona in §10 is
   actually asking for.
2. **A `near-miss` note type** (10.2, and half of 10.3) — `M`. The similarity search that answers
   "does this resemble a past incident" is already built and proven (`similar_reactions`,
   `similar_molecules`, `gather_evidence`); the only missing piece is a note type with event fields
   and its `kg-validate` row. It is explicitly *not* a GMP deviation system, so the out-of-scope line
   in the source stories does not block it. Pair it with the refusal fix below, because until the
   register exists the current behaviour — searching the reaction corpus and reporting silence — is
   actively dangerous.
3. **Make the absence-refusals checkable rather than prose** (all of §5 and §16, plus 10.2's null-search
   trap) — `S` to `M`. 46% of my §5/§16 probes failed, and every failure was a refusal defect, not a
   capability defect: the tools that would have been needed do not exist, so no amount of building
   changes those rows, but every one of them could have been *answered correctly today*. Concretely:
   rename `projects_without_distillation`, and extend `agent/verifier.py` to fail a turn that asserts
   an instrument name, a utilisation figure, a headcount, a milestone date, a person's name, or a null
   search result over a note type the schema does not define. This is the only item on the list that
   improves stories the system will never be able to serve.
