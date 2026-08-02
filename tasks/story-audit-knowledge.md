# §1 — Institutional Knowledge & Search

**Section summary:** The retrieval spine is real and reaches the whole corpus: `gather_evidence`
fans one natural-language question across every enabled source, every chunk carries the note id
that produced it, and `expand_note` walks from a hit into the graph — so "how was this run before"
and "what molecule/chemistry does the record hold" are served end to end over 1,025 notes. What the
section cannot serve is anything indexed **by a person or by a project**. `Note.created_by` is
`Literal["human", "agent"]` (`src/chemclaw/kg/note.py:224`) — it records machine-vs-human, not
authorship — and the only place a chemist's name survives is inside the free-text `Note.source`
(`eln-json:<entry>:<operator>`, `src/chemclaw/ingest/sources/../eln/json_adapter.py:368`), which no
retriever indexes: `note_text` is `id + tags + body`
(`src/chemclaw/retrieval/vector_index.py:56`). The single biggest thing standing between this
section and the rest is a **person entity** — a first-class author/owner on the note schema, its
ingest mapping and a retrieval filter — with a **project entity** a close second, because "project"
today is a free-text tag and the ELN's `project` field is dropped entirely on note creation.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 1.1 | technician: ask in natural language how a similar experiment/analysis was run before | `FULL` | `gather_evidence` (`agent/research_tools.py:98`) over `GraphRetriever` + `FingerprintReactionRetriever` (`retrieval/retrievers.py:148`, `:217`), then `find_notes` (`agent/graph_tools.py:73`) / `expand_note` (`:122`); judgment in `skills/deep-research/SKILL.md` + `skills/knowledge-graph-query/SKILL.md`. Reaction notes carry the verbatim recipe (`ingest/eln/note.py:16` `_procedure_block`). | — |
| 1.2 | technician: surface known pitfalls, safety notes, lessons learned for a reaction/reagent/method | `PARTIAL` | Serves: `failure-mode` + `playbook` note types (`kg/note.py:118`) retrievable by `note_type` filter; `conflicts_with` stamped on every chunk (`retrieval/retrievers.py:311`); `screen_hazards` (16 SMARTS process-safety rules). Missing: nothing agent-reachable **mints** a failure record — `memory/failure.py:29` `failure_note` has zero production callers; and "method" is not an entity. | `S` |
| 1.3 | lab leader: query a project's or molecule's collective history across chemists and time | `PARTIAL` | Molecule + time halves served: `compound_smiles`, DRFP/ECFP search, `since`/`until` windowing on `valid_from` (`retrieval/retrievers.py:108`), `memory/progression.py` run ordering. Missing: "across chemists" has no field (see 1.4); "project" is a free-text tag, and `ingest/eln/note.py:16` sets **no tags at all**, so `OrdReaction.project` never reaches a note. | `M` (project) |
| 1.4 | lab leader: be pointed to colleagues or past projects with relevant expertise | `MISSING-ENTITY` | No person node, no `author`/`owner`/`expertise` field. `created_by` is `human`\|`agent` only (`kg/note.py:224`). Names exist only inside `Note.source` free text and are unindexed (`retrieval/vector_index.py:56`). The "past projects" half is served by 1.1's machinery via `campaign`/`optimization-campaign` notes. | `L` |
| 1.5 | manager: assurance that departing staff's expertise and rationale stays searchable | `PARTIAL` | Serves: rationale is captured in several durable places — `OrdReaction.hypothesis` (`ingest/eln/ord.py:188`), `failure_reason` (`:165`), verbatim `procedure_text` (`:193`), the mandatory job `rationale` searchable via `find_past_jobs` (`agent/durable_tools.py:224`), `interaction` notes from `record_confirmed_answer` (`agent/memory_tools.py:24`), and Git permanence + `valid_to` retirement instead of deletion. Missing: none of it is attributable, so "what did *this person* know" is not a queryable set. | `L` |

### Notes

