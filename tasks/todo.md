# Anthropic Agent SDK features worth having here — implementation

Four items came out of an audit of the Claude Agent SDK against this repository's LangGraph
stack. Three are implemented here; the fourth is a design pass, because it touches the
middleware chain and the ADR has to come before the edit.

## 1. A turn's *spend* cap, beside its *iteration* cap — DONE

**The gap, corrected against the code rather than the prose.** The proposal said "Chemclaw only
caps by call count". That was half wrong: `api/budget.py` already meters tokens. What it does
with them is the gap — `check()` runs *before* a turn against usage already booked, and
`record()` books the turn *after* it finished. Nothing bounds spend **inside** a turn, and
`api/budget.py`'s own docstring states the belief that leaves the hole: "A single agent turn is
already iteration-capped, so one turn cannot loop forever." That caps *iterations*, not tokens.
A turn inside its 25-call ceiling can bill unboundedly — a wide fan-out of large tool results
against a 200k context is ~25 calls and millions of tokens — and the session budget finds out
one turn too late.

- [x] `agent/spend_cap.py`: meter in `wrap_model_call`, enforce in `before_model`
- [x] `billed_tokens` (`TurnTotal`) and `spend_capped` (`TurnFlag`) channels on `ChemclawState`
- [x] `agent_max_turn_billed_tokens` setting (0 = off, matching `_over`'s convention)
- [x] wire into `_middleware()` beside the loop cap
- [x] `spend_cap_reached` error code + `chemclaw_turn_spend_caps_total` + runner event
- [x] tests on a **compiled graph**, not on the hook

**Two design points, both measured rather than assumed.**

*Why `before_model` enforces.* `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`
— an `after_model` counter is short-circuited by any middleware that jumps from `after_model`.
`before_model` cannot be skipped. This is the same slot `loop_cap` occupies, for the same reason.

*Why the count is a state channel and not the ambient.* The turn's spend has to cross the
subagent boundary or a fan-out gets one budget each — regression 3 in `agent/loop_cap.py`'s
list. `TurnTotal` already folds concurrent writes additively. Probed on a compiled graph before
committing to it: `wrap_model_call` returning `ExtendedModelResponse(command=Command(update=…))`
reaches the channel and `before_model` reads it back — `[0, 100, 200]`, final `300`. The first
probe wrote a channel `ChemclawState` did not declare and LangGraph dropped it in silence,
which is the failure `tests/test_state_channels.py` exists to catch and is why the probe came
before the design.

## 2. Session fork — DONE

Branch a thread at its current checkpoint without mutating the original.

- [x] `agent/session_fork.py` — the copy, as SQL
- [x] `POST /sessions/{session_id}/fork`, authorized by the existing `resolve_session`
- [x] tests against a real Postgres schema

**Three things the research turned up that a naive fork gets wrong:**
- Every checkpoint PK leads with `thread_id`, so the fork is an `INSERT … SELECT` with the id
  swapped — no LangGraph API needed, and none exists (`adelete_thread` is the only thread-level verb).
- `checkpoint_blobs` is keyed `(thread_id, ns, channel, version)` and is **shared across a
  thread's checkpoints**, so copying only the tip loses channel values written at an earlier
  version. The whole thread is copied.
- A fork with no `session_messages` rows is **invisible** to `GET /sessions`: the owner listing
  `LATERAL`-joins `max(created_at)` and drops sessions with none. The transcript is copied too.
- The fork inherits the parent's **profile**, because a profile is attenuation-only and
  restoring the default would silently widen the tool surface.

## 3. Per-profile effort — DONE

- [x] `effort` on `AgentProfile` and `llm_effort` in settings
- [x] per-provider translation, gated the way `prompt_caching_middleware` is
- [x] tests asserting the constructed client, and asserting absence when unset

**Why this is not one shared kwarg.** The shipped chart runs `openai_compatible` against
`gpt-oss`, where `reasoning_effort` is a real parameter; `ChatAnthropic` has no such parameter
and spells the same idea `thinking={"type": "enabled", "budget_tokens": N}`, which additionally
must be under `max_tokens` and refuses a set `temperature`. So it cannot join
`_generation_options()`, whose contract is "caps both providers accept". An unset effort stays
**absent** from the request, which is that module's existing rule and matters more here than
elsewhere: a 400 from a rejected parameter is deliberately *not* failed over
(`_failover_exceptions`), so a bad value fails every turn rather than degrading.

## 4. Deferred connector tool schemas — DESIGN ONLY, NOT IMPLEMENTED

The most valuable of the four and the only one that needs an ADR before an edit, which is what
this item delivers. `docs/decisions/D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for.md`
states the measurement, the design, the three rejected alternatives and the restart condition.
No code in `agent/` is touched.

## Review

**What shipped.** Three guards (`agent/spend_cap.py`, `agent/session_fork.py` +
`POST /sessions/{id}/fork`, `AgentProfile.effort`), two ADRs, 21 new tests, and one design document
for the fourth item. `make lint` and `make type` green.

**Four things found while building that were not the task.** Each is the same shape — a claim in
prose that the code did not support — which is why they are listed rather than quietly fixed:

1. **My own first finding was wrong in the reassuring direction.** "Chemclaw only caps by call
   count" — `api/budget.py` meters tokens and has done all along. The real gap was narrower and
   more interesting: both its halves sit *outside* the turn. Writing the proposal from the
   architecture docs rather than the code produced a finding that was true in outline and wrong in
   the part that decides the design.
2. **`tests/pg.py::create_checkpoint_tables` ran `MIGRATIONS[1:4]`** — three `CREATE TABLE`s, none
   of the `ALTER`s — while its docstring claimed "the shape under test is the shape production
   has". Invisible to every test that only `INSERT`s named columns; immediate for the first one
   driving a real saver. Fixed here, since a fork test cannot exist without it.
3. **`tests/test_context_floor.py` undercounts by 7,799 tokens (~24%)**, in a file whose docstring
   argues its number "is the payload rather than an approximation of it". `@tool` is identity, so it
   measures raw callables while `create_agent` binds larger wrapped objects (all 49 differ), and it
   never sees the 7 `FilesystemMiddleware`/`SubAgentMiddleware` tools. **Not fixed here** — the
   corrected floor (~39,983) exceeds the ceiling it would have to be measured against, and this
   repository's rule is that raising a ceiling is its own deliberate commit. `BACKLOG.md` row added.
4. **`events.py` said `loop_cap_reached` was the *only* error sharing its turn with an answer.**
   True until this change, false after it; corrected in the same commit rather than left to rot.

**One thing deliberately not done.** Item 2 is designed and unbuilt. It changes what the model can
see, inside the chain that authorizes tool calls, and its failure mode is a wrong answer that never
names the capability it needed — not a slow turn. The ADR carries the measurement, three rejected
alternatives, and an explicit **stop**: if the eval corpus cannot separate the deferred arm from the
bound arm on *tool selection*, the schemas stay bound. That is the D-2026-08-12/13 precedent applied
before the work rather than after it.

**A false alarm I raised and then disproved, kept because the reasoning error is the lesson.**
Five `tests/test_api_sessions.py` tests failed with `psycopg.errors.UndefinedObject: operator class
"bit_jaccard_ops" does not exist`, and I called them pre-existing and environmental on the strength
of reproducing them on a stashed clean tree. That check was **confounded**: the full suite was
running in the background at the time, so both arms ran two pytest sessions against one Postgres.
With the suite finished the same five pass. The cause was concurrency, not the database.
"Reproduces without my change" is not the same claim as "reproduces in isolation", and only the
second one was worth making.

**One genuinely pre-existing failure, established the second way.**
`tests/test_message_migration.py::test_erasure_still_works_where_the_checkpointer_has_never_run`
fails with the same error *in isolation, on a stashed clean tree, with nothing else running* —
which is the test the paragraph above should have been. Root cause is the sandbox database rather
than the code: the `chemclaw` database has only `plpgsql` installed (`pg_extension` lists no
`vector`, and `pg_am` has no `hnsw`), so the migration that builds a bit-vector index has no
operator class to name. Untouched by this change and left alone.

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
