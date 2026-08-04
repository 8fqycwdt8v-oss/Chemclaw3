# User-story capability map

**What this is.** A code-based audit of all **106 user stories** in the chemical and analytical
development requirements set, against the tree as it stands. Every verdict is grounded in source —
a tool name, a `file.py:line`, a manifest, a schema field — not in observed behaviour. The 190-probe
live run (`docs/archive/live-user-stories-2026-08.md`) is cited only as corroboration.

The per-section audits, with a row for every story and its evidence, are in `tasks/story-audit-*.md`.

**Why the distinction between kinds of "missing" carries the whole document.** A missing *table* is a
week; a missing *model* is a research programme; a missing *entity* is a schema migration across the
corpus. A roadmap that calls all three "not supported" is useless. So:

| verdict | meaning |
| --- | --- |
| `FULL` | A named, agent-reachable path serves the story end to end. |
| `PARTIAL` | Real machinery serves part of it; the audit names which part, and which part it does not. |
| `MISSING-TOOL` | The computation exists in the tree, nothing exposes it to the agent. **Cheapest class.** |
| `MISSING-DATA` | The machinery works; the corpus, table or record type does not exist. |
| `MISSING-MODEL` | Needs a predictive or physical model the system would have to build or licence. |
| `MISSING-ENTITY` | Needs a first-class concept the schema lacks — a project, a document, a method, a batch, an instrument, a person. |
| `OUT-OF-SCOPE` | The requirements exclude it, or the architecture deliberately refuses it (D-089). |

---

## The headline

| verdict | stories | share |
| --- | ---: | ---: |
| `FULL` | 10 | 9% |
| `PARTIAL` | 50 | 47% |
| `MISSING-ENTITY` | 30 | 28% |
| `MISSING-DATA` | 6 | 6% |
| `MISSING-MODEL` | 6 | 6% |
| `MISSING-TOOL` | 2 | 2% |
| `OUT-OF-SCOPE` | 2 | 2% |

**57% of stories have working machinery behind them today** (`FULL` + `PARTIAL`), and only **6% need
a model that does not exist**. The system is far less short of *capability* than it looks.

**The dominant gap is schema, not science.** 30 of the 44 missing stories — 68% — are
`MISSING-ENTITY`: the system cannot represent a **method**, a **document**, a **project**, a
**batch**, an **instrument**, a **person**, an **incident** or a **solid form**. Nothing about
chemistry blocks them. Four entities alone (`method`, `project`, `document`, `near-miss`) would move
roughly twenty stories.

**Only two stories are `MISSING-TOOL`** — computation stranded behind a missing wrapper. That is a
good sign about the connector seam: capability that exists is, with two exceptions, reachable.

---

## By section

Sorted by how well served the section is.

| § | section | n | `FULL` | `PARTIAL` | missing | verdict on the section |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 13 | Regulatory input preparation | 5 | 3 | 1 | 1 | **Best served.** The report harness is purpose-built for this. |
| 9 | Data interpretation & reporting | 3 | 1 | 2 | 0 | Nothing missing; quality is a prompt problem. |
| 2 | Experiment & study planning | 4 | 0 | 4 | 0 | Every story partly works; none completely. |
| 15 | Training & knowledge continuity | 3 | 1 | 2 | 0 | Retrieval carries it. |
| 17 | Trust & governance | 5 | 0 | 5 | 0 | Every mechanism exists and none is *binding*. See below. |
| 1 | Institutional knowledge & search | 5 | 1 | 3 | 1 | The system's core competence. |
| 4 | HTE campaigns | 6 | 0 | 5 | 1 | Design yes, plate logistics no. |
| 10 | Safety & risk | 3 | 1 | 0 | 2 | The one `FULL` is genuinely excellent; the other two need a register. |
| 3 | Bayesian optimization | 6 | 1 | 2 | 3 | Engine is sound; the loop is open. |
| 6 | Reaction chemistry | 18 | 2 | 9 | 7 | Calculators + precedent carry over half. |
| 14 | Tech transfer | 3 | 0 | 2 | 1 | Report harness again, without a package concept. |
| 12 | Master batch records | 5 | 0 | 2 | 3 | No document entity. |
| 11 | Scale-up | 5 | 0 | 2 | 3 | Precedent yes, engineering models no. |
| 16 | Portfolio oversight | 4 | 0 | 1 | 3 | No project entity. |
| 8 | Analytical data detail | 19 | 0 | 8 | 11 | Thinnest section, and it is schema-bound. |
| 7 | Analytical method development | 5 | 0 | 1 | 4 | Blocked almost entirely on one missing note type. |
| 5 | Robotic lab hardware | 7 | 0 | 1 | 6 | No hardware interface of any kind. |

---

## What is genuinely `FULL` (10 stories)

Worth stating plainly, because a report of gaps reads as a report of failure.

- **1.1** ask how a similar experiment was run — `gather_evidence` over the graph, ELN and
  fingerprint sources.