**1.2 — the refutation path is built and unwired.** `memory/failure.py:29` builds exactly the note
this story needs: a `failure-mode` note with a `contradicts` relation to what it refutes, a
`reported_by` provenance line and a `valid_from` so a correction is not retroactively current. Grep
finds its only callers in `tests/test_conflicts.py`. There is no `report_failure` tool in the
inventory, so the agent's only route is to hand-roll the note through `propose_knowledge_note` with
`relations=[Relation(rel="contradicts", …)]` — which works, and which nothing tells it to do.
Cheapest honest improvement: a ~30-line `@tool` wrapper around `failure_note` + `propose_note`,
gated by `DEFAULT_WRITE_TOOL_GATES` like the other two write tools.

**1.3 — the dropped project is a measurable defect, not a design gap.** `note_from_ord_reaction`
(`ingest/eln/note.py:16`) constructs the `reaction` note with `id/type/created_by/source/
compound_smiles/valid_from/body` and no `tags`. Measured on the shipped corpus: 6 of 993 reaction
notes carry any tag, and all six are hand-authored seeds. So `gather_evidence(tag="<project>")`
cannot reach a single ingested run. Fixing it is one line (`tags=[reaction.project] if
reaction.project else []`) and it also unblocks half of 15.3.

**1.4 — the honest state is worse than "absent", because the names are there.** The live run found
both models failing on this in opposite directions from the same undocumented fact (probe `kn-10`:
one model asserted authorship "isn't captured in the graph", the other named two chemists and
suggested pinging them). On the corpus currently on disk, 31 ELN-derived notes carry a person in
`source` (`eln-json:liu-orgsyn-procedure-1:Richard Y. Liu`), and `NoteRef.source` surfaces it to
the model (`agent/graph_tools.py:64`). It is a provenance string, not a directory: `find_notes` and
`GraphRetriever` both search `id + tags + body` and never `source` — measured, `GraphRetriever`
returns 0 hits for `"Merck"` although `source` contains it. The cheapest honest improvement here is
**not** to build the directory: it is one `_INSTRUCTIONS` sentence saying an ELN `source` may name
the chemist who logged a run, that this is provenance and not an expertise index, and that it may
be reported as "this run is logged under X" and never as "X is who to ask".

**1.5 — the risk is capture, not storage.** Everything merged is permanent and time-scoped. What
decays is what never gets written: the memory jobs distil only from *merged* reaction notes, and
`record_confirmed_answer` fires only when a chemist explicitly confirms. There is no prompt,
schedule or exit-interview path that harvests a departing person's open knowledge, and without a
person field there is no way to even list what is at risk.

---

# §15 — Training, Onboarding & Knowledge Continuity

**Section summary:** The "ask how we do X" half of this section is genuinely served — the playbook
note type plus the verbatim procedure text in every reaction note plus `gather_evidence` is a
working internal-practice Q&A, and the live run served it repeatedly. The "capture and measure"
half is where it stops. A lab leader can turn judgment into a **note** (through the PR-gate) but
never into a **skill**, because layer 3 is `SKILL.md` files on disk with no authoring path at all.
And `find_knowledge_gaps` — the one tool aimed squarely at 15.3 — reports gaps over *tags*, not
over projects or people: measured on the shipped corpus its `projects_without_distillation` returns
27 entries, every one of them a topic tag (`analysis`, `base`, `computed`, `geometry`, `pka`). The
single biggest gap is the same person entity §1 needs, because "dependent on a single person" is
un-askable without it.

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 15.1 | technician: interactively ask "how do I…" about established procedures, from internal practice | `FULL` | `gather_evidence(note_type="playbook")` (`agent/research_tools.py:98`) + `expand_note`; `playbook` notes are the distilled house rule and `reaction` note bodies carry the ordered recipe verbatim (`ingest/eln/note.py:16`); judgment in `skills/deep-research` + `skills/knowledge-graph-query`. Corpus-dependent (5 playbooks shipped), not capability-dependent. | — |
| 15.2 | lab leader: turn my own experience and judgment calls into reusable guidance | `PARTIAL` | Serves: `propose_knowledge_note` (`agent/graph_tools.py:188`) for a `playbook`, `record_confirmed_answer` (`agent/memory_tools.py:24`) for an `interaction`, both through `kg/pr_gate.py:88`; plus the automatic `distill_playbooks` job (`memory/jobs.py`) and `skills/playbook-distillation`. Missing: no path to author a **Skill** — `SKILL.md` files are repo artifacts; `agent/skill_manifest.py` validates, nothing creates. | `M` |
| 15.3 | manager: see which areas of knowledge are thin or dependent on a single person | `PARTIAL` | Serves (thin half, partially): `find_knowledge_gaps` (`agent/graph_tools.py:169` → `kg/analytics.py:46`) gives type counts, isolated notes, hubs, dangling links. Missing: `_undistilled_projects` (`kg/analytics.py:72`) diffs `note.tags`, not projects — so it names topic tags; and the single-person half has no person entity to count at all. | `L` |

