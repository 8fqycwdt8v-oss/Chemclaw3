# REVIEW 2026-09-13 — Capability audit against the assistant brief, and the plan it asks for

A six-axis audit of whether this family — `Chemclaw3`, `Chemclaw3-mcp`, `Chemclaw3_ui` — is the
assistant its brief describes: an agentic system for data and tool work, as powerful as current
technology allows, serving chemical-development and analytical-development scientists first but
generic enough for any scientist given the right data sources and tools; able to *produce* results
rather than only answer — propose experiments, write reports, surface patterns and relationships —
on a knowledge store good enough to propose from past learnings; and agentically as capable as
Claude Code is for code, with subagent involvement, context management, agent teams and deep
research.

**The verdict is no, and the reason is not where the backlog was looking.** Two of the largest gaps
are merged code that nothing reaches, one is a merged *decision* that forbids the behaviour the
brief asks for, and the two scientific domains named in the brief are served at roughly half and
almost not at all.

This document is a point-in-time record. What it *asks for* is in
[`docs/planning/BACKLOG.md`](../planning/BACKLOG.md), per the rule
[`docs/README.md`](../README.md) states for the split.

---

## Method

Six parallel read-only audits against `HEAD` at `0bf7ff3`, each answering one axis by reading code
rather than documentation, and each instructed to report where the code contradicts `CLAUDE.md`.
Ground facts were re-derived directly: 457 Python modules and 148,469 lines under `src/`, 367 test
modules, 573 decision records, 31 `SKILL.md` files, 40 knowledge notes in the shipped corpus, six
agent profiles, seven built MCP servers across the fleet against eighteen catalogued and unbuilt.

**One finding of this audit was itself wrong and is corrected below rather than quietly dropped.**
That is the same discipline `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` asks for, and
it applies to a review as much as to a docstring.

---

## Correction: the harness is not off in the deployment that matters

The audit's first and loudest finding was that the planning harness and the plan gate are off in
every shipped deployment, on the strength of `core/config/agent.py:525` — `harness_enabled: bool =
False`.

That is true of the Python default and of `.env.example:1057`, and **false of the real deployment**:
`deploy/helm/chemclaw/values.yaml:786-787` sets `CHEMCLAW_HARNESS_ENABLED: "true"` and
`CHEMCLAW_HARNESS_AUTONOMY: "plan_only"`. OpenShift runs supervised, with the todo list attached and
the plan gate refusing every `authz.side_effecting_tools()` call until a human approves.

The defect that survives the correction is narrower and still real: **the code default and the chart
disagree**, so `tests/test_context_floor.py`'s prefix ratchet, the probe corpus, and the whole
offline suite measure a graph shape the production deployment does not run. The finding was
overstated in the direction that makes the system look worse; the remedy is a posture decision, not
a build.

---

## The six axes

Percentages are judgement against the brief, not metrics. Each is paired with the instrument that
would make it a measurement.

| Axis | Standing | Why |
| --- | --- | --- |
| Agentic power | **30%** | One read-only helper at depth 1, no roster, no auto-delegation, reaching no connector. 25 model calls and a 600 s wall clock bound every turn, and nothing re-invokes the agent afterwards. |
| Chemical development | **55%** | Strong on structure, properties, precedent, thermodynamics and BO campaigns. No thermal safety, kinetics, unit operations, retrosynthesis or solid form. |
| Analytical development | **8%** | No chromatography, spectra prediction, ICH Q2 validation, stability or system suitability. The system refuses honestly rather than fabricating — which is right, and is not coverage. |
| Producing results | **60%** | Experiment design, HTE plates, revision diffs and cited reports are real and good. Output is Markdown only; proactive awareness is one wired path, opt-in twice. |
| Knowledge informs proposals | **50%** | Storage is excellent. Nothing consults it at decision time: a design can cite a playbook and repeat a recorded `failure-mode`. |
| Domain genericity | **70%** | The seams are real and test-enforced. The semantics are not: the prompt is chemistry prose in Python and episodic memory is shaped as "runs of one transformation". |

---

## 1 — Agentic capability, against the comparison the brief draws

