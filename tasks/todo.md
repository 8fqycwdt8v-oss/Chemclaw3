# The remaining actionable parts of C, D and E

Follow-up to `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`, which investigated
C (per-helper connector sessions), D (an advisor) and E (a second roster name) and built none of
them. What each investigation left actionable *now*, as distinct from what it left to a measurement:

## C — the stated reason is not the binding one

- [x] `agent/subagents.py` and `langgraph_agent._subagents` both give "two concurrent turns over one
      MCP tool object deadlock" as why a helper reaches no connector. That measurement is real and
      it is about **sharing one session object**; a helper holding sessions of its own shares
      nothing, and `open_connector_specs` already opens a fleet concurrently by design.
- [x] Replace it with the constraint that actually binds — the lifecycle — which is also the
      stronger argument: connectors are opened by the *async caller* into an `AsyncExitStack`
      before the *synchronous* builder runs, and the roster is frozen per compiled graph, so a
      per-helper set means a second full set opened eagerly on every turn.
- [x] `tests/test_subagents.py::test_a_helper_holds_no_connector_tool` cites the same wrong reason.
- [x] Behaviour unchanged. The BACKLOG row keeps the behavioural half, gated on the measurement.

## D — the guard the advisor investigation exposed, fixed without building the advisor

- [x] `tests/test_spend_cap.py::test_no_in_tool_model_call_passes_its_own_callbacks` guards the
      chain that puts a tool body's model call on the turn's ledger — and it guards it in
      **`agent/condense.py`**, by name. A second in-tool model call walks past it in silence.
- [x] The realistic mistake is not hypothetical: `verifier.py` passes `config=off_stream_metering()`
      deliberately, and `off_stream_metering`'s own docstring says attaching it to an in-graph call
      would take that call off the stream. Copying that line into a tool body is one edit.
- [x] Derive the module set instead: every module that defines a registered tool **and** makes a
      model call. Module granularity, not per-function — in `condense.py` the `.ainvoke` is in
      `_read_prose`, and the tool is `condense_protocols`, so a per-function scan misses it.
- [x] The advisor itself is **not** built: it cannot be enabled without a second model tier this
      deployment does not have, which is the shape `D-2026-08-15` deleted 3,300 lines for.

## E — nothing to implement, and saying so is the deliverable

- [x] The recommendation was to leave it closed; the BACKLOG row already carries the trigger. No
      code follows from "not yet", and adding a second roster name to be ready for one is the
      capability-that-ships-off shape again.

## Verification

- [x] The derived scan finds exactly the module the hardcoded one named, and would fail on a
      planted second offender.
- [x] `make lint type test`, reporting what it skipped.
- [x] Two ADRs, one decision each, and their ledger rows.

## Review

**Two of the three had an implementable part; E did not, and that is the finding rather than an
omission.**

**C — done, and the correction made the bound stronger rather than weaker.** The three places that
said "two concurrent turns over one MCP tool object deadlock" now say what actually binds: a
connector's tools do not exist until its session is live, so the async caller opens them into an
exit stack *before* the synchronous builder runs, and `SubAgentMiddleware` freezes the roster at
compile time — a helper cannot open sessions at spawn time even in principle, and its own set would
mean a second full set opened eagerly on every turn, spawned or not. The deadlock measurement keeps
the one job it fits, in `test_a_helper_holds_no_connector_tool`: it is why the caller's *already
open* tools must not be passed down, which is the edit that test exists to catch. No behaviour
changed.

**D — the advisor is still not built, and the trap it exposed is closed.** Building it now would
create a capability that cannot be enabled without a second model tier this deployment does not
have, which is the exact shape `D-2026-08-15` deleted 1,442 lines for. What *was* implementable is
the guard: `test_no_in_tool_model_call_passes_its_own_callbacks` scanned `agent/condense.py` **by
name**, so it guarded one file rather than the invariant, and a second in-tool model call would have
walked past it in silence — silence being the defect's own signature, since what fails is that spend
stops being counted. It now derives its module set from the tool registry: every module that defines
a registered tool *and* builds a model.

*Two things that decided the derivation's shape, both found by looking rather than reasoning.*
`agent/verifier.py` passes `config=off_stream_metering()` and is **right** to — a judge runs outside
the graph where nothing else meters it — and it holds **zero** registered tools, so requiring both
halves excludes it precisely; a naive "every module that builds a model" scan would have failed on
correct code. And in `condense.py` the `.ainvoke` is in `_read_prose` while the tool is
`condense_protocols`, so a per-function scan would have found **nothing** — module granularity is
not looseness here, it is the only granularity that sees the one call there is to see.

**The new scan was verified to fail rather than assumed to.** A planted second module holding a
registered tool and an `ainvoke(..., config=off_stream_metering())` failed the test naming the file
and the line; the by-name scan passed on the same tree. Then removed.

**E — nothing to implement, recorded in the row so nobody looks again.** Everything a second roster
name needs already exists — `AgentProfile.model_route` for its model, `helper_profile` for its
surface, `governed_roster` for its governance. What is missing is the reason, and a name added to be
ready for one is the capability that ships off and stays off.

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

## 6 — Review cycle (2026-08-29)

An adversarial pass over the merged tier found **fifteen** defects, all under a green 231-test
suite, all now fixed — `D-2026-08-29-the-review-of-the-prescriptive-tier-found-fifteen-defects`.

