# Artefacts, wave 3 — bindings and the html kind

Contract: "Wave 3 (frozen 2026-10-03)" of the artefacts wire contract, shared with the frontend
built in parallel — names and shapes exactly as frozen. Owner decision 2026-10-03: build both.

- [x] **0. Baseline** the targeted test files before the first edit.
- [x] **1. Result handle.** `bound_tool_results` stores every successful result's full text through
      the turn's sink (one write; a cut keeps its existing ref) and stamps the ref on
      `response_metadata`; an outermost presentation middleware appends `\n⟨r:<12 hex>⟩` outside
      the framed/defanged region. `runner_trace.returned()` reuses the stamped ref instead of a second
      write; the transcript pairs by the stamp. Grounding (`returned_values`, `stated_numerals`,
      `mentioned_ids`) never reads the handle (test). Measure what the handle costs the thread.
- [x] **2. `$bind` and `rows_from`** in the spec models; `exhibits/bindings.py` resolves them (RFC
      6901, session-scoped prefix lookup, ambiguity refusal, type checks, caps, off the loop for
      large results, a missing blob → null + ok:false). Stored spec keeps bindings with full refs;
      `ExhibitView` gains `raw_spec` + `bindings[]`, `spec` is resolved. Writes (tool + REST) resolve
      and validate; bound values are grounded; diff compares raw specs; exports use resolved values;
      `read_exhibit` shows both.
- [x] **3. `html` kind**: spec, `exhibit_max_html_bytes`, `agent_html_artefacts_enabled`,
      `html_enabled` on the list route, export `.html` as `text/plain` attachment, grounding over
      text content (stdlib `html.parser`), migration 117 widens the kind CHECK, create_exhibit names
      it. A test that no route answers `text/html` for artefact content.
- [x] **4. Docstring + skill** teach `$bind` compactly; re-measure the prefix
      (`tests/test_context_floor.py`) and the warm arm (`tests/test_compaction.py`) — stop and report
      if the warm arm would fail.
- [x] **5. Two ADRs** (bindings; html sandbox) with `Revisit when:` on what is declined; ledger rows.
- [x] **6. Verify**: lint, type, skill-validate, prose-validate, the targeted files with Postgres up.

## Review (wave 3)

- Handle: 17 characters, +4 tokens per result on the approximate counter (12 on cl100k); the
  compaction warm arm's unreclaimable batch now charges one per parallel call, 13,000 -> 13,034,
  and the arm reads 14,181 on this tree. No compaction or thread test failed before or after.
- Prefix: create_exhibit +75, read_exhibit +20; default 73,027 -> 73,122 under the 73,450 ceiling.
  No ceiling, budget or `PREFIX_BOUND` moved.
- Deviations from the frozen contract: none in names or shapes. Choices inside it: a handle is
  written only where the full text was stored (no handle on a failure, an empty or over-cap
  result, or a sinkless driver); a `rows_from` whose result is gone reads as no rows (`rows` is a
  list); a `rows_from` column pointer an element lacks is an empty cell; the html export filename is
  the existing `<title>-<xid>-r<N>.html`; `height` must be an integer >= 1.

# Artefacts, wave 2 — geometry, drafts, report artefacts, fork, push pruning

Contract: "Wave 2 additions (frozen 2026-10-03)" of the artefacts wire contract, shared with the
frontend built in parallel — names and shapes exactly as frozen.

- [x] **1. `geometry` kind.** `GeometrySpec` (xyz XOR source, label, energy_hartree,
      highlight_atoms); XYZ validated on write (count line, known elements, finite coordinates,
      `exhibit_max_atoms`); `source` must exist in the calc `ArtifactStore` at write time; migration
      116 widens the kind CHECK; export `xyz`; diff `xyz`/`source`/`label`; `create_exhibit`
      docstring names it; re-measure the prefix (`tests/test_context_floor.py`) and the warm arm
      (`tests/test_compaction.py`) — stop if the warm arm would fail.
- [x] **2. `GET /calc-artifacts/content?ref=`** — any authenticated caller; 404 unknown, 413 above
      `calc_artifact_max_download_bytes`, stored media type, sanitised `Content-Disposition`.
- [x] **3. `exhibit_draft` event** — derived from `create_exhibit`/`revise_exhibit` tool-call chunks
      in the graph stream; partial JSON; document only; throttled by
      `exhibit_draft_min_interval_ms`, growth only, capped by `exhibit_max_spec_bytes`; Event union,
      OpenAPI, dev page, contract fixture; test through the real graph stream with a chunking model.
