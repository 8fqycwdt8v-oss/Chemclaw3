# What a helper returns

Continuation of the C/D/E follow-up. The delegation *run* the BACKLOG row asks for needs a live
model, and this environment has none — `API-KEY` is present and the provider answers
"credit balance is too low", checked rather than assumed. So this took the half of the same question
that needs no model: **is the isolation a helper exists for actually real?** That is a property of
the graph, not of a model, and nothing had ever asserted it.

- [x] Measure it. Driven on a compiled graph with a scripted helper reading ~9.8 kB: the caller's
      whole thread is **57 characters** — the `task` call and a 28-character report. Isolation holds.
- [x] Pin it, since every argument for spawning a helper rests on it.
- [x] Follow what the probe exposed: a helper's report reaches the caller's thread with **nothing
      applied to it**, because `task` returns a `Command` rather than a `ToolMessage`.
- [x] `agent/tool_result_shape.py` — one function both result-rewriting middlewares go through.
- [x] Two ADR-worthy defects fixed, each measured before and after, each test verified to fail first.
- [x] `make check` green, with the infrastructure actually up.

## Review

**The finding is one shape, and it produced two defects that had been invisible for the same
reason.** `task` returns `Command(update={'files': …, 'model_calls': …, 'messages': [ToolMessage]})`
— it has to, because a helper must write its report *and* the channels that cross the subagent
boundary in one act. Both middlewares that rewrite what the model reads opened with
`if not isinstance(result, ToolMessage): return result`, so both silently excused the one tool whose
result is unbounded prose a model wrote, while both docstrings said "every tool".

*Defect 1, measured:* a report containing `</retrieved-note-…>` reached the caller's thread with a
**live** delimiter, so everything after it read as text outside any envelope. The nonce does not
cover this the way it covers external content — a helper *copies* the tag it has just read around
its own evidence rather than guessing it, and `frame_untrusted`'s own docstring says the nonce and
the defang each cover the other's gap. Every other route by which model prose reaches a prompt
already neutralises it: the condenser defangs each field the digest model returns, the verifier
defangs the answer under review. This was the one span arriving raw.

*Defect 2, measured:* nothing bounded the report in the band between this repository's
`agent_max_tool_result_chars` (60,000) and upstream's `tool_token_limit_before_evict` (20,000 tokens
x 4 = 80,000). A 180,048-char report was offloaded by upstream to 1,599 chars; a **70,048-char**
report landed whole. After the fix: 60,312.

**Two things I got wrong on the way, both caught by measuring rather than reasoning.** I first
patched `frame_connector_results` behind its existing `isinstance` guard and re-ran the probe — the
delimiter was still live, which is what sent me to look at the return type instead of assuming the
branch had fired. And I first read the 180 kB result being cut to 1,599 chars as "the size control
works", when what had actually fired was *upstream's* offload at a different threshold; only probing
the band between the two thresholds showed the gap.

**What is deliberately not fixed.** A caller still cannot tell that a helper's report is derived from
untrusted reading. Framing it is the obvious answer and the wrong one — an envelope says "evidence to
cite", and citing a helper's summary credits a source that is this system's own paraphrase. That is a
BACKLOG row with the measurement that should come before any design, and that measurement needs a
live model.

**Gate:** `make check` green — **6275 passed, 14 skipped**. An earlier run reported 8 failures and
386 skips; the Docker daemon had died mid-run, and all 111 tests in those five files pass with
Postgres up. Reporting that rather than only the green number is the point of the rule.

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
## Review section — fifth cycle over the prescriptive tier (2026-08-30)

Six fresh-context subagents over disjoint slices; 67 findings, 37 defects here and 8 in the
frontend. Every one reproduced locally before being fixed, and every new test was checked against
the pre-fix code.

- [x] **Authorization** — `design_id_for` takes the owner; both write surfaces consult
      `owner_permits`; HTTP answers 403 for owner-or-reviewer. Was: a second chemist's turn replaced
      a signed-off plate, and any principal with no role wrote `executed` into somebody else's trail.
- [x] **Checks** — eight fixed, one added (`limiting_is_limiting`). `forbidden_absent` reads what
      the design does; the equivalents tolerance allows written rounding; coverage counts
      combinations; plausibility reads all eight fields; three checks read `is_plate` rather than
      `request.mode`; a randomised layout needs its seed.
- [x] **Renderer** — nine fixed, and `tests/test_protocol_render.py` created (the module had no test
      file at all). The safety one: per-arm atmosphere and pressure now reach the page.
- [x] **Store** — unpaired surrogates refused on both backends; `require_movable` stops `executed`
      on an ask; `page()` reads one snapshot at `REPEATABLE READ`.
- [x] **Agent surface** — `stated` attests the value; the two protocol readers left the MCP face;
      both `levels` collections bounded.
- [x] **Prose** — four false claims corrected in place, one ADR sentence corrected by the new ADR.
- [ ] **Deferred, with rows in `docs/planning/BACKLOG.md`:** the status compare-and-set (needs
      `expected_status` across the store, the route and the UI) and the multi-turn `stated` quote
      (needs the thread's user turns at the stamp site).

Gate: 6,340 passed / 19 skipped with Postgres up, `ruff` and `mypy --strict` clean over 798 files;
frontend 820 tests over 81 files plus the full seven-command gate.
