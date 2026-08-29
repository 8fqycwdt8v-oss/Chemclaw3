# Protocol generation — from historic record and tools to a runnable experiment

**Branch:** `claude/eln-protocol-generation-jk0i04` (all three repos)
**Ask:** generate new protocols/conditions from historic ELN + HTE data and the tool fleet, for
**single experiments** and **HTE campaigns/screens** alike; structure unstructured free-text input;
show them well, make them tailorable, persist them.

---

## 1 — What is actually missing (measured against `HEAD`, not assumed)

The system can already *read* the record and *suggest points in a design space*. Every ingredient
exists and none of them produces a protocol:

| Have | Where |
| --- | --- |
| Faceted precedent over a labelled corpus | `rxnfp`: `substrate_precedent`, `conditions_for_similar_reaction`, `conditions_for_similar_product`, `reagent_frequency`, `workup_precedent`, `reactions_making_substructure` |
| Structure/reaction similarity | `molfp`: `similar_molecules`, `substructure_matches`; `rxnfp: similar_reactions` |
| Many whole protocols → one comparison | `agent/condense.py::condense_protocols` (`ProtocolDigest`, `digest_source: extracted`) |
| Evidence sweep over every enabled source | `agent/research_tools.py::gather_evidence` |
| Memory tiers | `memory/{campaign,optimization,playbook,failure,progression,observations}` |
| Design of experiments | `bo`: `generate_screening_design` (factorial/fractional), `suggest_next_experiment` (BO), `predict_outcome`, `campaign_progress` |
| Physics / properties | `calc`: 17 tools + 12 durable jobs |
| Bench chemistry | `chem`: `resolve_compound`, `stoichiometry_table`, `green_metrics`, 6 enumerations |
| Safety | `safety`: `screen_hazards`, `screen_genotoxic_alerts`, `ich_impurity_limit` |

**What has no home at all:**