### Notes

**15.2 — the note/skill line is the whole gap.** The architecture's own split is that skills hold
*judgment* and notes hold *data* (`ARCHITECTURE.md`, layers 3 and 4). Every write path built —
PR-gate, `propose_knowledge_note`, `record_confirmed_answer`, the distillation jobs — writes into
layer 4. A lab leader's "here is how I decide whether to escalate to DFT" therefore lands as a
`playbook` note that retrieval may or may not surface, rather than as a skill the agent loads on
demand. The mechanism to close this already exists and is unused: `NoteSubmission.files`
(`kg/pr_gate.py:39`) is a multi-file reviewable unit, so a `propose_skill` tool writing
`skills/<name>/SKILL.md` on a `skill/<name>` branch would reuse the same gate and the same
reviewer. `make skill-validate` is already the CI check that would guard it.

**15.3 — measured, so the diagnosis is not a reading of the docstring.** Running
`analyze(build_graph(...), load_notes(...))` over the shipped corpus:
`total_notes=1025`, `type_counts={reaction: 993, compound: 9, playbook: 5, campaign: 3,
interaction: 3, job-result: 3, optimization-campaign: 2, bo-candidate: 2, failure-mode: 2,
report: 2, experiment-proposal: 1}`, `projects_without_distillation` = 27 entries, all topic tags,
`isolated_note_ids` = 994. Two causes compound: `_DISTILLED_TYPES` includes four types
(`kg/analytics.py:43`) but **only `campaign` ever sets a project tag** (`memory/campaign.py:55`) —
`optimization-campaign`, `playbook` and `report` notes carry no tags — and the evidence side is
tagged only on hand-authored seeds, since ELN ingest sets none (see 1.3). Fix the ingest tag drop
and tag the three distilling types, and the same function starts answering the real question with
no new machinery. The "single person" half needs the §1 person entity and a per-author bus-factor
count on top of it; nothing today counts authorship of anything.

**Corroboration:** live probe `kn-08` called the right tool and got the right structure back — the
27, the 993 and the hub counts were all genuinely returned — which is exactly the trap: the
number is real and the label ("projects") is not.

---

# §17 — Cross-Cutting Trust & Governance

**Section summary:** This section has the most machinery and the widest gap between what is *built*
and what is *enforced on the default path*. The append-only hash-chained audit trail, the signed
tail anchor, the PR-gate with an HTTP-only decision route, the derived side-effecting-tool
partition and the plan-approval gate are all real, tested and durable. But three of the five
controls a user would recognise are **off or advisory by default**: `verifier_enabled=False`
(`core/config.py:692`) means nothing checks an answer's citations on the chat path,
`harness_enabled=False` (`:826`) means the plan gate is not installed, and the RBAC gate is scoped
to **tool and skill names, never to data** — one shared corpus, by an explicit deferral. The
single biggest thing standing between this section and the rest is not a build: it is that the
system does not *state* its own boundary, so the model invents a stronger one (live probe `kn-26`,
both models).

