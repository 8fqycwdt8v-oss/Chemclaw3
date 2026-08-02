# Story audit — reporting, documents, regulatory input, tech transfer

Method and verdict vocabulary: `STORY_AUDIT_SPEC.md`. Every `FULL`/`PARTIAL` names the code that
serves it; every `MISSING-*` names what is absent. Live-run probes (`tasks/live-test/`,
`docs/archive/live-user-stories-2026-08.md`) are cited as corroboration only — no verdict rests on
them. Temporal was down for the whole live run, so `request_development_report` never executed
there; that is an environment fact and is judged as a deployment requirement, not a gap.

---

# §9 — Data Interpretation & Reporting

**Section summary:** The system reads raw data a chemist hands it and summarises it against cited
internal evidence — that half is real and reachable. What it cannot do is *compare against a norm*:
there is no stored numeric series for experimental outcomes (yield, purity and temperature exist on
the ingest schema but are written into note **prose**, never into a queryable field), no
specification or acceptance-criterion entity anywhere in the tree, and no statistics engine in
`science/`. The one genuine anomaly detector — the predicted-vs-measured residual ledger behind
`calculator_trust`/`calculator_outliers` — scores *calculators*, not experiments, and covers exactly
two properties. The single biggest thing between this section and the rest is that outcome numbers
never reach the note schema: `ingest/eln/note.py:25` drops `yield_percent`, `purity_percent`,
`temperature_c` and `project` into Markdown text.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 9.1 | technician: raw data → first-pass summary | `FULL` | `list_attachments` / `read_attachment` (`src/chemclaw/agent/attachments.py:301,322`) parse csv/tsv/xlsx/docx/pdf/pptx to text+rows; pasted tables work directly; `gather_evidence` (`src/chemclaw/agent/research_tools.py:98`) + `expand_note` add cited context; judgment in `skills/deep-research/SKILL.md`. Summary is model prose over the text, not a computed statistic. | — |
| 9.2 | lab leader: cross-check new results vs historical trends / specs, flag anomalies | `PARTIAL` | Serves: the calculator-calibration half — `report_measurement` → `calculator_trust` / `calculator_outliers` (`src/chemclaw/connectors/calc/server/tools.py:109,407,449`), a real signed-residual ledger with uncertainty coverage, `solubility` and `pka` only. Missing: no numeric outcome field on `Note` (`src/chemclaw/kg/note.py:182`), no specification/acceptance-criterion entity anywhere, no statistics package in `src/chemclaw/science/`. | M (trend) / L (specs) |
| 9.3 | manager: plain-language summaries for cross-functional audiences | `PARTIAL` | Serves: any turn can be asked for it, over cited evidence, and `data/profiles/` is the seam that would narrow an agent for it. Missing: no lay/audience profile exists (`data/profiles/` holds one file, `property-lookup.yaml`), and `verifier_enabled` defaults False (`src/chemclaw/core/config.py:692`) so nothing scores an answer written for a reader who cannot check the citations. | S |

### Notes

**9.1 — what the "raw data" path actually is.** There is no run-table object and no ingestion of a
pasted table into the graph; the attachment store parses a file to text and rows
(`attachments.py:206-213` maps the six content types) and the model reads it. That is enough for a
first-pass summary and it is honest about being that. Corroborated by rp-01 (`partial`): both scale
groupings and the SB-045 outlier were read faithfully out of a pasted table with no invented
numbers — the downgrade was for asking an unnecessary clarifying question, not for a capability gap.

