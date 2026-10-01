# Queued compute, round 2 — the open points

- [ ] **Chemclaw3**: `tool_queued` event (state `queued` with an approximate position from the
      task queue's backlog, then `running`), emitted by `connectors/queued.py` while the turn waits.
- [ ] **Chemclaw3**: `queued:` for `chem` (the seven admission-gated tools) and `kinetics`
      (`semibatch_accumulation_profile`); chart `interactive:` for chem/kinetics and the pyexec
      example; ADR row unchanged (same decision, wider application).
- [ ] **Chemclaw3-mcp**: `queued:` in `manifests/{chem,kinetics,pyexec,rxnpredict}`; the stand-in
      `HttpEndpoint` learns `queued`; a fleet test holds *queued == admission-gated* per server.
- [ ] **Chemclaw3_ui**: mirror `tool_queued` in `shared/events.ts`; attach it to the open tool-call
      row; render "queued · N ahead" / "running…"; contract fixtures and tests.
- [ ] Fresh-subagent review of all three diffs; fix findings.
- [ ] PRs in dependency order — Chemclaw3, then Chemclaw3-mcp (its consumer-agreement lane reads
      Chemclaw3 `main`), then the UI (its backend-contract test reads Chemclaw3 `main`) — merge on green.

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