| # | Story (abbreviated, persona) | Verdict | What serves it / what is missing | Size |
|---|---|---|---|---|
| 17.1 | leader/technician: every answer shows where it came from, so it can be verified | `PARTIAL` | Serves: every `EvidenceChunk` carries `source_note_id` + `created_by` + `source` + `confidence` + `conflicts_with` (`retrieval/retrievers.py:311`); `expand_note` resolves any cited id; the report path **gates** citations (`retrieval/harness.py:107` `verify_claims`, run by the report workflow). Missing: on the chat path that gate is reached only via `agent/verifier.py:62`, only when `verifier_enabled` — default `False`; and even on, an answer with **no** citations scores 1.0 (`verifier.py:80`). Citing is prompted (`_INSTRUCTIONS` "Discipline"), not required. | `S` |
| 17.2 | lab leader: a clear, consistent boundary between autonomous and sign-off work | `PARTIAL` | Serves: PR-gate for every knowledge write (`kg/pr_gate.py:88`), decision deliberately an HTTP route not a tool (`api/app.py:1504`); `side_effecting_tools()` (`agent/authz.py:112`), a derived read/write partition `tests/test_authz.py` holds against the live registry; `enforce_plan_approval` (`agent/plan_gate.py:96`) under `harness_autonomy="plan_only"`; `authorize_trigger` for `expensive: true` jobs; dry-run (`agent/dialogue_tools.py`). Missing: `harness_enabled=False` by default, so the plan gate ships uninstalled; and nothing states the boundary to a user — the model must describe it. | `S` |
| 17.3 | manager: an audit trail of AI-assisted work — what was asked, what data used, what generated | `PARTIAL` | Serves: `audit_events` hash-chained + append-only (`agent/audit.py:155`, `agent/audit_store.py`, `infra/sql/006`+`011`+`026`), signed tail anchor (`agent/audit_anchor.py`, `infra/sql/032`), `make audit-verify`; `session_messages`/`session_events`; `job_records` with a mandatory rationale (`durable/job_record.py`); `note_proposals` with session, correlation, `decided_by`, `reason` (`infra/sql/027`); `turn_costs`. Missing: `AuditEvent.purpose` is declared and deliberately never populated (`agent/audit.py:81`); no per-answer evidence manifest; **no read surface** — no `/audit` route, no tool. | `S` |
| 17.4 | manager: role-based access so the assistant only surfaces authorised data | `PARTIAL` | Serves: `authorize_tool` (`agent/authz.py:196`) gates **tool names** against Entra roles via `tool_role_gates` + `tool_authz_default` + `DEFAULT_WRITE_TOOL_GATES`; `RoleScopedSkillsSource` (`agent/skill_access.py:64`) hides gated **skills**; `_is_reviewer` scopes the proposal queue (`api/app.py:1418`); session ownership scopes `/sessions`. Missing: **no data-level scoping anywhere** — `_eligible_notes` (`retrieval/retrievers.py:74`) filters on caller-supplied `type`/`tag`/`since`/`until` and never on identity; `search_job_records` (`durable/job_record.py:146`) takes no actor. | `L` |
| 17.5 | lab leader: correct or refine what the assistant "knows" when it is wrong or outdated | `PARTIAL` | Serves (the answer): `record_confirmed_answer` (`agent/memory_tools.py:24`) → PR-gated `interaction` note. Serves (partly, the knowledge): `propose_knowledge_note` accepts `relations` incl. `supersedes`/`contradicts` (`kg/relations.py`) and `valid_from`/`valid_to` — on the **new** note only. Missing: nothing agent-reachable retires the **old** note; `memory/supersede.py:36` does exactly that but is called only from the durable synthesis job (`memory/jobs.py:81`) and only for notes that synthesis itself minted. | `S` |

### Notes

**17.1 — "shows its sources" vs "is required to".** These are different systems here and the
difference is one config flag. `verify_claims` (`retrieval/harness.py:107`) is a genuine gate: a
claim survives only if it cites at least one note and *every* cited note was actually retrieved.
The **report** workflow runs it. The **chat** path reaches it only through
`agent/verifier.py:62 _deterministic_result`, which `api/runner.py:557 _answer_event` calls only
when `settings.verifier_enabled` — `False` in `core/config.py:692`. So by default an answer is
streamed unscored. Two further subtleties worth naming before anyone flips the flag: (a) the
deterministic path treats an answer carrying **no** citations as supported with confidence 1.0, by
design — it catches *fabricated* citations, never *missing* ones; (b) `verify_turn_answer`
re-resolves cited ids against the note tree rather than against the turn's actual retrieved chunks,
so "the model retrieved this note" and "the model recalled this id" are indistinguishable to the
gate. Cheapest honest improvement is still the flag plus a `review_required` render, since it is
the only mechanism in the tree that would catch an unfaithful-but-cited claim (corroborated:
`kn-18`, four real note ids used as sources for content none of them contains).

