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

## Items

- [x] **Bound the workflow cache, from a measurement.** `durable/background_worker.py` set
      `max_concurrent_activities` and stopped there, and a **child workflow is not an activity** —
      so the one ceiling this repository chose never reached the bundle children core starts. Two
      of the facts needed were wrong in the obvious reading: the SDK default is **100**, not the
      500 its own constructor docstring names (that is the resource-based tuner), and the
      workflow-task ceiling is not what holds memory. `max_cached_workflows` is.
      `worker_max_cached_workflows` ships at 750, held as an inequality against the chart's
      memory request rather than as a literal, with a second test reading the `Worker(` call so a
      declared-but-unarmed setting reds. ADR + ledger + topic row; the old row replaced by what is
      left of it.
- [x] Fresh-context subagent review; five blockers, three of them factual and all three reproduced
      independently before acting: the `1.35` multiplier's arithmetic, the falsified "replay
      history" mechanism, the 500 (it is `workflow_task_executor`'s thread pool and it *does*
      apply, because the task ceiling is deliberately unset), `connectors/worker.py` never getting
      the ceiling at all, and an `ast` assertion that checked a keyword's name rather than its
      value. All five fixed; the ceiling moved 1,000 → 750 as a consequence of the third term.
- [ ] Full serial `make cov`, PR, merge on green CI.

## Measurements this wave rests on

Against the broker `make up` runs — the downloaded dev server is not fetchable here, which
`tests/temporal_env.py` already records. N workflows parked in `wait_condition`, RSS from
`/proc/self/status` against a 65.8 MiB idle baseline:

| cached | RSS | over idle | each |
|---|---|---|---|
| 50 | 72.5 MiB | +6.7 | 137 KiB |
| 100 | 76.9 | +11.2 | 114 |
| 250 | 85.7 | +20.0 | 82 |
| 500 | 101.5 | +35.7 | 73 |
| 1,000 | 135.1 | +69.4 | **71** |

And at 200 cached, scaling the workflow's own state: 16 KiB → 91 KiB each, 64 → 142, 256 → 347.

**The two-term model those numbers were first fitted to was wrong twice**, and the fresh-context
review caught both. `~75 KiB plus 1.35x state` took `1.35` as `347 / 256` — the *total* per-workflow
cost over the state — so the fixed overhead sat inside the quotient and was then added again.
Against this table's own figures the state coefficient is `(91−75)/16 = 1.00`, `(142−75)/64 = 1.05`,
`(347−75)/256 = 1.06`: **~1.05**, not 1.35. The bad model overstates a 256 KiB workflow by ~18%
(411 MiB per 1,000 against 339 measured) — safe-direction, and still a number nothing produced.

And "the excess being the replay history beside it" is falsified by this table: the excess over
state is *flat* in state, so it cannot be the state term's residual. History is its own axis, and
measuring it on its own at zero state and 200 cached:

| signals per workflow | each | signals delivered |
|---|---|---|
| 0 | 69.1 KiB | 0 |
| 20 | 198.9 | 4,000 |
| 100 | 245.9 | 20,000 |

Slopes 6.5 and 1.77 KiB/signal here against the reviewer's 0.95, so **the axis is established and
its coefficient is not** — which is why the third constant is named an allowance.

Three terms, then: `85 + 1.05×state + history`. At the asserted shape (256 KiB of state, a hundred
signals) that is ~529 KiB, so the SDK's 1,000 comes to ~516 MiB — over half the worker's 1Gi
request. **The ceiling ships at 750**, ~387 MiB. Lowering it beats raising a request every worker
Deployment shares, and an evicted workflow replays rather than fails.

## The measurement that was wrong first

The state-scaling arm initially read 0 KiB per workflow at every size — "state is free". The
fixture was `["y" * 1024 for _ in range(n)]`, which CPython constant-folds into *n* references to
**one** string, so 200 workflows "holding 256 KiB" held 1 KiB between them. A live-instance count
found it: 200 instances, 51,200 entries, 15 MiB of RSS — three numbers that cannot all be true.
The figure only became a measurement once the fixture allocated what it claimed.

That is the third time this session a measurement's *instrument* was the defect rather than its
logic: a tokenizer that matched quotes and missed YAML scalars, a sweep that never reached the leg
it was about, and now a fixture that allocated one object and reported 51,200.

## Review

Wave 13 shipped three things: Row A (Postgres identity), Row B (turn memory -> a durable thread-size
cap, `resources.service` 768Mi/1536Mi), and a background worker that could not boot at all
(`regex` imported outside the sandbox pass-through since #434) — found only because the lane was
started for Row B's measurement. A fresh-context review found seven issues in Row B's first cut
(wrong counter/alert, no entry refusal, SQL reading an older copy, YAML-0 fallback, a false ADR
claim, unstated gaps); all fixed. The full suite also caught that Row A had broken
`test_db_pool`'s split-gauge test, whose premise was the defect Row A fixed.

Gate: serial `make cov` 10,627 passed, 11 skipped (3 need `promtool`), 89.93% coverage.

Lesson worth keeping: `pkill -f <pattern>` inside a Bash call kills the calling shell when the
pattern appears in its own command line — use `pgrep` on a pattern the shell does not contain.