- **3.1** which experiments to run next given results so far — `suggest_next_experiment`; the engine
  returns correct candidates with an explore/exploit split when driven on real data.
- **9.1** turn raw data into a first-pass summary.
- **10.1** hazards for a reaction or reagent combination — 16 cited SMARTS rules across nine
  energetic classes plus five pairwise incompatibilities, with a clean screen explicitly *not* a
  clearance. The strongest single capability in the system.
- **13.1, 13.3, 13.5** assemble, structure and keep traceable the data behind a submission section —
  the report harness renders only retrieved chunks, each wikilinked to its source, marks an
  unsupported section as unsupported, and keeps a *failed* section visibly distinct from an *empty*
  one.
- **15.1** "how do I…" answered from internal practice.
- **6.1, 6.16** troubleshoot a stalling coupling and a workup problem from past cases.

---

## The four entities that unlock the most

Ranked by stories moved per unit of work.

| entity | stories it unblocks | size |
| --- | --- | --- |
| **`method`** — what we ran, and how it performed | **8**: 7.1, 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 8.5 | `M` |
| **`project`** — a real field, not a free-text tag | **~7** across 13.4, 14.3, 16.1, 16.2, 16.4, 1.3, 15.3 | `M` |
| **`document`** — an MBR/controlled document type | **3**: 12.1, 12.2, 12.3 | `M` |
| **`near-miss`** — a safety incident record | **2**: 10.2, 10.3 | `M` |

A `method` note type is **not** a retention model. It is "we ran this gradient on this column and it
resolved these peaks" — the same shape as the `reaction` note that already exists, reusing
`memory/progression.py` for time-ordering and condition diffing. Eight of the nineteen analytical
stories turn on it.

**`project` is the one to be careful about.** It looks like a field and is a migration: `Note` has
no project field at all. When this audit was written `OrdReaction.project` never reached the graph
either, because `ingest/eln/note.py` wrote no tags — measured on the committed corpus, 6 of 993
reaction notes carried any tag — and `kg/analytics.py` reported a set difference over free-text
tags (`playbook`, `solvent`, `suzuki`) under the name `projects_without_distillation`, which is how
a correctly-computed field produced a fabricated portfolio status report in the live run.

Both halves of that are fixed (D-2026-08-02-shipped-is-not-reachable, and the rename to
`tags_without_distillation`), and neither fix makes `project` a field. That is the intent: the ELN
note builder now writes the project as a *tag*, so the migration has real data to migrate and
`gather_evidence(tag=…)` works on the largest note class before the schema changes.

---

## Cheapest real wins

Each is days, and each is backed by machinery that already exists. **All six have since shipped**
— see the "what changed" note at the end of this document. They are kept here as written, because
the argument for each is the reason the next six should be chosen the same way.

1. **ICH Q3C / Q3D limit tables behind a lookup tool** (`S`). Serves 8.14 and 8.16 outright,
   improves 8.15, and closes 8.10's remaining gap. Published static tables, no schema change, no
   model — and it removes the most dangerous fabrication class in the run, where the system recited
   a palladium PDE from training as though it were the record.
2. **Read the BO campaign store back** (`S`). `read_campaign` and `suggestions_for` have **zero
   non-test callers**. The entity, the stable id hash, both backends and the migration all exist and
   are written on every suggestion; the tool docstring tells the agent to quote the id back "so a
   later session picks the thread back up" — and nothing can. One `resume_campaign` tool turns a
   shipped-but-dead subsystem into the cross-session loop stories 3.2 and 3.5 need.
3. **Index impurity structures** (`S`, one line). `ingest/eln/ingest.py` indexes `inputs + outcomes`,
   so recorded impurity structures never enter the molecule fingerprint index — while the impurity
   block *is* rendered into note text. Structural search cannot find them and lexical search can,
   the exact inverse of what story 8.8 needs.
4. **Give `stoichiometry_table` volumes and densities** (`S`). It accepts only molar equivalents, so
   a solvent charge is inexpressible — which breaks the pairing `green_metrics`' own docstring
   documents, on the term that dominates E-factor and PMI. In the live run "10 volumes" was fed in
   as 10 molar equivalents and produced a charge table wrong by 2.2× on the principal solvent.
5. **A cited genotoxicity SMARTS table beside `rules.yaml`** (`S`–`M`, pure data). The matcher,
   the severity/citation schema, `screen_hazards`, the briefing template and the PR-gate hazard
   section all exist and need no code. **Caveat that matters:** structural *alerts* are data; an
   ICH M7 *class*, a purge factor or an acceptable-intake limit is `MISSING-MODEL` and must not be
   costed as data.
6. **Three columns on the optimization-campaign table** (`S`) — purity, major impurity, area%. The
   data is on every `OrdReaction` already; the one artifact built for side-by-side reading carries
   only temperature, time and yield, which is why 6.3 and 6.18 are `PARTIAL`.

---

## The governance section is the sharpest result

**All five §17 stories are `PARTIAL`, and all five for the same reason: the mechanism exists and is
not binding.**