**9.2 — "historical trend" is prose, and that is the whole finding.** `OrdReaction` carries
`yield_percent`, `purity_percent`, `temperature_c`, `time_h`, `impurities[]`, `outcome_class`,
`performed_at` and `project` as typed fields (`src/chemclaw/ingest/eln/ord.py:145-186`).
`note_from_ord_reaction` (`src/chemclaw/ingest/eln/note.py:25-38`) carries exactly four of them
onto the note: `id`, `source`, `compound_smiles`, `valid_from`. Everything numeric is rendered into
the body by `_conditions_block` and `_impurity_block`. So a trend question has no series to read —
only free text an LLM re-parses per turn. The nearest real machinery is
`memory/optimization.py` + `memory/progression.py`, which lay one transformation's runs out in
performed order with what changed between consecutive runs; that is a genuine development history,
but it is a *rendered note* produced by a batch synthesis job, not a queryable series, and it says
nothing about whether today's number is in family. rp-07 (`fabricated`) is the exact failure this
predicts: one seed note was treated as a stored performance history and today's three runs were
declared "normal variation… no drift" with no series behind it. rp-08 (`served`) is the
counter-example and shows the calibration ledger working as designed.
*Cheapest honest improvement:* not the trend engine — say the limit. The agent prompt already
disclaims "stability, shelf-life or batch-trending data" (`src/chemclaw/agent/chemclaw_agent.py:158`);
what it does not say is that a *yield* series is equally unavailable, which is why rp-07 read one
note as a history. One clause, S.

**9.3 — the risk is unverified confidence, not vocabulary.** The model writes plain English on
request; nothing about the system prevents that. What is absent is the control that matters when
the reader cannot check a citation: `verify_turn_answer` runs on every turn
(`src/chemclaw/api/runner.py:571`) but with `verifier_enabled=False` it degrades to the
deterministic citation gate, which by construction only catches *fabricated* citations and treats an
uncited answer as clean (`src/chemclaw/agent/verifier.py:80-94`). A plain-language summary is
precisely the answer least likely to carry wikilinks. rp-04 (`fabricated`) corroborates: five
`expand_note` calls of real evidence, then confidence claims the notes did not license.

---

# §12 — Master Batch Record Drafting

**Section summary:** Nothing in this section that needs a *document* is served, and the reason is
one schema fact: `KNOWN_NOTE_TYPES` (`src/chemclaw/kg/note.py:118-134`) holds eleven types and none
of them is a controlled document — no `mbr`, no `batch-record`, no `procedure`, no `sop`. Templates
do not close it either: a template is a **tool sequence**, not a document
(`src/chemclaw/templates/manifest.py`, `src/chemclaw/templates/README.md`), and its output is an
agent step's answer text returned through `get_durable_job_status`. What *is* served, and served
well, is the governance half: the PR-gate marks agent-authored content as requiring human sign-off
and physically cannot be bypassed. The single biggest thing standing between this section and the
rest is the absence of a document entity with sections, status and per-section authorship — a
schema migration, not a model or a corpus.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 12.1 | lab leader: first-draft MBR from process description + history | `MISSING-ENTITY` | Absent: an `mbr`/`batch-record` note type in `KNOWN_NOTE_TYPES` (`kg/note.py:118`); a document/section schema (a template is a step list, `templates/manifest.py:56-101`); an equipment/instrument entity; a specification/acceptance-criterion entity; a batch/scale entity (`OrdReaction` has amounts, no batch id or campaign scale). Refusal already declared: `agent/chemclaw_agent.py:162`. | L |
| 12.2 | lab leader: flag inconsistencies between a draft MBR and the process data | `PARTIAL` | Serves: the draft can arrive as an attachment or paste; `gather_evidence`+`expand_note` retrieve the supporting notes and the model compares item by item with resolvable ids. Missing: any *mechanical* comparison — `kg/conflicts.py:8-12` deliberately has no property extractor, the parameters live as prose, and `verify_claims` never runs on a chat answer's arithmetic. | M |
| 12.3 | lab leader: update an MBR draft when the process changes | `MISSING-ENTITY` | Same absent document as 12.1. Note the update machinery already exists once a document does: stable ids + `note/<id>` branches update a file in place (`retrieval/harness.py:78-87`, `kg/pr_gate.py:136`), `memory/supersede.py` retires replaced notes via `valid_to`, `watch_for` (`agent/subscriptions.py:154`) notifies on new matching knowledge. | L |
| 12.4 | manager: how much was AI-drafted vs human-authored | `MISSING-ENTITY` | `Note.created_by` is whole-note and two-valued (`kg/note.py:224`). Nothing finer exists — no per-section, per-paragraph or per-claim authorship. `AuditEvent.purpose` is reserved and **unpopulated** by design (`agent/audit.py:75-81`; `audit_store.py:109` writes `""`). The trail reconstructs which tools ran in which turn, never which sentence a person wrote. | M (given 12.1) / L |
| 12.5 | manager: AI-assisted draft clearly marked as needing qualified review | `PARTIAL` | Serves: `propose_note` rejects non-agent notes and stamps every PR "Requires human review before merge — GxP: AI proposes, human signs off" (`kg/pr_gate.py:119,143`); every report goes through it (`durable/report_workflow.py:66`); the audit trail is append-only + hash-chained with `make audit-verify`. Missing: it governs *knowledge notes*, not controlled documents — no e-signature, no effective date, no document status lifecycle, no training record, no change control, no approver role beyond Git review. | M |