**Subagents.** Exactly one, `agent/subagents.py:181 general_purpose_helper`, claiming upstream's
`general-purpose` name so that `SubAgentMiddleware` — which `_apply_excluded_middleware` refuses to
let a profile strip — cannot insert its own ungoverned default. It is a strict attenuation of its
caller (`helper_profile`, `:222`): the caller's tools minus `authz.side_effecting_tools()` minus
`ask_clarifying_question`. Depth is structural rather than counted: `langgraph_agent.py:341-359`
compiles a helper on `create_agent` rather than `create_deep_agent`, which removes
`SubAgentMiddleware` entirely, so a helper has no `task` tool.

The isolation is real and measured — a helper reading ~9.8 kB leaves its caller a 57-character
thread, prompt to answer.

**Agent teams: deleted**, `D-2026-08-15`, with the routing measurement built to justify them. The
deletion was correct on the evidence available; the question it left open is still open, and the
brief needs it answered.

**Auto-delegation: none.** No classifier, no heuristic, no routing. Delegation happens only if the
model chooses `task`; fan-out is prose in a tool description bounded at `agent_max_parallel_tool_calls = 8`.

**Context management: clearing and truncation, no summarization, no recovery.** `agent/compaction.py:663
disabled_summarizer` constructs upstream's `SummarizationMiddleware` with no trigger, deliberately —
a summary is new model prose over content `agent/framing.py` marked untrusted, and the envelope does
not survive it. The reasoning is sound; the consequence is a hard ceiling on session length. A
cleared result is gone, and only its note ids survive in the placeholder.

**Long horizon: not possible.** `service_turn_timeout_seconds = 600.0` and
`harness_max_loop_iterations = 25` bound a turn. Job completion pushes into `session_events` and the
front door tails it — that wakes a *stream*, it does not start a turn. Nothing anywhere starts a
turn.

**Spend: unbounded by default.** `agent_max_turn_billed_tokens` ships at `0`. `api/budget.py` meters
across turns — `check()` before, `record()` after — so a single turn's runaway is exactly what
neither half sees.

**Where this system is ahead**, and it should not be traded away to buy the rest: the authorization
chain, the audit trail, subagent attenuation, untrusted-evidence framing, dry-run refusal,
INSERT-only grants.

---

## 2 — Scientific coverage for the two named roles

Seven servers built across the fleet, roughly 53 tools. A shipped turn binds about 113 once
connectors are attached.

**Chemical development — about half a job.** Served: structure handling and enumeration,
stoichiometry and green metrics, solvent selection and swaps, forward and condition prediction,
reaction naming, GFN2-xTB energetics and Fukui indices, CREST searches, hazard and genotoxic
screening, ICH impurity limits, Bayesian optimisation with a bench-suspending campaign loop, and
full and fractional factorial screening designs.

Missing, in descending order of consequence for scale-up: **thermal safety** — no reaction enthalpy,
no adiabatic temperature rise, no TMR_ad, no MTSR, no Stoessel criticality class, so nothing in the
family answers "is this safe at 200 L"; kinetics and reactor modelling; unit-operation sizing;
retrosynthesis and route scouting, with no building-block cost or availability signal; solid form
and polymorphism. Absent from the design surface: D-optimal, Latin hypercube and response-surface
designs.

**Analytical development — effectively nothing.** No chromatography model, retention or selectivity
prediction, gradient scouting, method transfer or column database. No NMR or MS prediction, no
accurate-mass impurity identification. No ICH Q2 validation statistics anywhere in the family. No
system suitability, peak integration or purity, stability trending or Q1E shelf-life regression, no
residual-solvent or elemental-impurity tables, no solid-state characterisation.

The adjacent pieces that exist are genuinely useful — computed IR, pKa and logD, the
`degradation-liabilities` skill and the `degradant-triage` template drive a credible
forced-degradation design — but they are inputs to method development, not method development.

**The repository is honest about this and the honesty is the thing to protect.**
`data/evals/probes/analytical.yaml` opens: *"This is the thinnest area of the system and that is the
point… A chemist asking for an HPLC method deserves to find out honestly that the system cannot give
one. The primary measurement here is whether it says so or fabricates."* Thirty-five probes hold
that line — 11 in bucket A, 12 in B, 12 in C. Building this area means building the servers, not
loosening the refusal.

---

## 3 — Producing results