- **17.1** every answer shows its sources — `verify_claims` genuinely discards a claim whose citation
  was not retrieved, and it *is* reachable from the chat path, through its single production caller
  in `agent/verifier.py`. But `verifier_enabled` defaults `False`, so nothing ran it; and the
  deterministic backend scores an *uncited* answer at confidence 1.0, so enabling it would not catch
  an answer that cites nothing at all. Measured in the live run: 0 of 33 analytical answers carried
  a single wikilink. "Shows its sources" is true; "is required to" is not.
  Worth separating from a common misreading: the **report** path never calls `verify_claims`, and
  does not need to — it synthesizes no prose, only renders retrieved chunks. The module docstring
  reads as though the gate runs inside the report; it does not, and the report is safe for a
  different reason.
- **17.2** a boundary between autonomous and sign-off work — the PR-gate is real and binding for
  *knowledge notes*. Nothing gates a drafted document.
- **17.3** an audit trail — genuinely hash-chained and verified by `make audit-verify`. But
  `AuditEvent.purpose` is deliberately unpopulated, so the trail records *what* and not *why*.
- **17.4** role-based access — `authorize_tool` gates **tool names**, not data. `_eligible_notes`
  filters only on arguments the model itself supplies. There is one shared corpus (a recorded
  deferral, KM-9). Both models in the live run described a per-team read boundary that does not
  exist; the cheapest fix is two sentences of instruction, not a mirror.
- **17.5** correct what the assistant knows — `record_confirmed_answer` and `supersedes` edges exist;
  `memory/failure.py`'s `failure_note`, which builds exactly the right refutation note with a
  `contradicts` edge, has **zero production callers**.

---

## Where the system is least honest

The live run's corrected verdicts measured fabrication at **22%** on stories the system can serve,
**39%** where the substrate is partial, and **46%** where nothing backs the question — monotonic.

That ordering is the argument this map exists to support: **the sections with the most
`MISSING-ENTITY` rows are the sections where the system invents most**. §5 (6 missing of 7) and §16
(3 of 4) were the worst-behaved slices in the run. For those stories the cheapest honest improvement
is not the capability — it is making the *refusal* correct and checkable, which is the deterministic
output gate already on the backlog.

---

## What this map does not tell you

- **Nothing here was re-measured live.** Verdicts are from the code; the live run corroborates but
  does not establish them, and it ran on one model.
- **The durable layer is judged as written code.** Temporal could not start in the test environment,
  so eight job launchers and `request_development_report` were unreachable — a deployment
  requirement, not a capability gap, and marked as such. Roughly 90% of the credited analytical
  machinery is inline MCP tooling needing no broker.
- **Sizes are engineering judgement**, not estimates from a plan.

---

## What changed after this audit (2026-08-02)

The verdicts above are the state the audit found. The audit was then worked, in three waves, and
the rows below are what moved. **The verdict tables are deliberately not rewritten** — this
document's value is the reasoning that produced each verdict, and editing them in place would leave
no record of what an audit is worth.

| shipped | ADR | stories it moves |
| --- | --- | --- |
| `resume_campaign`; impurity structures indexed; ELN notes tagged with their project; a `record_failure` write tool through the PR-gate; conflict/confidence/provenance rendered in reports; scale on the reaction note; purity, major impurity and area% on the campaign table | D-2026-08-02-shipped-is-not-reachable | 1.2, 1.3, 3.2, 3.5, 6.3, 6.6, 8.8, 11.2, 12.4, 13.2, 13.5, 17.5 |
| The verifier scored against the turn's own tool results; an uncited answer is unverified; a deterministic ungrounded-parameter scan (off by default); the durable-subsystem outage announced before the turn plans; the access boundary stated instead of implied; `projects_without_distillation` renamed | D-2026-08-02-grounding-is-what-this-turn-saw | 17.1, 17.4, 17.5 |
| ICH Q3C/Q3D limit tables behind a lookup; a cited genotoxicity structural-alert table | D-2026-08-02-a-limit-is-data-a-classification-is-a-model | 8.10, 8.14, 8.15, 8.16 |
| Volumes and densities for `stoichiometry_table` | D-2026-08-02-a-solvent-charge-is-a-volume | 6.9, 6.10 |
| Reduced (fractional) screening designs | D-2026-08-02-the-fraction-lives-where-bofire-will-fractionate | 4.1, 4.4 |
| A warehouse ELN attachable by configuration rather than by an adapter, carrying both halves: curated reactions ingested through the PR-gate, and similarity search run inside the warehouse over its own embedding column, so the whole ELN is reachable as evidence and not only the ingested slice | D-2026-08-04-the-schema-is-a-file | 1.1 |

**Two things did not change, and they are the two that matter most.** `project`, `method`,
`document` and `near-miss` are still not entities, so the 30 `MISSING-ENTITY` rows are still 30.
And the honesty work is **argued, not re-measured** — the shape gate ships off by default and the
46% has not been re-run with it on (`docs/planning/BACKLOG.md`).