### Notes

**12.1 — the template question, answered.** A template is a *tool sequence*, not a document.
`data/templates/hazard-briefing.yaml` is the shipped worked example and its three steps are
`tool: screen_hazards` → `tool: similar_molecules` → `kind: agent` with a prompt; the manifest
allows exactly three step kinds (`ToolStep`, `JobStep`, `AgentStep`) and two substitution forms,
with "deliberately not a template language" stated in the module docstring
(`templates/manifest.py:1-20`). There is no document body, no section model, no field set, no
output artifact — a template's result is whatever the last step returned. So an MBR is **not**
expressible as a template, and 12.1 is `MISSING-ENTITY`, not `MISSING-TOOL`. Corroboration cuts
both ways and is worth reading carefully: rp-14 (`served`) declined cleanly and stated there is no
MBR template; rp-10 (`fabricated`) produced a full formatted MBR with 25 kg charge masses, an IPC
table with acceptance criteria and a QA sign-off block. Both ran against a system prompt that
already names "master batch record" in its does-not-hold list (`chemclaw_agent.py:162`, present at
run time — verified against commit `6862412`). *So the cheapest honest improvement here is not
prose: the declaration exists and was ignored.* It is an eval case — an MBR-request probe in
`data/evals/` that fails the build when a formatted batch record comes back. S.

**12.2 — real, but entirely un-mechanised.** rp-11 (`partial`) is the honest picture: the draft was
compared item by item against `report-biaryl-development` with real citations (1.5 mol% Pd, 80 °C,
12 h, 76%) and the unsupported 92% target was correctly challenged without inventing a replacement
— and it missed the sparge-time check against `playbook-degassing`, because nothing enumerates what
must be compared. `kg/conflicts.py` is the layer that would own this and explicitly refuses:
"There is no property extractor here, and there should not be… a false conflict is as damaging as
a missed one" (`kg/conflicts.py:8-12`). Its two signals — a declared `contradicts`/`supersedes`
relation, and two same-type/same-compound notes with materially different `confidence` — cannot see
a parameter range. **One cheap gap worth closing regardless:** the retrievers *do* compute
`EvidenceChunk.conflicts_with` (`retrieval/retrievers.py:136`, carried at `evidence.py:44`) and
`report_note` drops it — `harness.py:151` renders only `content`, `source_note_id` and `retriever`.
A report can therefore cite two notes the system knows disagree and show no marker. One line, S.