- [x] **4. Report → artefact** — optional session/requester on the workflow input; an activity
      creates the `document` with id `xb-` + sha256(workflow_id)[:16], idempotent on retry;
      `job_completed.summary.exhibit_id`; `exhibit` pushed on `/events`; a deleted session skips.
- [x] **5. Fork copies artefacts** (head only, new ids, `forked from <xid> r<n>`).
- [x] **6. `exhibit_refs` 422 carries `detail.code = "invalid_exhibit_ref"`.**
- [x] **7. Retention prunes `exhibit` push rows** older than `exhibit_push_retention_hours`.
- [x] **8. Mock LLM scenario** creating a document artefact — only if the mock's design fits.
- [x] Verify: lint, type, skill/prose-validate, the targeted test files (Postgres up, helm on PATH).

## Review (wave 2)

- Prefix: `create_exhibit` naming the geometry kind costs +15 tokens (default 73,012 -> 73,027
  under the 73,450 ceiling); no ceiling or budget moved.
- Two ADRs: the geometry source is a citation checked on write and not pinned (with migration
  116's rollback reading), and the draft is read off the streamed call arguments for a preview only.
- The report push is new: a report never pushed `job_completed` before, so the payload is
  `{job_id, job: "report", summary, note_id, note_ref, exhibit_id?}` — `exhibit_id` omitted when
  the artefact was skipped. A second session rejoining the same report run gets no artefact.
- Interpretations to confirm with the frontend: `highlight_atoms` are 0-based; the geometry diff
  also names `energy_hartree`/`highlight_atoms`/`format`; a `done: true` draft frame closes a call
  when the text grew after the last throttled frame; FastAPI's own 422 for too many
  `exhibit_refs` keeps its list-shaped `detail`.

---

# Artefacts, phase 0 — measure, then decide

Concept: the "Exhibits" concept doc (UI label "Artefacts"; code name `exhibit` because `artifact` is
the calc by-product store). Decided by the user 2026-10-02: label "Artefacts", on by default with a
kill switch, session members may revise, `chart` in phase 1.

- [x] **0.1 Answers.** Over the recorded live transcripts (`tasks/live-test*/transcripts*`): how
      many answers carry a Markdown table (any / >= 4 data rows), what share of answer tokens the
      tables are, what share of table figures are verbatim tool values (`verified_numbers` — a
      figure absent there is *unchecked*, never "wrong", per `evals/live.py::_verified_numbers`),
      how many answers list >= 3 structures as SMILES, and how often `render_structure` ran.
      Instrument: `chemclaw.evals.answer_shape`; raw output in `tasks/artefacts-phase-0/results.md`.
- [x] **0.2 Prefix.** Draft the three tool signatures (`create_exhibit`, `revise_exhibit`,
      `read_exhibit`) with real docstrings and a compact spec schema; measure them with the exact
      basis `tests/test_context_floor.py` uses (`convert_to_openai_tool` +
      `count_tokens_approximately`); compare to `MAX_SINGLE_TOOL_TOKENS` and the ceiling headroom.
- [x] **0.3 ADR** `D-2026-10-02-an-artefact-is-part-of-the-answer-not-an-effect` + ledger row:
      server-owned versioned exhibits vs client-only; not plan-gated but subtracted from helpers;
      bind-don't-retype (with 0.1's numbers); HTML/JS artefacts declined with `Revisit when:`;
      prefix budget from 0.2.
- [x] Verify: `make lint type test` (Postgres up; report skips), decision-log tests.
- [ ] PR, merge on green.

## Review (phase 0)

- 0.1 refuted the concept's token argument for bindings (tables are 2.7% of answer tokens), so
  bindings and result handles are deferred with a trigger; documents (15.3% of answers) are the
  core kind.
- 0.2 chose the untyped server-validated spec: 961 prefix tokens vs 1,989 typed (whose create tool
  alone breaks `MAX_SINGLE_TOOL_TOKENS`). Headroom at base is 404, so phase 1 raises the ceiling.
- Full serial suite with Postgres/Temporal up: 11,415 passed, one failure (evals importing RDKit
  directly) fixed by going through `core.chem`; skips were helm (92) and the sibling fleet (8).

---

# Queued compute, round 2 — the open points