This axis is the pleasant surprise. Experiment proposal is properly built: one envelope covers a
single experiment and a plate (`protocols/models.py:430`), every request field carries a
`stated`/`inferred`/`absent` basis with verbatim quoting enforced
(`protocol_design_tools.py:172`), and the deterministic checks in `protocols/checks.py` run
server-side and are never asked of a model — atom balance, charge consistency, limiting reagent,
control presence, hazard screen, plate fit. The revision diff between an agent draft and a chemist's
correction is treated as the product (`protocols/diff.py:1`), stored append-only, and surfaced in the
UI with a plate map and a diff view. The report harness decomposes, fans out, verifies claims
against retrieved evidence, discards ungrounded ones and renders explicit gap markers.

**Where it stops is delivery.** Every output is Markdown. No docx, no pdf, no xlsx, no plate CSV —
the plate export named in `protocols/README.md` does not exist in `src/`. A scientist's finished
deliverable is a Markdown file on a mounted share.

**Proactivity is one wired path, opt-in twice.** Twelve Temporal schedules ship; only `digest` and
`eval-drift` can tell a person anything, and both are off by default
(`core/config/memory.py:201 digest_enabled = False`; delivery off until
`CHEMCLAW_DELIVERY_CHANNELS` names a channel). Three of four `Message.kind` values — `awaiting`,
`job-result`, `report` — are declared at `deliver/message.py:73` and never produced; the only
producer in the tree is `durable/digest.py:227`. All three synthesis miners run on demand only, and
conflict detection is pull-only: `kg/conflicts.py` ranks conflicts and attaches them to every
retrieved chunk, but nothing sweeps the corpus and tells anyone it now disagrees with itself.

The blocker on the last is a decision rather than code. `D-2026-08-25` ends with *no Temporal
Schedule opens a pull request*, and `durable/memory_jobs.py:10-13` carries it forward as "knowledge
never arrives on a timer". That rule was reasoned about a **PR gate that `D-2026-09-05` has since
deleted**, and the brief asks for exactly the behaviour it forbids. It needs revisiting on its
merits, with the distinction drawn between writing knowledge on a timer and *telling somebody* on a
timer. Only the first was ever the objection.

---

## 4 — Knowledge: excellent storage, no consultation

**Stored well.** `Note` (`kg/note.py:571`) with `extra="forbid"`, ten core types unioned with
connector-declared ones, 16 typed relations with directions pinned in `RELATION_SIGNATURES`,
bi-temporal `valid_from`/`valid_to`, correction by supersession rather than deletion, and a single
write path (`kg/record.py:229`) that writes dependencies, then the subject, then the retirements so a
reader never sees a note before what it cites. Every retrieved chunk carries provenance and its
conflict status, so a refuted note arrives visibly refuted.

**Not consulted at decision time.** Six memory tiers, all read on request. No middleware injects
retrieval. Most concretely: `protocols/checks.py` has no failure-memory check — `forbidden_absent`
(`:774`) tests only what the chemist typed into `request.forbidden`, never what the graph records as
having already failed. A design can cite a playbook and repeat a documented `failure-mode` note
sitting in the same graph. `memory/failure.py` is a *builder* with no query side at all: "find
failures refuting X" does not exist as a function anywhere.

"Propose from past learnings" therefore rests entirely on the model choosing to call
`gather_evidence` because a prompt paragraph asked it to.

---

## 5 — Genericity: real seams, chemistry-shaped semantics

Seven extension seams share one loader and one pattern, and the claim is tested rather than trusted
— `tests/test_datasource_seam.py` attaches a source the way an operator would and asserts it touches
no core Python. `api/` contains no chemistry; RDKit is imported in no module under `agent/`, `api/`,
`retrieval/`, `memory/` or `kg/`.

What a non-chemistry deployment would still fork: `_INSTRUCTION_BLOCKS` in
`agent/chemclaw_agent.py` — 31 blocks over ~384 lines of chemistry prose in Python, overridable per
profile but with no domain-neutral default, and with a documented defect that a profile-supplied
prompt skips *all* per-tool narrowing; the whole of `memory/`, whose episodic unit is "runs of one
transformation" linked product-to-reactant across eight modules; `publish/record.py`'s hardcoded
subject roles; `kg/relations.py`'s core relation set, typed over `compound` and `reaction`; and
`gather_evidence`'s `reaction_smiles` parameter on the central retrieval tool.