**12.3 — nothing to build until 12.1 exists, but one live trap.** `request_development_report` is
idempotent on title *and* section specs (`agent/durable_tools.py:84-94`,
`WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY`), so re-asking for the identical report after the
process changed returns the **old** job rather than re-drafting. The note id, by contrast, is keyed
on the title alone (`harness.py:78-87`), so a re-draft with a changed section set lands on the same
`note/<id>` branch and updates the file in place. Those two idempotency keys disagree, and the
combination means "refresh this report" is only reachable by editing a section query. Worth naming
now because it will be the update path for any document type added later.

**12.4 — finer-grained provenance does not exist anywhere, and the audit trail cannot substitute.**
`AuditEvent` records correlation id, session id, actor, tool, truncated arguments, outcome, a
truncated result detail, latency and deployment revision (`agent/audit.py:62-96`) — enough to say
"this actor ran `gather_evidence` with these arguments at this time", never enough to attribute a
paragraph. `purpose` is the field that would come closest and its own comment explains why it is
empty: authoring a reason per call means changing every tool signature, and deriving one from the
harness todo step would be a heuristic, so "a provenance field that is sometimes an inference is
worse than an empty one". That is a defensible decision and it means 12.4 has no partial answer.
rp-12 (`fabricated`) confirms the failure mode: it correctly offered no percentage, then claimed
note-level `created_by` "flows through" a document so it could show which sections were human vs
agent — it does not, and there is no document. *Cheapest honest improvement, and it is genuinely
cheap:* every `EvidenceChunk` already carries `created_by`, `source` and `confidence`
(`evidence.py:55-61`) and `report_note` discards all three. Rendering them per bullet gives a report
real per-item provenance — "which of this draft's evidence is agent-distilled" — without any new
entity. S, same line as the `conflicts_with` fix.

**12.5 — the mechanism is right, the scope is wrong, and the honest answer is a sentence.** The
PR-gate genuinely serves the *marking*: an agent note cannot enter the graph unreviewed, the
rejection of a non-agent note is a hard `ValueError`, and the PR body states the GxP line verbatim.
Both sides of the submit are recorded durably with actor, session and correlation id
(`kg/pr_gate.py:151-179`), so "what is awaiting review" and "what never reached review" are both
answerable. What it is not is a controlled-document control: `valid_from`/`valid_to` are
*epistemic* validity ("what did we know at time T", `kg/note.py:317`), not document effectivity;
there is no signature, no approver identity beyond a Git reviewer, no status field, no training
record, no QMS integration. Grading against the story — "assurance that a draft is clearly marked as
requiring qualified human review" — the marking is served and the assurance is not, because there
is no MBR to mark and no notion of *qualified*. rp-13 (`partial`) described the gate and the
hash-chained trail accurately (both real) and never stated the limit; that is the fix, S.

---

# §13 — Regulatory Document Input Preparation