- [x] **Chemclaw3**: `tool_queued` event (state `queued` with an approximate position from the
      task queue's backlog, then `running`), emitted by `connectors/queued.py` while the turn waits.
- [x] **Chemclaw3**: `queued:` for `chem` (the seven admission-gated tools) and `kinetics`
      (`semibatch_accumulation_profile`); chart `interactive:` for chem/kinetics and the pyexec
      example; ADR row unchanged (same decision, wider application).
- [x] **Chemclaw3-mcp**: `queued:` in `manifests/{chem,kinetics,pyexec,rxnpredict}`; the stand-in
      `HttpEndpoint` learns `queued`; a fleet test holds *queued == admission-gated* per server.
- [x] **Chemclaw3_ui**: mirror `tool_queued` in `shared/events.ts`; attach it to the open tool-call
      row; render "queued · N in queue" / "running…"; contract fixtures and tests.
- [x] Fresh-subagent review of all three diffs; fix findings.
- [ ] PRs in dependency order — Chemclaw3, then Chemclaw3-mcp (its consumer-agreement lane reads
      Chemclaw3 `main`), then the UI (its backend-contract test reads Chemclaw3 `main`) — merge on green.

## Review (round 2)

Three fresh reviewers, one per repo; every finding below was fixed unless it says otherwise.

- Chemclaw3: the `waiting` count never arrives on Temporal 1.25.2, because the server sends no stats. The process now stops asking after the first stats-less answer, and the test requires a count from a server that reports one.
- Chemclaw3: a running call could flip back to "queued" just before its result. With no pending activity the tick now reports nothing.
- Chemclaw3: progress reads are now bounded by the tick (`rpc_timeout`), the result is checked before a timeout, and a failed run on the fallback path is the tool's refusal, not an exception.
- Chemclaw3: the backlog is read once per connector per tick, and every waiting turn shares that read.
- Chemclaw3 (not fixed): `call_id` on `tool_queued`. The UI pairs by `job_id`, which already tells apart two calls to one tool with different arguments; identical arguments share one run.
- Chemclaw3-mcp: the stand-in now refuses a duplicate queued name. The consumer-agreement table has six `queued:` probes. The queued-equals-gated test runs over every agent-facing server, and a separate test fails if the gate reader goes blind.
- Chemclaw3_ui: annotations pair by `job_id`. The wait has its own activity kind, so a screen reader hears it. A zero count reads "queued…" and the badge says "N in queue". Ended rows drop the annotation.


Measured before starting: a queued call costs ~80 ms over a direct one (median 198 ms vs 116 ms for
a 50 ms tool, 20 sequential calls each, `make up`'s Temporal) — cheap enough to queue every gated
tool, including chem's depictions.

---

# Queued, autoscaled compute — near-real-time for many concurrent chemists

**Goal.** Every compute-heavy tool call waits in a global queue instead of being refused, the chemist
waits briefly in the turn and gets the answer, and capacity follows the queue. Target to agree:
seconds-class calls (xTB single point, pKa, properties, reaction prediction) answer **p95 ≤ 30 s at
50 concurrent chemists** on warm capacity. Hours-class work (CREST, scans) is queued fairly, not
real-time.

## What exists, and the gap (from the two code maps)

- `connectors/jobs.py::build_job_tool` + `_await_briefly` already do "start a Temporal workflow,
  wait `inline_wait_seconds`, else return a job id and push the result later". Only the 12 calc
  `jobs:` use it.
- The 17 inline calc tools, BO `suggest_next_experiment`/`predict_outcome`, and every remote MCP
  tool (rxnpredict, pyexec, chem) bypass Temporal. A full pod refuses; inline nothing retries.
- The durable retry waits 112 → 900 s between asks: polling, idle slots, no FIFO.
- No HPA on any worker; MCP servers scale on CPU only; no admission-slot gauge exists.
- rxnpredict/pyexec/rxnlabel/chem refuse with a plain `ValueError` — no at-capacity marker, so a
  full pod reads as bad input.

## Design

1. **One mechanism: a tool may declare `dispatch: queued` in its manifest** (with
   `inline_wait_seconds`). The chat service wraps it: authz/audit/plan-gate run as today, then it
   starts `QueuedToolWorkflow` on the bundle's **interactive lane** `connector-<name>-interactive`
   and reuses `_await_briefly`. Answer inside the wait → ordinary tool result. Otherwise → job id,
   result pushed through the existing session mailbox. No new result path.
2. **The activity calls the connector's own MCP tool** (same session helper, same identity headers),
   so no tool body moves and the calc cache (`cached_compute`) stays where it is. Works unchanged for
   Chemclaw3 bundles and for `Chemclaw3-mcp` servers.
3. **Two lanes per bundle so hours never block seconds**: interactive (queued tools, short
   `start_to_close`) and the existing batch lane `connector-<name>` (jobs, CREST).
4. **Pull, don't push**: a worker's `max_concurrent_activities` equals the downstream slots it owns,
   so excess work waits *in Temporal* (FIFO, visible) instead of being refused. At-capacity becomes a
   rare race, retried at seconds on the interactive lane (new `interactive_retry`), not minutes.
5. **One at-capacity marker for the whole fleet**: move `[…-at-capacity]` into
   `mcp_server_kit.Admission` so every gated server signals it, and Chemclaw3's generic connector path
   classifies it too.
6. **Autoscaling on backlog, not CPU** (KEDA = OpenShift "Custom Metrics Autoscaler"):
   - interactive workers scale on the Temporal task-queue backlog;
   - MCP servers scale on a new `chemclaw_mcp_admission_in_flight / _ceiling` gauge;
   - warm floor (`minReplicas`) sized for the expected peak — pod start (~60 s+) is too slow to be
     the real-time answer, so scaling handles sustained load and the floor handles bursts.
   - Shipped **optional** (`autoscaling.keda.enabled`), CPU HPA stays the default where KEDA is absent.
7. **The chemist sees it**: new `ToolQueuedEvent` (position ≈ backlog ahead, then running) over the
   existing signal stream; UI renders it on the tool call.

## Steps

### Chemclaw3-mcp (PR 1)
- [x] ADR: admission refusal carries one fleet-wide marker; autoscaling may read admission occupancy.
- [x] `mcp_server_kit.limits.Admission`: server name, fleet marker, gauges `admission_in_flight`,
      `admission_ceiling`, counter `admission_refused_total`; all five gated servers inherit.
- [x] Optional `deploy/keda/scaledobject.yaml` per gated server (Prometheus scaler on occupancy);
      extend `tests/test_deploy_shape.py` to hold it against the HPA (same bounds, never both applied).
- [x] `make check` green (deps-audit red on pyjwt/urllib3 advisories, identical on main). Pushed.

### Chemclaw3 (PR 2)
- [x] ADR superseding the "inline compute is synchronous" half of the calc/connector ADRs; states the
      two lanes, pull-based concurrency, and what stays refused-at-server (safety net).
- [x] Manifest: `dispatch: queued` + `inline_wait_seconds` on a tool; `connector-validate` checks the
      wait against the turn budget (reuse the jobs check).
- [x] `QueuedToolWorkflow` + generic `call_connector_tool` activity; `interactive_retry` policy.
- [x] Registry wraps queued tools; generic path classifies the fleet at-capacity marker.
- [x] Mark heavy tools queued: calc compute tools, BO `suggest_next_experiment`/`predict_outcome`,
      rxnpredict predictions, pyexec `run_python`. Cheap ones (solubility, developability, lookups)
      stay direct.
- [x] Worker per bundle per lane; concurrency derived from the downstream ceiling, validated at startup.
- [ ] ~~`ToolQueuedEvent`~~ not built: a call that outlasts the wait already announces `job_started` and lands as `job_completed`, which the UI renders. A position indicator is a follow-up.
- [x] Helm: interactive worker Deployments, optional KEDA ScaledObjects, warm floors; `helm-validate`.
- [ ] `make lint type test` green with Docker/Postgres/Temporal up (report skips).

### Chemclaw3_ui (PR 3)
- [ ] Not needed for this change (existing job events cover a deferred call); a queued/position badge is a follow-up.

### Verification (the claim is a number)
- [x] Load test in the local stack: 50 concurrent sessions each asking a seconds-class calc tool,
      before vs after. Report p50/p95 time-to-answer and refusal rate. Same for a mixed load with
      CREST running, to show the lanes isolate.

## Decided with the user (2026-09-30)
- Target: seconds-class calls **p95 ≤ 30 s at 50 concurrent chemists**.
- KEDA availability unknown → ScaledObjects ship behind a switch, **default off**; CPU HPA stays default.
- retro (`chemclaw2_retrosynthesis`) out of scope; `dispatch: queued` makes it a one-line opt-in later.

## Review (2026-09-30)

- Measured, 50 concurrent calls vs a stand-in server (worker concurrency = slots): direct refused
  68% at 8 slots; queued answered 50/50 in the turn, p95 14.9 s (2-s calls, 8 slots), 8.7 s (16),
  6.9 s (32); 5-s calls need 16 slots for p95 ≤ 30 s. The warm floor is the sizing decision.
- Found on the way: `CalcBusyError` reached callers of our own bundles as "internal error" (the
  sanitizer only passed `ValueError`) — fixed with `AtCapacityError` + the fleet marker.
- A retry policy with initial > max is rejected by Temporal as "missing task queue name"; clamped.
- The interactive worker opens no Postgres pool (measured 0 connections); budgeted at zero with a test.
- Follow-ups: `queued:` for `Chemclaw3-mcp`'s `pyexec`/`chem` manifests once this lands on main (the
  consumer must accept the key first); a queued/position badge in the UI.

---

# Wave 11 — what a worker holds between tasks

---

# A screen answers per item; a distribution refuses by name

Source: arXiv 2609.14840 (El Agente Potente, typed execution graphs for MLIP campaigns). Its one
pattern this repository does not already have: **every input to a high-throughput run ends as a
typed outcome** — a result, or a typed failure naming the input — so one bad structure never costs
the campaign and is never silently dropped. Everything else in the paper is either already here
(typed dispatch over `XtbJobSpec`, per-primitive caching, provenance) or already decided against
(MACE: licence; a coding mode reaching validated functions: `D-2026-08-25-a-sandbox-is-a-server-not-a-verb`).

## The defect, measured before planning

`connectors/calc/compose.py`: the four list-taking composites run their items with no per-item
boundary, so the **first** item that raises aborts the whole durable job.

| Composite | Job | Items | Today |
|---|---|---|---|
| `bond_dissociation_survey` | `survey_bond_strengths` | bonds (independent) | 1st failure aborts |
| `solvent_comparison` | `compare_solvents` | media (independent) | 1st failure aborts the `gather` |
| `species_solvent_comparison` | `rank_species_across_solvents` | media (independent) | same |
| `species_ranking` | `rank_species` | species (**not** independent) | 1st failure aborts, names only that one |

## Design decisions

1. **The per-item boundary catches `ValueError` and nothing wider.** That is exactly the
   repository's existing "bad data" contract: `ChemclawError` (a `ValueError`) is documented as
   "catch this at batch boundaries (reject-and-continue)"; `CalcToolError` (a server refusal),
   `CalculationDomainError`, `InvalidSmilesError` and pydantic's `ValidationError` are all
   `ValueError`s, and `ValueError` is on `durable/publish.py::_BAD_DATA_TYPES` — i.e. already
   declared deterministic for that input. **Outages must not become item failures**:
   `CalcServerError` / `CalcBusyError` are `SubsystemUnavailableError` (not `ValueError`) and must
   still propagate so Temporal retries the activity; `CancelledError` is a `BaseException` and
   propagates. A test drives each of those through the boundary.
2. **Independent items (bonds, media) → typed failure, the rest still answered.** New models in
   `science/calc/models.py`: `FailedMedium(solvent: str | None, reason: str)` (two callers) and
   `FailedBond(atoms, bond, fragments, reason)`. New field `failed: list[...] = []` on
   `SolventComparisonResult`, `SpeciesSolventComparison`, `BondDissociationSurvey` — defaulted, so
   an in-flight run's payload without it still decodes.
3. **A distribution is not independent items — it refuses, by name, after trying every species.**
   Populations normalise over the set, so ranking a subset is "confident about the wrong universe"
   (the composite's own docstring). Dropping the failed species is therefore wrong, and so is
   returning partial populations. `species_ranking` attempts *every* species (each success is
   cached, D-011, so the rerun without the offender pays nothing for them), then raises one
   `ValueError` naming each failed species and its reason. Today it names only the first.
4. **Everything failed → one `ValueError` naming every item.** No empty result, no fabricated
   ranking.
5. **Honest aggregates over the survivors**, each with a warning:
   - failed items are listed in `warnings` too (the publish projection turns warnings into
     `calculation_flag` rows, so a published record carries the gap with no projection change);
   - bond survey: `is_weakest` is the weakest *of the computed bonds*, and the warning says a
     failed bond may be weaker; `considered == len(bonds) + len(failed)`; `method` taken from a
     computed bond;
   - solvent screens: fewer than two media computed → no spread/"does not distinguish" claim;
     say there is nothing to compare instead. A lost gas-phase reference is named as such.
6. **Activity summaries carry the gap** (`connectors/calc/activities.py`): a completion push-back
   must not read "weakest of 5 bonds" when 2 were not computed.
7. **The job descriptions say it** (`connectors/calc/connector.yaml`, which is the prompt): the three
   screens report per-item failures under `failed`; `rank_species` refuses naming every failed form.
8. **One small helper, four callers**: `_attempt(awaitable) -> result | ValueError` in `compose.py`.
9. **Out of scope, argued**: `durable/orchestrator.fan_out` drops a failed child (D-030) — its report
   caller already reconciles the gap into a visible `retrieval_failed` marker and its memory caller
   counts it on `chemclaw_fan_out_children_dropped_total`; changing its return type is a separate
   decision. Template waves abort on a failed step by design (a step's output feeds the next).
10. **ADR**: `D-2026-09-27-a-screen-answers-per-item-a-distribution-refuses-by-name.md` — a choice
    between options (drop / partial populations / refuse) and the boundary class, not a defect fix.

## Items

- [x] `science/calc/models.py`: `FailedMedium`, `FailedBond`, `failed` fields (defaulted).
- [x] `compose.py`: `_attempt` helper; `bond_dissociation_survey` per bond; `solvent_comparison`
      and `species_solvent_comparison` per medium (inside `one()`, so `gather` still propagates
      outages); `species_ranking` try-all-then-refuse-by-name; the all-failed refusals; the
      fewer-than-two-media wording.
- [x] `activities.py`: summaries for the three screens name the failed count.
- [x] `connector.yaml`: four descriptions.
- [x] Tests (`tests/test_calc_ensembles.py`, `tests/test_calc_compose.py`, `tests/test_calc_jobs.py`),
      driven through `FakeCalcServer.overrides` so a refusal arrives on the real wire path as
      `CalcToolError`:
  - [x] survey: one bond refused → other bond answered, `failed` names it with the server's reason,
        `considered == bonds + failed`, warning says a failed bond may be weaker.
  - [x] survey: every bond refused → `ValueError` naming each.
  - [x] survey: an outage (`CalcServerError`) on one bond propagates, nothing is returned.
  - [x] solvent screen: one medium refused → ranked over the rest, `failed == [FailedMedium]`.
  - [x] solvent screen: only one medium left → no "does not distinguish" claim, says nothing to compare.
  - [x] solvent screen: every medium refused → `ValueError`.
  - [x] species screen: a species refused in one medium only → that medium in `failed`, others ranked.
  - [x] ranking: one species refused → `ValueError` naming it; every other species was still relaxed,
        and ranking the set without it relaxes nothing new.
  - [x] ranking: two species refused → both named in one error.
  - [x] ranking: `CalcBusyError` propagates as itself (stays retryable).
  - [x] jobs: a survey job with a refused bond has a summary naming the failure.
  - [x] wire: a payload without `failed` still validates.
  - [x] mutation check: remove the boundary → the per-item tests go red.
- [x] ADR + ledger row.
- [x] `make lint type`, targeted tests, then full serial `make test` with Postgres up; report skips.
- [x] Fresh-context subagent review (correctness; contract/wire/publish; docs-vs-code), fix findings.
- [ ] PR, CI green, merge, delete branch.

## Review

Three fresh-context reviews (correctness; contracts and consumers; prose and tests), every finding
reproduced before acting. What they changed, beyond the plan:

- **Two CI blockers the local calc tests could not see**: the publish field guard (`failed` read by
  no projector) and the context-floor ratchet (`rank_species` +77). Fixed by publishing each failed
  item as its own flag, and by trimming the description sentences to 11-21 tokens before
  re-recording three figures.
- **The parent of a bond survey and a screen's equation are input, not items** — as items, a
  refused parent was asked for once per bond. Computed/checked once, up front.
- **A published record must not overstate a partial answer**: no spread/winner/swing from one
  medium, no `weakest_bond` from a survey with a missing bond, and reasons in JSONB `detail`
  because a flag message is `VARCHAR(2000)` at the sink.
- **The ADR's own headline example was false** (the jobs' precondition refuses an unparameterised
  solvent before launch) and its mutation counts were stale twice; both re-measured.
- **Out of scope, filed**: the calc server's inline time budget refuses as a plain `ValueError`,
  so a load-dependent stop is one item's failure. A server-side marker is a cross-repository
  contract change — `BACKLOG.md` row.
- Lesson: a count or example written into an ADR before its review round is written twice; measure
  it last.