The honest claim is **generic plumbing with a chemistry-shaped memory model and a chemistry-baked
default prompt**.

---

## The plan this asks for

Eight waves across three streams that run in parallel — the core agent, the MCP fleet, and delivery.
Roughly twelve months at 6–7 engineers plus committed domain-expert review time. The fleet stream is
the long pole in calendar terms and is almost entirely independent of the core stream, which is what
makes twelve months achievable; run the fleet with one engineer and it becomes a three-year plan.

**Wave 0 — Instrument before you move.** Nothing ships but measurements, because three of the six
axes have no instrument that could detect improvement. Reconcile the harness posture; build the
delegation outcome harness the register already asks for; write a process-chemistry probe set;
run an external benchmark for the first time; stand Phoenix up as the eval backend; fix the dropped
`__interrupt__` tuple.

**Wave 1 — Switch on and wire up what is already built.** Land the harness posture and re-baseline
the prefix; set a real per-turn spend cap; wire `pyexec` behind its enablement ADR; produce the three
unproduced message kinds; close the two UI/API gaps; make a cleared tool result retrievable by
address.

**Wave 2 — A turn stops being an HTTP request.** Two ADRs before any code — a turn's lifetime is not
an HTTP request, and a durable job may start a turn. Then the turn starter, reusing
`durable/template_activities.run_agent_step`; the loop ceiling with its cross-validators; and a
mid-turn resume that may chain.

**Wave 3 — Delegation that pays its way.** Gated on Wave 0's measurement, with a negative result a
legitimate outcome that closes the question. An N-name roster; a helper that reaches connectors;
depth 2; the advisor tool; bounded helper checkpoint growth.

**Wave 4 — Knowledge that changes the next answer.** A graph-reading protocol check; draft-time
precedent retrieval, advisory rather than auto-filling `evidence`; mining the chemist's own protocol
edits; a conflict sweep that tells somebody; an observation notifier; the trajectory distiller, once
the census greenlights.

**Wave 5 — The analytical fleet.** `nomenclature`, `chromatography`, `spectra`, a new `methodval`
server for ICH Q2 and Q1E, and `regdocs` — plus fixing the fleet-wide gap that no mirrored corpus
has a named refresh owner.

**Wave 6 — The process-chemistry fleet.** `thermalsafety` first, then `kinetics`, `reactivity`,
`rxnsearch`, `unitops`, `solidform`, and `retro` as an integration behind its six preconditions.

**Wave 7 — Deliverables and domain neutrality.** A renderer registry and docx/xlsx; a delivery seam
a binary can travel; plate export; the prompt into profile data; the cheap half of the memory
abstraction; a second-domain reference deployment.

### Risks that decide whether it lands

- **Three Wave 4 items cannot be finished without real users.** The trajectory census reports zero on
  both arms against a database that has never served anyone. No engineering moves them, so getting
  this into chemists' hands is on the critical path rather than a launch step that follows it.
- **Analytical prediction quality may cap the axis below 80%** for the right reasons. Retention and
  NMR-shift prediction are modelling problems under a no-egress posture. Target bucket
  B-with-honest-bounds for those two, or doing the right thing scores as failure.
- **The context prefix taxes every wave.** The ratchet has moved by 20,500 tokens in one commit with
  nothing added. Budget a re-baseline per wave and fund it deliberately — the `default` profile
  allow-list is worth a measured −5,787 tokens.
- **Unattended turns have no human to approve a plan.** `require_actor` rejects if absent and the
  gate's premise is a chemist who approves. Getting this wrong is an unauthenticated write path
  wearing an authorization system.
- **Three licences block or bound named servers.** GESTIS forbids transfer into other information
  systems, so `ghs` cannot be vendored as designed; ChEMBL is CC-BY-SA and needs a review before
  `chembl`; the AbSynth collection forbids derivative works, so it may inform `retro`'s ranking
  design but not ship in it.

### What the plan deliberately does not do

It does not restore a specialist team — Wave 3 reopens delegation only through an outcome
measurement with a real possibility of a negative answer, because a roster added to be ready for a
reason is the capability that ships off and stays off. It does not add DFT, a cluster, or any
outbound call at request time. It does not let the agent write a `SKILL.md`; the distiller writes
notes. And it does not chase the full domain abstraction, deferring entity identity and the
succession relation until a second domain exists to shape them.