**Section summary:** This is the system's strongest section and the report harness is the reason.
`request_development_report` produces a durable, resumable, section-by-section **evidence package**
in which every line is a retrieved chunk carrying its source wikilink, an unsupported section says
so in words, and a section whose retrieval *failed* is kept visibly distinct from one that
genuinely found nothing — a distinction most systems collapse. Traceability from any assembled
line back to a Git-versioned note, and from a note out to a stored calculation or artifact, is
complete and enforced at the type level. What is absent is everything that would make the package
*checkable*: no batch entity, no method entity, no units, no project or schedule. The single
biggest thing standing between this section and the rest is that "internally consistent" and "on
schedule" both need entities the schema does not have.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 13.1 | lab leader: pull together and structure data for a submission section | `FULL` | `request_development_report` (`agent/durable_tools.py:98`, authz-gated `agent/authz.py:70`) → `DevelopmentReportWorkflow` (`durable/report_workflow.py:102`) fans each `ReportSection` to a child workflow in request order; `gather_section` (`retrieval/harness.py:90`) queries every active source (`report_workflow.py:39-52`); `report_note` (`harness.py:132`) renders the draft as a PR-gated `report` note. Deployment requirement: Temporal must be running. | — |
| 13.2 | lab leader: check all expected elements present and internally consistent | `PARTIAL` | Serves: *presence* at section granularity — `_No supporting data found; section left unsupported._` (`harness.py:148`) and a distinct `_Retrieval failed… re-run required._` (`harness.py:145`), backed by `SynthesizedSection.retrieval_failed`/`supported` (`harness.py:44-61`). Missing: no batch entity (an `OrdReaction` has `reaction_id`/`provenance`, no batch number), no analytical-method entity or version field, no unit type on any value, no comparator — `kg/conflicts.py` refuses property extraction by design. | L |
| 13.3 | lab leader: structured factual summary of experiments / development history | `FULL` | Same harness as 13.1 — and the better fit, because the workflow synthesizes **no prose at all**: it emits one bullet per retrieved chunk. Also `memory/optimization.py` + `memory/progression.py` render one transformation's runs in performed order with what each run changed relative to its predecessor, as an `optimization-campaign` note. | — |
| 13.4 | manager: are input packages complete and on schedule | `MISSING-ENTITY` | No project/programme/package entity. `project` exists on `OrdReaction` (`ingest/eln/ord.py:180`) as free text and reaches the graph only as `tags` on a `campaign` note (`memory/campaign.py:55`); `note_from_ord_reaction` sets no tags at all. No status, owner, dates-as-schedule or package definition; nothing in `core/config.py`. | L |
| 13.5 | manager: assembled data stays traceable to source records, audit-defensible | `FULL` | `EvidenceChunk.source_note_id` is `min_length=1` (`retrieval/evidence.py:20`) — the harness structurally cannot hold an uncited fact; every report bullet renders `[[id]]` + retriever (`harness.py:151`); `find_notes`/`expand_note` resolve an id to the record; `Note.calc_refs`/`artifact_refs` are shape-validated pointers into the calculation store (`kg/note.py:235-265`) fetched by `list_artifacts`/`fetch_artifact`; proposals recorded on both success and failure (`kg/pr_gate.py:161-179`); append-only hash-chained audit with `make audit-verify` and `AuditEvent.revision`. | — |

### Notes

**13.1/13.3 — exactly what the harness guarantees, and exactly what it does not.** The guarantees
are worth stating precisely because they are unusual:

- *Every line is a citation.* `gather_section` returns `EvidenceChunk`s and nothing else; the chunk
  type requires a non-empty `source_note_id`. There is no code path by which a fact without a
  source note enters a report.
- *An unsupported section is labelled, never filled.* `harness.py:147-149` — `supported` is
  `not retrieval_failed and bool(evidence)`, and a section that fails it renders
  `_No supporting data found; section left unsupported._`
- *A failed section is not an empty one.* `ReportSectionWorkflow` catches `ActivityError` after the
  activity's own retry budget and degrades to `retrieval_failed=True`
  (`durable/report_workflow.py:82-99`), which `report_note` renders as an explicit re-run marker.
  Every requested section appears in the draft in request order, so a failure is shown rather than
  silently missing.
- *The draft is a proposal.* It opens as an agent-authored `report` note through the PR-gate.
- *Retrieval is the same sweep a chat turn gets.* `default_retrievers()` reads the config-driven
  registry, so a deployment that enables hybrid retrieval does not have to remember to do it twice
  (`report_workflow.py:39-52`).

And what it is not: **the workflow writes no prose.** `verify_claims` (`harness.py:107`) — the
adversarial claim gate the module docstring and `skills/development-report/SKILL.md` both describe
— has exactly one production caller, `agent/verifier.py:95`, on the *conversational* answer path.
No report code path calls it, because there is nothing for it to filter. So a "development report"
is an evidence package: a structured, cited, gap-marked assembly of retrieved material that a human
turns into narrative. For 13.1 and 13.3 that is the right shape and is why they are `FULL`; anyone
expecting a written section will be surprised. Corroboration: rp-03 and rp-16 both reached
`request_development_report` and both got a loud failure (Temporal down) — the wiring is exercised;
what those probes actually graded is the hand-written substitute the model then produced, which in
both cases inserted numbers no note contains. That is an argument *for* the harness, not against it.
rp-17 (`served`) is the in-turn version working: every number carried a resolvable note id and every
one checked out against the corpus.
One caveat on 13.3: "a *method's* development history" has no method entity behind it — this is a
reaction/transformation history.