**Four of the five worst were a blocker that could not fail**, each under a passing test written
from the same misunderstanding as the code: `components_resolve` never consulted the strict parser
on the silent-truncation class it exists for (`"CCO junk"` passed as `1 structures parse`);
`forbidden_absent`'s structure half could never fire for a named reagent (forbidding DMF let
`N,N-dimethylformamide` through); a limiting reagent at `0.0` mmol emptied the equivalents
comparison; and `layout_fits` accepted a 96-well plate declared as 1x2 with wells at row 98.

**Three fixes supersede the merged ADR**, and one of them is the only path in this tier to somebody
running the wrong conditions: an approval now returns to `draft` on any revision, because a chemist
approving 80 °C and an agent then drafting 200 °C left the header reading `approved` over the head
that `GET /protocols/{id}` serves. The other two: the loser of a real READ COMMITTED race gets a
`RevisionConflict` (and so a 409) instead of a raw `UniqueViolation` and a **500**; and a citation
counts only when it names something to open, because two bare sentences cleared the load-bearing
blocker.

Each fix was verified against the review's own reproduction rather than against a new test alone.
The remaining open items are unchanged and are in `docs/planning/BACKLOG.md`.

## 7 — Second review cycle (2026-08-29)

A second adversarial pass, over the code the first cycle's fifteen fixes left behind, found **six**
more — `D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing`. All fixed, in this repository
and in `Chemclaw3_ui`.

**The largest is a fix whose stated cost was paid by a record that did not exist.** `advanced()`
retires an `approved` status when a revision lands, correctly; the docstring paying for that said
"which revision *was* approved stays recoverable: `set_status` records it", and `set_status` wrote
one header column and logged a line without the revision in it. `experiment_protocol_status_events`
(077) is that record now — revision, actor, reason, append-only by grant, and returned on
`GET /protocols/{id}` so it can be read rather than merely stored.

**The `reason` was worse than latent.** The route validated it to 2,000 characters and dropped it,
while `Chemclaw3_ui` labels its box "recorded with the move", disables every status button until it
is filled in, and confirms "recorded against you with the reason you wrote" — a control an
interface *tells a person* is operating. `executed` joins `approved` as a status a revision retires
(a header saying a design was run, over a document that was not).

**And the UI could not render a protocol at all.** `ProtocolView` declared
`{ revision: DesignRevision }` where the service returns the revision flat, so `revision.design` was
`undefined` and the document page threw on `design.request.title` — under 808 unit tests and 8
browser tests, every stub and the e2e fixture emitting the same invented shape. Settled by dumping
`DesignOut.model_json_schema()`; fixed in `Chemclaw3_ui#55`, where re-nesting a stub now fails six
tests.

The rest: a `replicate_of` naming a real arm with different conditions (measured, a full 2-level
grid reported as "reduced design: 2 of 4" with zero checks failing); `render.summarise` as a fourth
caller re-spelling `has_protocol`; a duplicated `reaction_records` in the grant matrix; and one
merge that committed conflict markers into the ADR ledger, dropping eight of `origin/main`'s rows.

Each behaviour fix was proven non-vacuous by mutation — five mutations, each failing only its
intended test.

**What the full suite caught that nothing smaller did — six declaration registries.** Every one is
a place this repository makes you say out loud what you just added: a new turn outcome must be
reachable (`test_api_observability`), a new setting must be in `.env.example` (`test_config`), a new
`degraded()` subsystem must be declared (`test_degraded`), a new error code is mirrored by the UI
and mock repos (`test_event_contract`), a new metric needs a dashboard panel (`test_deploy_chart`),
a new `ChemclawError` subclass must be classified retryable or not (`test_publish`), and a new
session-scoped route must be in the ownership inventory (`test_service`). None of these is
reachable by running the tests for the thing you changed, which is the argument for running the
whole suite before believing any of it.
## 8 — Third review cycle (2026-08-29): six fresh-context reviewers, ~50 defects

Six independent reviewers, none of which wrote the code, each required to prove a finding by
reproduction. They found more than both earlier cycles combined, including defects those cycles
introduced. Phases below; each ships with its own tests and mutations.

- [ ] **P1 `source_text` is self-graded** — `basis="stated"` means "the chemist wrote this" and the
      haystack is a tool argument the model fills. Ambient, on `session_context`'s stated
      precedent ("not something the model should pass as a tool argument … the model must not be
      able to spoof it"). Fail closed when absent.
- [ ] **P2 Tier 1 correctness** — the abandonment race (20/20), `forbidden_absent` blind to
      `setpoints.solvent`, the bench document printing the body's conditions over an arm's,
      `setpoints_for` whole-object fallback, one arm in two wells.
- [ ] **P3 Availability + integrity** — the O(n²) `_labelled` (46 s of event loop from one
      request), NaN/Infinity (500 on pg, 200 in memory, stored ≠ served), NUL bytes.
- [ ] **P4 Checks that cannot fail** — `factor_levels_declared`'s "no factors" exit, the 0-mmol
      short-circuit, the four `_ok`-on-a-finding sites, `reaction_smiles` unread by the structure
      checks, `replicate_of` self-reference, `arms_are_distinct` prescribing a refused remedy.
- [ ] **P5 diff/render fidelity** — position-anchored `#index`, `None`-as-leaf inverting
      added/removed, fifteen dropped leaf fields (units among them), `summarise` on `mode`.
- [ ] **P6 Persistence** — byte-identical revisions demoting, the two backend divergences,
      `history()` reading every document, the unindexed default listing.
- [ ] **P7 Prose** — every false claim the audit found, including two in my own cycle-2 commits.
- [ ] **P8 `Chemclaw3_ui`** — the diff query params, 0-based plate columns, dropped 422 detail,
      orphaned arms on a level rename, request-stage "14 checks passed", swallowed errors, and
      fixtures the service would reject.