1. **A prescriptive object.** `OrdReaction`/`ReactionRecord` are *descriptive* — the ORD schema says
   so in as many words ("describe what was actually done in the lab, and not an idealized protocol
   or instruction set"). Nothing in this tree represents *what to run*.
2. **A structured intake.** A chemist's ask arrives as free text. `structure_experiment_request`
   does not exist; the request is re-derived from the transcript on every turn and is not editable
   before the expensive work starts.
3. **Anything for a plate.** `generate_screening_design` returns *runs*; nothing turns runs into a
   layout, controls, replicate placement or a plate map.
4. **Persistence + revision.** A drafted protocol lives in one transcript. There is no id to reopen,
   no revision, no diff, and therefore no record of the edit an expert makes — which is the single
   highest-value learning signal the task names.
5. **A UI surface.** `USER-STORIES.md` E4 is `PARTIAL` and G1 is `PROSE-ONLY`; the closest thing to a
   protocol view is `RunSheet` in the result registry.

## 2 — The design, in one page

### 2.1 One envelope, two shapes

**`ExperimentDesign`** is the envelope; **`ProtocolArm`** is one runnable set of conditions.

> A single experiment is a design with **one arm and no factors**. An HTE screen is the same design
> with factors, levels, N arms and a layout. Written once, the store, the checks, the renderer, the
> export and the UI serve both — which is what the ask means by "structured for both".

```
ExperimentDesign
├── request:   ExperimentRequest      # the structured ask (§2.2)
├── mode:      single | screen | campaign
├── base:      ProtocolBody           # what every arm shares: steps, charge, analytics, safety
├── factors:   list[Factor]           # [] for a single experiment
├── arms:      list[ProtocolArm]      # 1 for a single experiment; N for a screen
├── layout:    PlateLayout | None     # wells, controls, run order (screen only)
├── evidence:  list[EvidenceRef]      # precedent + tool citations — why these conditions (§2.4)
└── checks:    list[ProtocolCheck]    # deterministic verdicts, computed not asserted (§2.5)
```

Why not a `Note`: a draft is a proposal to act, not a knowledge claim. The PR-gate answers "is this
true"; nothing about a draft is true yet. Same argument as
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim`, arriving from the other side. Rows, not
notes. A chemist who wants a *rule* out of an approved protocol still proposes a `playbook` or an
`experiment-proposal` note citing it.

Why not a `Template`: a template is a fixed chain of orchestration steps with no loops
(`D-2026-08-25-the-loop-is-a-composite-not-a-template`). A protocol is chemistry, not orchestration.

### 2.2 Structuring the free text — the honest version

`ExperimentRequest` is filled **by the turn's own model** through a tool schema; no second LLM call,
because the model is already reading the chemist's text. What makes it honest is that every slot
carries its own basis:

```
RequestField{ value, basis: stated | inferred | absent, quote: str }
```

and the tool **refuses** `basis="stated"` without a `quote` that occurs verbatim in the supplied
text. Inference is allowed here — a *request* is not a *record* — but it can never be silent. This
is the counterpart of `D-2026-08-26-a-transcription-may-not-infer-a-setpoint`: that rule forbids
inference into a record; this one requires it be marked in a proposal.

The historic free text is *already* structured by two existing paths and this layer reuses both
rather than adding a third extractor: deterministic segmentation at ingest
(`eln/json_adapter.py`), and marked extraction behind `condense_protocols`
(`digest_source: extracted`).

### 2.3 Where the work runs

**In the turn, composed by the agent under two new skills** — not in a new Temporal workflow.
Putting the pipeline in Python would freeze chemistry judgment into orchestration code, which is the
one thing this tree consistently refuses. Code owns the *shape* and the *checks*; skills own the
*judgment*; the agent composes the tools.

The tools that make it enforceable rather than aspirational:

| Tool | Does |
| --- | --- |
| `structure_experiment_request` | validates + persists the structured ask (revision 1) |
| `draft_experiment_protocol` | validates, checks, persists a protocol revision (new design or a revision of one) |
| `read_experiment_protocol` | one design at a revision |
| `find_experiment_protocols` | list by status/project/session |

`draft_experiment_protocol` **refuses a design with no precedent citation and no tool citation.** A
protocol with neither is a guess, and the refusal is what makes "use the tools massively" a property
of the code rather than a hope about the prompt.

### 2.4 Evidence is a citation, not a sentence

```
EvidenceRef{ kind: precedent | tool | note | record | observation,
             ref, tool: str, summary: str, supports: list[str] }
```
`supports` names the design paths the citation is offered for (`base.temperature_c`,
`factors[0].levels`), so the UI can put the reason next to the number and a reader can check it.

### 2.5 The checks are code

Deterministic verdicts, each `blocker | warning | note`, computed at draft time and stored with the
revision — never the model's opinion about its own work:

components resolve · charge table consistent with the limiting reagent · atom balance ·
factor levels declared · arms distinct · layout fits the plate · every arm placed · controls present ·
evidence present · hazard screen ran · quantities bounded · forbidden reagents absent.

### 2.6 Persistence and tailoring

`experiment_protocols` (identity, status, head revision) + `experiment_protocol_revisions`
(immutable, `parent_revision`, `author_kind agent|human`, `document JSONB`, `checks JSONB`).

- **Editing is a new revision**, never an update. Optimistic concurrency on `parent_revision`.
- **The diff between revisions is the product**, not a debug aid: it is exactly "what did the expert
  change about the first shot", and it is stored where a later miner can read it.
- The agent revises through `draft_experiment_protocol(design_id=…, parent_revision=…)`; a human
  revises through `POST /protocols/{id}/revisions`. Same table, `author_kind` tells them apart.

### 2.7 The UI

Three surfaces from two pieces of work:
- a `protocol` entry in `src/results/renderers.tsx` → in-answer card + `ResultSheet` + `TracePanel`
  full-result, free, because all three read the one registry;
- a `/protocols` route and a `/protocols/:id` document view (the `/review` + `/jobs` shape), with the
  checks strip, the factor table, the **plate map**, the run sheet with CSV, the evidence panel and
  the revision history with a field-level diff;
- field-level editing that posts a human revision, gated on `parent_revision` with a 409 the way
  `decidePlan` is gated on `plan_hash`.

---

## 3 — Tasks

### P1 — `src/chemclaw/protocols/` (the shape)
- [ ] `models.py` — `ExperimentRequest`, `RequestField`, `Factor`, `ProtocolArm`, `ProtocolBody`,
      `ProtocolStep`, `ChargeLine`, `Analytic`, `EvidenceRef`, `PlateLayout`, `ExperimentDesign`,
      `ProtocolCheck`, `DesignRevision`. Reuse `ingest.eln.ord.StepKind`/`Role` and
      `kg.note.ProcessConditions`; do not restate them.
- [ ] `layout.py` — plate formats (24/48/96/384/1536), well labels, `place(arms, controls, …)`,
      row-major and seeded-random order.
- [ ] `checks.py` — the checks, each a pure function over a design.
- [ ] `diff.py` — field-level diff between two documents.
- [ ] `render.py` — the Markdown a reader gets; the JSON payload the model + UI get.
- [ ] `store.py` — `DesignStore` Protocol, in-memory + Postgres backends, `default_design_store()`.
- [ ] `README.md`.

### P2 — persistence
- [ ] `infra/sql/073_experiment_protocols.sql` (additive only).
- [ ] `agent/leaver.py` — declare `experiment_protocols.opened_by` /
      `experiment_protocol_revisions.author` in the **retain** tier with the reason
      (a protocol is a shared scientific artifact, like `bo_campaigns.opened_by`).
- [ ] `durable/retention.py` — a position on the two tables.

### P3 — the agent surface
- [ ] `agent/protocol_design_tools.py` — the four tools; register the module in
      `agent/chemclaw_agent.py`'s import block.
- [ ] `agent/authz.py` — `structure_experiment_request` and `draft_experiment_protocol` are
      state-changing (they write rows), the two reads are not.

### P4 — judgment
- [ ] `skills/protocol-generation/SKILL.md` — the single-experiment judgment.
- [ ] `skills/hte-campaign-design/SKILL.md` — factors, levels, design type, controls, replicates,
      plate constraints, analytics.
- [ ] cross-reference from `connectors/bo/skills/experiment-design`.

### P5 — the front door
- [ ] `api/routes/protocols.py` + `api/schemas.py` shapes + `create_app` registration.

### P6 — the gates
- [ ] `tests/test_layering.py` — `protocols → {core, kg, ingest}`, `{agent, api} → protocols`.
- [ ] `ARCHITECTURE.md` row (`tests/test_repo_map.py` fails otherwise).
- [ ] `data/evals/probes/protocol-generation.yaml` — one probe per new tool
      (`tests/test_probe_coverage.py`).
- [ ] `tests/test_context_floor.py` — measure, then raise the ceiling with the number in the commit.
- [ ] tests: `test_protocol_models.py`, `test_protocol_checks.py`, `test_protocol_layout.py`,
      `test_protocol_store.py`, `test_protocol_tools.py`, `test_protocol_routes.py`,
      `test_protocol_diff.py`.

### P7 — the record
- [ ] ADR `D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not.md` + ledger row.
- [ ] `docs/planning/BACKLOG.md` — the follow-ups this deliberately does not do, each with its cost.

### P8 — `Chemclaw3_ui`
- [ ] `shared/events.ts` — nothing new needed for the renderer; add the API types.
- [ ] `server/routes.ts` — whitelist the six protocol routes (+ `tests/routes.test.ts`).
- [ ] `src/api/client.ts` — the methods.
- [ ] `src/results/renderers.tsx` — the `protocol` renderer (card + sheet).
- [ ] `src/routes.tsx` + `src/components/ProtocolPanel.tsx` + `ProtocolDocument.tsx` + `PlateMap.tsx`
      + `RevisionDiff.tsx`; sidebar link.
- [ ] vitest specs + an e2e spec.

### P9 — ship
- [ ] `make lint type test` green in `Chemclaw3` (with Postgres up — report what skipped).
- [ ] `npm run lint typecheck test` green in `Chemclaw3_ui`.
- [ ] PR per repo, auto-merge, then review→fix cycles until clean.

## 4 — Deliberately not in this change

- **Wiring `rxnpredict`** (`predict_reaction_conditions`) as a connector. It is the highest-value
  addition to this pipeline and it is a *default-surface* decision, not a wiring change — the same
  argument the `pyexec` backlog row makes: discovery is enablement, so a manifest here turns six new
  tools on in every fresh checkout, needs six probes and moves the context floor. Backlog row, own PR.
- **A durable `ProtocolDraftWorkflow`.** The composition is a turn's work today; a workflow becomes
  right when a draft fans out durable calc jobs, and that is a measurement to take first.
- **Mining the human-edit diffs into playbooks.** The diffs are stored from day one so the
  measurement is possible; the miner needs a corpus that does not exist yet.

## 5 — Review

**Shipped.** `chemclaw.protocols` (models, checks, layout, diff, render, store), migration 073 with
its grants, four agent tools, five HTTP routes, two skills, the ADR, and the whole `Chemclaw3_ui`
surface (contract, BFF routes, client, result renderer, `/protocols` list, document view, plate map,
revision diff, field-level editor).

**Four things the work changed about its own plan, each because a measurement said so:**

1. **`draft_experiment_protocol` does not take the ask back.** It first took a whole
   `ExperimentDesign`; `tests/test_context_floor.py` refused that at 4,645 tokens and narrowing it
   produced a better contract than the prose had — the tool takes `design_id` plus the protocol half
   only, so `structure_experiment_request` is a prerequisite structurally rather than by advice, and
   the ask exists in exactly one place. Measured: the two writing tools went 6,231 → 3,380 tokens
   (−46%), the `default` prefix 35,035 → 32,184, and the ceiling moved 29,500 → 33,000 rather than
   to the 36,000 an unnarrowed version would have needed.
2. **A `request` revision is checked at its own stage.** The first version ran every check
   on the structured ask, so `evidence_present` failed at *blocker* severity on every intake — a
   blocker that fires on the normal path is one a reader learns to ignore, which would have hollowed
   out the one blocker the design depends on. Found by running the tool end to end, not by a test.
3. **`ProtocolArm.charge_overrides` is deleted.** No producer, and it inlined the whole `ChargeLine`
   model into every schema; an arm that varies an amount declares it as a continuous factor.
4. **`SpeciesRole`'s docstring shipped three times in one schema**, `RequestField`'s four times —
   pydantic publishes a referenced model's class docstring as a JSON-schema description and
   `convert_to_openai_tool` inlines rather than `$ref`s. Fifteen model docstrings moved into `#`
   comments. The general finding is filed as a `BACKLOG.md` row, because the real fix is upstream
   and would cut all four `KNOWN_OVERSIZED` entries at once.

**One thing recorded rather than resolved.** Both writing tools stay over `MAX_SINGLE_TOOL_TOKENS`
and are in `KNOWN_OVERSIZED` with their measurements: `base: ProtocolBody` is 922 tokens with every
description already one line, so a typed laboratory procedure cannot meet a 900-token bound. The
alternatives were measured against and rejected in that file — a JSON-string or scratchpad payload
drops the schema to ~150 tokens and takes schema-guided generation with it, on the call where a
malformed argument is most expensive.

**Two things looked like defects and were not.** A full-suite run failed
`test_durable_observability.py` twice; both were the docker daemon dying mid-run, and both pass with
Postgres up. Eight `e2e/protocols.spec.ts` failures were a `dist/` I had rebuilt without
`ALLOW_DEV_AUTH=true`, which is what that harness serves; all eight pass on the build it expects.