**17.2 — the boundary is well-drawn in code and undocumented to the user.** The design here is
better than most of this audit: the write/read partition is *derived* (connector manifests +
template launchers + a classified in-process set) rather than hand-listed, and
`tests/test_authz.py` fails if a new tool is added without classifying it — so the boundary cannot
silently drift. What is absent is any artifact a lab leader can read. The live run's `kn-24` served
this correctly (the model drew the `remember_preference`/`report_measurement` vs
`propose_knowledge_note`/`record_confirmed_answer` line accurately), but that is the model
reasoning from tool docstrings, not the system stating a policy. Cheapest improvement: a
`GET /policy` route rendering `side_effecting_tools()`, `expensive_actions()` and the current
`harness_autonomy` — the data is already computed at startup.

**17.3 — the record exists; the ability to *read* it does not.** Everything a manager would want
is being written durably, and the chain is verifiable (`kn-23` and `kn-27` both served, and the
model's self-description of the hashed field set checks out against `_V1_FIELDS`). The gap is
purely surface: verification is `make audit-verify` on a shell, and inspection is SQL. Note also
that `default_audit_sink()` is gated on `session_store == "postgres"` (`agent/audit.py:136`) — a
deployment without it is log-only, which is the correct fallback but means the compliance record's
existence is a deployment property. Two things a manager would ask that the schema cannot answer:
*why* a tool was called (`purpose` is intentionally empty rather than heuristically filled), and
*which evidence chunks* an answer was built from (only truncated `arguments`/`detail` blobs,
bounded by `agent_audit_max_arg_chars`).

**17.4 — precisely what the gate is scoped to.** Tool names and skill names. `authorize_tool` takes
a single `tool: str` and consults role sets; there is no note, tag, project or team dimension in
`agent/authz.py` at all. On the data side, `_eligible_notes` accepts `type`/`tag`/`since`/`until`
— all of which arrive as the **model's own tool arguments**, so they narrow a query and cannot
enforce anything — plus the currency check. The knowledge graph is one shared corpus and this is a
recorded decision, not an oversight: `docs/planning/DEFERRED.md` carries *"Postgres RLS mirror of
the graph (KM-9) — broad internal read access is fine for cross-project learning"*, triggered on
"real, combinatorial project-level confidentiality requirements". So the verdict is `PARTIAL`
against the story and the missing piece is `MISSING-ENTITY`: a classification/owning-team field on
`Note` plus an identity-derived predicate in `_eligible_notes` and an actor argument on
`search_job_records`. Corroboration `kn-26` (both models, so not a model failure): the system
described *"the system checks your account's authorization against the note's tags before returning
anything"* — a per-team read boundary that does not exist. **The cheapest honest improvement is to
make the description correct, not to build the boundary**: one `_INSTRUCTIONS` sentence stating
that authorization is per-tool and per-skill role gating on the signed identity, and that any note
or job record in this deployment is retrievable by anyone who can search.

**17.5 — correcting the answer and correcting the knowledge are two different capabilities, and
only the first is clean.** In-conversation correction works: `record_confirmed_answer` captures the
chemist's corrected wording as an `interaction` note through the ordinary gate, and once merged it
re-enters retrieval (`kn-25` served — the model re-read the note, reported exactly what was there,
and did not agree reflexively). Correcting the *knowledge* is where it thins. The relation
vocabulary is right (`supersedes`, `superseded-by`, `contradicts` in `kg/relations.py`) and
`kg/conflicts.py` reads `contradicts` so that a chunk from a disputed note arrives stamped with
`conflicts_with` — which means a wrong note that has been contradicted is at least *visibly*
disputed rather than silently served. But nothing agent-reachable sets `valid_to` on the note being
retired, so the wrong note stays **current** in every discovery sweep. There is one accidental
route and it is a trap worth naming: `propose_note` (`kg/pr_gate.py:88`) writes
`knowledge/<type>/<id>.md` at whatever id it is given and forces `created_by: agent`, so an agent
re-proposing an existing id would overwrite a human-authored note *and flip its provenance* — the
gate would catch it at review, but the tool does not prevent authoring it. The clean fix is small
and reuses what exists: a `retire_note`/`report_failure` tool that builds `failure_note`
(`memory/failure.py:29`) **and** the amended predecessor with `valid_to` set, and submits both as
one `NoteSubmission` — the multi-file reviewable unit is already there (`kg/pr_gate.py:39`).