**13.2 — presence is mechanical, consistency is not.** The completeness half is genuinely served
and at a useful granularity: a requester who names the sections a submission needs gets back, per
section, "evidenced / nothing found / retrieval broke". That is a real checklist. The consistency
half has nothing under it. Batch numbers: `OrdReaction` has no batch field. Method versions: no
method note type, no method store, no version field. Units: every quantity in a note body is text.
`kg/conflicts.py` will not extract them, on purpose. rp-20 (`fabricated`) is the precise shape of
the failure — it found the planted 90 min / 1.5 h unit mismatch by reading, then declared method
versions "consistently versioned" and batch numbers "tracked consistently" against no register at
all. The reading-based check is real and useful; the *verdict* it then issues is not backed.
*Cheapest honest improvement:* render `conflicts_with` into `report_note` (S, shared with 12.2/12.4)
so the one consistency signal the system does compute reaches the document.

**13.4 — a "project" is a string on some notes, and mostly not even that.** Three facts, each
checkable: (1) `OrdReaction.project` is `str | None`, free text, with no registry
(`ingest/eln/ord.py:180`); (2) it reaches a note only through `memory/campaign.py:55`
(`tags=sorted(projects)`) — `note_from_ord_reaction` passes no `tags`, so every ELN-derived
`reaction` note in the graph is untagged and `gather_evidence(tag=…)` cannot reach it; (3)
`kg/analytics.py::_undistilled_projects` therefore computes "projects" as a set difference over
note tags, i.e. over whichever notes happened to be tagged. There is no status, no owner, no
target date, no package definition and no completeness criterion, and `core/config.py` contains no
project setting. Making a project first-class touches: the `Note` schema (a field or an enforced
tag namespace), `ingest/eln/note.py`, a re-ingest of the corpus, the retrieval filter, `kg-validate`,
and `kg/analytics.py` — a schema migration across the corpus, hence L. rp-22 (`partial`)
corroborates the *shape* of the gap well: it refused to invent a percentage or a filing date, then
framed the absence as a missing note rather than a missing concept.
*Cheapest honest improvement:* make the refusal correct. The prompt already disclaims project and
timeline data (`chemclaw_agent.py:163-164`); what it does not say is that an empty search is not
evidence of absence in this domain — which is exactly the elision rp-22 made. S.

**13.5 — where the chain genuinely stops.** Traceability inside the system is complete and enforced
by types rather than convention, which is the strong claim here. Two boundaries should be stated
rather than papered over: (a) it stops at the note — a `reaction` note's `source` is the ELN
provenance string, and there is no link from a note to an instrument file, a chromatogram or a raw
CoA, because no such store exists; (b) `AuditEvent.purpose` is empty, so the trail records *what*
ran and not *why* (a durable job's rationale is the exception — `find_past_jobs` searches it).
rp-21 (`partial`) is instructive: the model neither demonstrated the note-level click-through it
genuinely has on a real retrieved id, nor said where the chain stops. Both are one-sentence
behaviours over machinery that already works.

---

# §14 — Tech Transfer & Cross-Functional Collaboration

**Section summary:** The chemistry half of a transfer is assemblable today — the report harness will
produce a cited package of what was run, what failed, and which playbooks generalise, and the
cross-project retrieval surface (`similar_reactions`, `similar_molecules`, `playbook` notes,
`recall_observations`) is built for "how did another programme handle this". Everything that makes
it a *transfer* is absent: no transfer-package concept, no receiving-site entity, no method entity,
no scale or equipment data, no risk register, no status. The single biggest thing standing between
this section and the rest is the same missing project entity as §13.4, plus a cheap adjacent
defect: ELN-derived reaction notes carry no tags, so the cross-project filter that would answer
14.2 cannot reach the 987 notes it is supposed to search.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 14.1 | lab leader: assemble a transfer summary (chemistry, methods, risks) for a receiving site | `PARTIAL` | Serves: the chemistry + known-risk half — `request_development_report` with sections over `reaction`/`failure-mode`/`playbook` notes, plus `screen_hazards` (16 SMARTS process-safety rules) and the `failure-mode` note type (`kg/note.py:132`). Missing: no transfer-package entity or defined content set, no receiving-site entity, no method entity, no scale/equipment data, no acceptance criteria — so "everything they need" is undefinable and uncheckable. | M |
| 14.2 | lab leader: how did a related programme handle a similar scale-up/transfer challenge | `PARTIAL` | Serves: `similar_reactions` (DRFP), `similar_molecules`, `substructure_matches`, `gather_evidence` with `note_type`/`tag`/date filters, `playbook` notes (distilled only across ≥2 projects, `memory/playbook.py:67`), `recall_observations` with `projects_seen`. Missing: `project` never reaches a `reaction` note (`ingest/eln/note.py:25` sets no tags), so "a related programme" is only reachable through campaign/playbook notes; and no scale, vessel or equipment data exists at all, so a *scale-up* challenge is findable only if someone wrote prose about it. | S (tag) / L (scale) |
| 14.3 | manager: consolidated view of open questions/risks across ongoing transfers | `MISSING-ENTITY` | Absent: a transfer entity, a project entity with status and dates, a risk/issue register, an owner. `find_knowledge_gaps` (`agent/graph_tools.py:169` → `kg/analytics.py:47`) reports graph structure — isolated notes, type counts, tags with evidence but no distillation, hubs, dangling links — which is adjacent enough to be mistaken for this and is not it. | L |

### Notes

**14.1 — a cheap, real improvement exists and is worth naming.** A template's `tool` step can call
*any* tool on the agent surface, resolved through the same `build_agent` + `connector_tools` path a
chat turn uses (`durable/template_activities.py:226-243`), and `request_development_report` is an
ordinary in-process registry tool. So a **fixed transfer-package section set is expressible as a
template today** — `data/templates/transfer-package.yaml` with one `tool` step naming the sections
(chemistry, conditions and their evidence, failure modes, hazards, open questions). That does not
create a transfer-package *entity* and must not be described as one — there is still no completeness
check, no method or equipment content, and no definition of "done" — but it turns "assemble the
chemistry half" from an ad-hoc prompt into a reproducible, auditable, PR-gated artifact. S, and it
is the highest value-per-effort item in this section. rp-23 (`fabricated`) shows what happens
without it: a document headed "AMIDE COUPLING TRANSFER PACKAGE" presented as a finished deliverable,
with reagent equivalents in no note and a purity acceptance target the system has no concept of.

**14.2 — the untagged-corpus defect.** `gather_evidence` advertises a `tag` filter and documents it
as "e.g. a project name" (`agent/research_tools.py:117`). `note_from_ord_reaction`
(`ingest/eln/note.py:25-38`) passes no `tags`, and it is the builder every ELN-derived reaction note
goes through — including the ~987 in the live corpus, loaded via that same function. `project` is
right there on the `OrdReaction` being mapped. So the one filter that makes "how did a related
programme handle this" answerable is inert on the largest note class in the graph. This is one line
plus a re-ingest, it makes an already-built retrieval path work, and it is a precondition for any
project-scoped question in §13.4 and §14.3. Corroborated indirectly by rp-24 (`partial`), which
answered the toluene/dioxane site-change question from retrieved notes but by text match, not by
programme. Separately: "scale-up" is not answerable at all — there is no batch size, vessel,
charge-rate or equipment field anywhere, and the prompt correctly disclaims heat/mass-transfer
models (`chemclaw_agent.py:160-162`).

**14.3 — the specific conflation to guard against.** `find_knowledge_gaps` returns
`projects_without_distillation`, which reads like a portfolio status report and is a set difference
over note tags. rp-26 and rp-27 (both `partial`) each ran it and each presented its output as
transfer-readiness — "What a Receiving Lab Will Ask For", "open risks across active transfers" —
without stating that the system holds no transfer register. The counts themselves were real and
verified against the corpus; the framing was the defect. rp-28 (`served`) is the model of the right
behaviour: a direct refusal naming staffing, instrument availability and calendar as out of reach,
with the redirection kept inside real capability. *Cheapest honest improvement:* tighten the
`find_knowledge_gaps` docstring so it says what it measures (graph structure over tags) and what it
does not (project status, ownership, schedule, risk) — the tool description is the only thing the
model reads before deciding what the output means. S.

---

## Verdict counts

| verdict | count | stories |
|---|---:|---|
| `FULL` | 4 | 9.1, 13.1, 13.3, 13.5 |
| `PARTIAL` | 7 | 9.2, 9.3, 12.2, 12.5, 13.2, 14.1, 14.2 |
| `MISSING-ENTITY` | 5 | 12.1, 12.3, 12.4, 13.4, 14.3 |
| `MISSING-TOOL` / `MISSING-DATA` / `MISSING-MODEL` / `OUT-OF-SCOPE` | 0 | — |

16 stories. Note that 12.1 and 12.3 are two rows over **one** absent entity (the controlled
document), so the five `MISSING-ENTITY` rows are four distinct missing concepts: a document, a
per-section authorship model, a project/package, and a transfer/risk register.

The distribution is itself the finding: **not one story in these four sections fails for want of a
model, a dataset or a tool wrapper.** Every gap is a first-class concept the schema lacks — a
document, a batch, a method, a specification, a project. That is a schema-migration roadmap, not a
research or procurement one.

## The three highest-value gaps, by value per effort

1. **Render what the evidence already carries into the report draft (S).** Every `EvidenceChunk`
   holds `conflicts_with`, `created_by`, `source` and `confidence` (`retrieval/evidence.py:39-61`),
   all populated by the retrievers, and `report_note` (`retrieval/harness.py:151`) throws all four
   away. One line of rendering gives the regulatory package per-item provenance and surfaces the one
   consistency signal the system does compute. It moves 12.4 off zero, strengthens 13.2 and 13.5,
   and costs an afternoon.
2. **Tag ELN reaction notes with their project (S).** `OrdReaction.project` is dropped at
   `ingest/eln/note.py:25`, leaving the largest note class untagged and `gather_evidence(tag=…)`
   inert on it. One line plus a re-ingest activates an already-built cross-project retrieval path —
   the precondition for 14.2, and the first brick of the project entity 13.4 and 14.3 need.
3. **A `transfer-package` step template (S).** `data/templates/transfer-package.yaml` whose single
   `tool` step calls `request_development_report` with a fixed section list turns the strongest
   machinery in the system onto the weakest section, reproducibly and PR-gated. It converts 14.1's
   chemistry half from an ad-hoc prompt into an auditable artifact without inventing an entity.

Runners-up worth logging but not ranked here, because each is a phase rather than a fix: the
document entity behind 12.1–12.4 (L), and the project entity behind 13.4/14.3 (L). For both, the
cheapest *honest* action today is to make the refusal correct — the declarations exist in
`agent/chemclaw_agent.py:154-175` and were live during the run; what is missing is an eval case that
fails the build when a formatted MBR or a transfer package comes back anyway (rp-10, rp-23).
